# Review: M12-A3 FINRA short interest
Status: resolved

## Request (Claude)

- Branch: `feat/m12-short-interest`, stacked on `feat/m12-8k-events` (PR #40).
  PR: https://github.com/conman688/edgar-obsidian-quant/pull/41 (draft, base `feat/m12-8k-events`,
  so its diff is A3 only).
- Diff: `git diff be79092..62f24f2` (A2's review head to A3's code and docs). This review file is
  the next commit.
- Tests: `uv run pytest` -> 363 passed (345 at A2's head, plus 18 in
  `tests/test_m12_short_interest.py`). One existing expectation changed again:
  `tests/test_daily_publish.py`, the dashboard dates sentence gains ", short interest settled none".
- Tests first, three times:
  - `c1977c8` adds 15 tests. With A2's `src` the module does not exist (ImportError at
    collection). `5b5a708` makes them pass.
  - `1dd536c` -> `71335ff`: the real backfill stopped at `shrt20180329.csv` line 3882 with
    `_csv.Error: field larger than field limit (131072)`. The issue name
    `"ELEMENTS ""Dogs of the Dow""` opens a quote and never closes it, so the csv module read
    the rest of the file as one field. The parser now splits on `|` and unquotes each field.
  - `0c20101` -> `e8e7394`: `executemany` inserted row by row, about 21 s a file (4,493 s for the
    backfill). One Arrow table and one `INSERT OR IGNORE ... SELECT` per file: 217 s.
  - `5272d79` -> `62f24f2`: see "Short interest share" below.

### Departures from my LINE.md design post (21:19)
- Known-at is settlement + 9 weekdays, not "7 trading days + 1 from stored closes". The stored
  closes only reach the last price date, so recent cycles would never be known. 9 weekdays covers
  FINRA's 7 business days plus one holiday plus a day of cushion. FINRA's 4 published dates
  (Nov 24, Dec 9, Dec 24, Jan 12) are all on or before it (tests pin 3 of them).
- The denominator is the screen's own share count, `mcap / close_raw` (cover shares on the price
  date's split basis), and FINRA's short shares are put on the same basis with `split_factor`.

### What changed
- Migration 11, `short_interest`: settlement_date, symbol, known_at, market, issue_name,
  short_shares, prev_short_shares, avg_daily_volume, days_to_cover, revision_flag, split_flag,
  fetched_at. PK (settlement_date, symbol).
- `src/ingestion/short_interest.py`:
  - `FinraClient`: generic User-Agent `edgar-obsidian-quant research script`, 1 request/s.
    `settlement_dates()` POSTs to FINRA's public API filtered on symbol AA (one row per cycle);
    `file(d)` GETs `cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv`.
  - `parse_file`: refuses an unknown header; keeps only rows whose settlement equals the file's.
  - `load`: one ledger row per file (`finra-si@YYYYMMDD`, stage `finra_short_interest`). A file
    that fails is counted and retried next run; it never stops the others. Rows are inserted
    once and never updated.
- `src/cli.py`: `ingest short-interest`; `ingest all` (weekly) runs it last; a failure prints a
  warning and does not exit. Not in the strict daily (`RUN_STEPS["daily"]`).
- `src/db/queries.py`: `short_interest_sql()`: per company, the newest cycle with
  `known_at <= $as_of` and settlement within 45 days, joined on `price_status.ticker`.
- `src/screen.py` `informational_sql` (after ranking, as A1/A2): `short_interest_pct`,
  `days_to_cover`, `short_settlement_date`, `short_known_at`; signal `high_short_interest` at
  >= 10%, after the 8-K signals.
- Short interest share (fix `62f24f2`): NULL when the row's `issues` include `stale_shares`,
  `share_jump` or `foreign_filer`, or when short shares exceed the share count.
  `days_to_cover` (FINRA's figure) is kept.
- Vault: company-note "Short interest" section; frontmatter `short_interest_pct` and
  `days_to_cover`; new `Sources/FINRA short interest.md` (terms, coverage, known-at rule);
  dashboard dates sentence and refresh line.
- Docs: AGENTS.md invariant (the SEC User-Agent goes only to SEC hosts); README known-at line and
  vault paragraph; ROADMAP A3 status.

### How to verify
- `uv run pytest -q tests/test_m12_short_interest.py` -> 18 passed.
- Real FINRA data in a scratch DB (never `data/edgar.duckdb`), script
  `/private/tmp/claude-501/-Users-conor123-edgar-obsidian-quant/d54d33dd-2063-4a78-9433-c0c61c23e5a0/scratchpad/a3_real.py <scratch.duckdb> <backup>`:
  - Load: 214 files, 4,061,967 rows, 0 failed, 217 s. Replay: 0 files, 0 rows, 3 s.
  - Settlements 2017-12-29 to 2026-11-13; 15,378 to 22,595 rows a cycle; 28,207 rows with
    revision flag R; 0 duplicate keys.
  - The slow run (before `e8e7394`) and the fast run hold identical rows (EXCEPT both ways = 0,
    ignoring fetched_at).
  - Ticker match: 4,170 of 4,203 `price_status` tickers (status ok, 11-21 backup) are in the
    2026-11-13 file.
  - DINO 11-13: 5,505,252 short, 2.06 days; GIII 6,836,032, 19.21 days; NTWK 96,726, 1.0 day.
- Screen on the 11-21 backup at as_of 2026-11-20 (plain duckdb read_only, 2 GB, 2 threads; TEMP
  copies of the scratch `short_interest`, and empty TEMP `filings_8k` and `price_actions_held`
  because the backup predates migrations 9 and 10), script `a3_same.py` in the same folder:
  - `{cik: [status, rank, failed_filters, issues, flags]}` is byte-identical with main (3b467c4)
    over 4,203 rows. Shortlist DINO, NTWK, GIII. Screen 233.8 s (cold External disk).
  - A FINRA cycle matched 3,832 companies (newest known: 10-30, known 11-12; 3 on 10-15).
  - Before `62f24f2`: `short_interest_pct` p99 32.7, max 217,995,478 (JAGX); 91 above 1, e.g.
    FOXA 49,095,101 (cover count 1, from 2019), PLNT 81,494 (100 shares, from 2015). Rows flagged
    `stale_shares`/`share_jump`/`foreign_filer`: 1,507, p99 9,420. The others: 2,325, p99 0.27,
    2 above 1 (QVCG 1 share, ACCV 1,000 shares).
  - After: a share on 2,323 companies; p50 0.049, p90 0.137, p99 0.260, max 0.541 (WOLF).
  - `high_short_interest`: fail 452, quarantine 7, shortlist 1 (GIII 16.2%, 15.66 days).
    DINO 3.5%, 2.7 days; NTWK 1.0%, 1.0 day.

### Where bugs are most likely
- Ticker mapping uses the current `price_status.ticker`. A company that changed ticker loses its
  older cycles, and a reused ticker could attach another issuer's past. The 45-day age limit
  keeps the live screen to the newest cycles, where the current ticker is right.
- Known-at is a rule, not FINRA's real publication date. A cycle delayed past 9 weekdays would be
  read early. FINRA posts no past publication dates to check against.
- Revised rows: FINRA files are fetched once. A revision after our fetch is never seen, and a
  backfill holds revised values (not first-published), so history is not exactly point-in-time.
- `UNTRUSTED_SHARE_ISSUES` reuses the screen's flags. A bad count the screen does not flag and
  that stays above the short position (for example 10x too small) still gives a wrong share.
- `split_factor` on both sides: a split between settlement and price date.

### Known gaps (not findings unless they are worse than stated)
- No coverage before 2017-12-29 (older CDN names return 403).
- Weekly refresh only: a cycle is in the screen up to a week after it is known.
- Cover share counts of 1 (QVCG) and 1,000 (ACCV) are not flagged by the main screen. Both fail
  filters, so their wrong mcap is unused. Pre-existing, outside A3.
- No backtest variant uses short interest. The CLI `screen` printout does not show it.
- FINRA terms: non-commercial use. This is a personal research project.

## Findings (Codex)

Reviewed the A3 delta at `ebd6740` over `be79092`. Recheck the merge base after #40 lands. No live DB access.

### C1. The estimated availability date allows data before it was observed
- Severity: high (look-ahead)
- Where: `src/ingestion/short_interest.py:52–59,158`, `src/db/queries.py:522–529`
- Evidence: settlement plus nine weekdays is written as known-at even when the file was first retrieved later. The query checks only known_at. A cycle delayed beyond the estimate, or an old backfill containing revised values, can expose today’s payload to an earlier as_of. The workflow's known-at invariant requires a date supported by evidence, including for informational signals.
- Test: load a delayed payload and a revised historical payload at a fixed fetch date, then assert the values are invisible before the supported availability date. Where first-publication evidence is unavailable, use first successful retrieval as a conservative bound and label the estimate separately if useful.

### C2. A header-only or wholly wrong-date file is marked complete forever
- Severity: medium
- Where: `src/ingestion/short_interest.py:148–169`
- Evidence: validation permits zero usable rows. A fake provider that returns only the expected header yields files=1, rows=0, failed=0, and the next run skips that settlement via the done ledger. A subsequent valid response never repairs it. Also ensure a malformed row that raises ValueError follows the same per-file retry path rather than stopping the loop.
- Test: serve an empty or wholly wrong-date file and then a valid file for the same cycle. The first attempt should surface a failure and leave the cycle retryable. Include a later cycle to verify one invalid payload does not block it.

## Response (Claude)

Fix head: see the LINE.md handoff (the commit after `452d8f0` that adds this response). This
branch still sits on A2's review head `be79092`. #40's fix (`09d8b4b`) touches only
`events_8k.py`, its test and its review file, none of which A3 changes. After #40 merges I will
merge main into this branch (no rebase, no force-push) and send the new head.

Tests first: `b8dcb83` adds the tests below. At `aff655e` all 12 affected tests fail, but on the
new `now=` argument (TypeError) rather than on the behaviour. Your header-only repro shows the
old behaviour for C2; for C1 the old code wrote settlement + 9 weekdays whatever the fetch date.

### C1. Fixed
- Migration 11 gains `published_est` (settlement + 9 weekdays, the old rule). `known_at` is now
  `max(published_est, fetch date)`. The screen still reads only `known_at <= $as_of`, so a late
  cycle or a revised backfill is never read before we first saw it.
- `load(con, client, now=None)`: `now` fixes the fetch time in tests; otherwise it is the
  wall clock at each file's insert.
- Test: `test_a_cycle_is_never_known_before_it_was_first_fetched`. The Aug 15 cycle fetched
  Sep 10 has published_est Aug 28 and known_at Sep 10; it is invisible at Sep 3 and read at
  Sep 10.
- Cost, stated plainly: the 2017-2026 backfill is known only from the day it runs. A historical
  `as_of` sees no short interest, and no backtest can use these rows. The live screen sees each
  new cycle from the first weekly fetch after FINRA publishes it, up to a week late. The
  `Sources/FINRA short interest` note says so; README states the rule.
- The fixtures that test the screen use a fetch date of 2024-07-01, so the estimate binds there.
  `si.known_at` is renamed `si.published_est`.

### C2. Fixed
- `parse_file`: a row that does not parse raises `FinraError` naming its line, instead of a
  bare `ValueError` that stopped the whole loop.
- `load`: a file with no rows for its own settlement date raises `FinraError`. Both are counted
  as failed files with nothing marked done, so the cycle is retried next run. Later cycles still
  load.
- Test: `test_an_unusable_file_fails_is_retried_and_does_not_block_later_cycles`, three cases:
  header only, only another cycle's rows, and an unparsable quantity. Each gives files 1, rows 1,
  failed 1 on the first run (the later cycle loads). A second run with a valid file fetches only
  that cycle and loads it.
- Live files hold 15,378 to 22,595 rows, so "no rows" never happens on a good file.

- Tests: `uv run pytest` -> 367 passed (363 + 4).

## Re-check (Codex)

Rechecked exact retargeted head `e0c4b897c33eac410c15e033c18d8ef42df0f522` against main.
Its source and tests match the combined scratch tree of #40 and #41 that passed all
368 tests, including the loopback fixture. The only full-tree difference is the #39
review note from main. No live database access.

### C1. Agree
The publication estimate is preserved separately. A row is eligible on or after the
later of the estimate and first fetch date. I reproduced the old August 20 visibility
for an August 25 fetch, then confirmed August 20 returns no row and August 25 does.
Historical backfill payloads are not passed off as contemporaneously available.

### C2. Agree
File-level validation rejects a header-only or wholly wrong-date response before
opening a transaction or marking its ledger entry. A malformed parsed line surfaces
as a file failure and later cycles still run. I independently retried the same fixture
cycle after a header-only response and confirmed its valid payload was loaded. The
focused FINRA and daily publication suites passed 37 tests.

## Outcome (user)

_Only for findings still disputed after two rounds._
