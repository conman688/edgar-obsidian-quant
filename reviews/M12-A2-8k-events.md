# Review: M12-A2 8-K red-flag events
Status: awaiting-response

## Request (Claude)

- Branch: `feat/m12-8k-events`, stacked on `feat/m12-insider-sells` (PR #39). PR: see LINE.md
  handoff (draft, base `feat/m12-insider-sells`, so its diff is A2 only).
- Diff: `git diff b0e31d7..b1c4b50` (A1's round-1 head to A2's code and docs). This review file is
  the next commit.
- Tests: `uv run pytest` -> 345 passed (327 at A1's head, plus 18 in `tests/test_m12_8k_events.py`).
  One existing expectation changed: `tests/test_daily_publish.py:303`, the dashboard dates
  sentence, now ends ", 8-K filed none.".
- Tests first: `85e8fe1` adds the 18 tests. With A1's `src`, 17 fail and 1 passes. `30f8ae6`
  makes them pass.
- Your A2 evidence is used: LINE.md "M12-A2 SEC evidence" and `m12-a2-sec-8k-items-sample.json`.
  Its live values are the fixtures: `12,7` legacy, `2.02,3.01,9.01`, the 2008 acceptance the
  evening before filing, `4.01,9.01`, `4.02` and `2.06,9.01`.

### What changed
- Migration 10, `filings_8k`:
  - Columns: accn, cik, form ('8-K' | '8-K/A'), filed, accepted, report_date, items (SEC's raw
    string, NULL when blank), primary_doc. PK (accn, cik).
  - Known-at date is `filed`. `accepted` is kept for reference only.
- `src/ingestion/events_8k.py` `load_8k_zip(con, path, sha256)`:
  - Reads the zip's main company files, skipping names with `-submissions-`. It keeps only
    companies that `companies` profiles (an SIC or a ticker, the same rule).
  - Takes `filings.recent`, then every paged file named in `filings.files` that is in the zip.
  - Inserts with `INSERT OR IGNORE` in batches of 200k, in one transaction.
  - Ledger stage `filings_8k`, key `submissions@<sha16>`. A replay of the same snapshot is
    skipped. A new snapshot inserts only new (accn, cik). Stored rows are never updated.
- `src/ingestion/reference.py`: `is_loaded` now needs both the companies and the 8-K ledger rows.
  So the first weekly run after deploy downloads the zip again even if profiles were loaded.
- `src/cli.py`: `ingest companies` (and so `ingest all`, weekly) calls `load_8k_zip` after the
  profiles.
- `src/db/queries.py`:
  - `EVENT_ITEMS` maps item to (signal, label, window days). The order of `signals` follows it:
    - 4.02 `8k_non_reliance`, 365 days;
    - 4.01 `8k_auditor_change`, 365;
    - 2.06 `8k_impairment`, 365;
    - 3.01 `8k_listing_notice`, 365;
    - 5.02 `8k_officer_change`, 90. The label is neutral, per your note that 5.02 also covers
      appointments.
  - `events_8k_sql()`: one row per (8-K, tracked item) with `filed <= $as_of` inside the item's
    window. Items are split on commas and trimmed, then matched as whole tokens. Untracked and
    legacy items fall through the CASE as NULL.
  - `last_results_8k_sql()`: the newest 2.02 per company.
- `src/screen.py` `informational_sql`: adds `ev` (items per CIK) and `res`, then appends the five
  8-K signals after the sell signals, plus `form8k_accns` and `last_results_8k`. It is still joined
  after ranking, so `metrics_sql` and `rank_sql` are untouched and C1's hash test still passes.
- Vault:
  - Company note section "8-K events": a table of filed date, form, labelled items and an EDGAR
    link, plus "Last results 8-K (Item 2.02)".
  - Dashboard dates sentence: "8-K filed <date>". `_data_dates` gains `form8k`.
- Docs: README (a known-at invariant line and a vault paragraph); ROADMAP (A2 verified items and
  status).

### How to verify
- `uv run pytest -q tests/test_m12_8k_events.py` -> 18 passed.
- Real SEC data in a scratch database (never `data/edgar.duckdb`):
  - Source: `submissions.zip` downloaded with `SecClient` at 20:52 MST, last-modified
    2026-11-25 05:00:19 GMT, sha256 `cf682e36…`, 1,578,012,344 bytes.
  - Script: `/private/tmp/claude-501/-Users-conor123-edgar-obsidian-quant/d54d33dd-2063-4a78-9433-c0c61c23e5a0/scratchpad/a2_real.py <zip> <scratch.duckdb>`.
    It loads with `connect()` on the scratch file.
  - Loads:

    | Step | Result | Time |
    |---|---|---|
    | companies | 75,308 | 37 s |
    | 8-K | 2,166,317 rows, 43,440 CIKs, filed 1993-10-29 to 2026-11-24 (8-K 2,080,178; 8-K/A 86,139) | 54 s |
    | Replay via the ledger | 0 | |
    | Replay with the ledger row deleted | 0 inserted | 51 s |

  - Items:
    - Before 2004-08-23: 360,119 rows, 41 NULL, the rest legacy integers.
    - From 2004-08-23: 1,806,198 rows, 3 NULL, and 40 still legacy-style (`5,7` 28, `5` 4,
      `12,7` 4, ...). Those never match.
  - Tracked item counts from 2004-08-23: 2.02 431,871; 2.06 3,960; 3.01 27,826; 4.01 30,720;
    4.02 9,426; 5.02 315,808.
  - Companies with each item inside its window at as_of 2026-11-20: 4.02 100; 4.01 473;
    2.06 49; 3.01 863; 5.02 1,824. The event query takes 0.01 s.
  - Shortlist names at 2026-11-20:
    - DINO: four 5.02 filings (10-28, 11-12, 11-16 8-K/A, 11-19).
    - NTWK: none. GIII: none.
    - LRN (fail): 5.02 on 10-09.
- Not run against the live DB or with a full screen. The 11-21 backup has no `filings_8k`, and
  status and rank are untouched by construction (C1 structure, plus
  `test_8ks_never_change_status_rank_or_shortlist`).

### Where bugs are most likely
- `is_loaded` now needs both ledgers. In `_fetch_zip`, a 304 with companies done but 8-Ks not done
  falls through to "never fully loaded; downloading again". That is one extra 1.58 GB download
  on the first weekly run after deploy.
- Memory: batches of 200k tuples. The whole load is one transaction of 2.17M rows (the real run
  used the default 2 GB limit). Check `INSERT OR IGNORE` against a large existing table on the
  weekly run.
- `_profiled` duplicates `load_companies_zip`'s keep rule (an SIC that is digits, or any ticker).
  If they drift, 8-Ks and profiles disagree.
- Window arithmetic: `filed > $as_of - days * INTERVAL 1 DAY` with `filed <= $as_of`, and the outer
  scan bounded by the longest window (365).
- 8-K/A counts as its own filing. A 4.02 amended twice is still one signal (a boolean per item).
- 3.01 fires for 863 companies at once, many of them minimum-bid notices. It is informational,
  but noisy.

### Known gaps (not findings unless they are worse than stated)
- Weekly refresh only, so an 8-K is in the screen up to about a week late. A nightly refresh is
  Phase B item 11.
- No `Sources/` vault note: SEC is the existing source, with the same fair-access rules.
- No backtest variant uses 8-K signals.
- The CLI `screen` printout does not show signals.

## Findings (Codex)

Reviewed `be79092ed7460b9adf0a08dd677fd710cb81e635` against the A2 stack base and the retargeted main. I checked filing-date gating, whole-token matching, and the post-ranking joins. No live DB access.

### C1. A missing declared archive page is silently recorded as a complete load
- Severity: medium
- Where: `src/ingestion/events_8k.py:103–108`, `src/ingestion/reference.py:66–71`
- Evidence: the loader skips any `filings.files` member absent from the zip, then marks the entire snapshot done. The combined ledger can subsequently accept an HTTP 304 and skip recovery. I built a one-company zip declaring an archival page with filingCount 1 but omitting its member. The load returned 0 and `is_loaded` returned True, with no indication of incomplete coverage.
- Test: a profiled CIK declares a nonempty archive member that is absent. Assert the omitted page is surfaced and that the snapshot is not reported as fully loaded. If the SEC bulk source intentionally omits declared pages, document how recovery or explicit partial coverage works and test that behavior.

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
