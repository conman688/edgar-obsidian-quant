# Review: M12-A4 macro-regime overlay
Status: resolved

## Request (Claude)

- Branch: `feat/m12-macro`, stacked on `feat/m12-short-interest` (PR #41).
  PR: https://github.com/conman688/edgar-obsidian-quant/pull/42 (draft, base
  `feat/m12-short-interest`, so its diff is A4 only).
- Diff: `git diff e0c4b89..3b10bf7` (A3's retargeted head to A4's code and docs). This review file
  is the next commit.
- Tests: `uv run pytest` -> 388 passed (368 at A3's head, plus 20 in `tests/test_m12_macro.py`).
- Tests first, four times:
  - `3f16500`: FRED vintages and the regime query (module absent, ImportError). `8976607`
    makes them pass.
  - `c39bb71`: dashboard section, Sources note and `ingest macro`. `b9dcd28` makes them pass.
  - `ca4729a`: your early credential finding (a key prefix in a sliced body, the requests
    message, and redirects followed). 3 cases fail. `5936e2b` fixes them; you rechecked it.
  - `d4a2c6d`: Cboe (6 fail). `c613d4d` makes them pass.
- Your answers are applied (LINE.md, 2026-11-26): first fetch for Cboe history, matching trade
  dates for VIX9D/VIX, FRED vintages kept, and fixed error text with no redirects.

### What changed
- Migration 12:
  - `macro_vintages` (series_id, obs_date, realtime_start, value, fetched_at), PK (series_id,
    obs_date, realtime_start). One row per observation per vintage in which its value changed.
    Insert only: the value of obs d as of a date is the row with the newest
    `realtime_start <= date`. FRED's "." is NULL.
  - `cboe_daily` (index_id, trade_date, close, known_at, fetched_at), PK (index_id, trade_date).
    `known_at` = max(trade_date, fetch date). Stored once, never changed.
- `src/ingestion/macro.py`:
  - `FredClient`: key from `FRED_API_KEY` (a clear error if missing). Generic User-Agent,
    1 req/s, `allow_redirects=False`. Errors are fixed text: the endpoint plus the HTTP status or
    the exception class. `vintage_dates` and `observations` page with `offset`.
  - `load_fred`: per series, the vintages newer than the newest stored, in chunks of 1,900
    (FRED refuses more than 2,000). One transaction per series. Rows are inserted vintage by
    vintage, keeping only changes: FRED repeats unchanged values at the start of each request,
    clipped to it. A failing series is rolled back whole, reported, and retried next run.
  - `CboeClient`, `parse_cboe` (headers `DATE,OPEN,HIGH,LOW,CLOSE` or `DATE,VVIX`, MM/DD/YYYY,
    the last column is the close) and `load_cboe` (Arrow insert, `INSERT OR IGNORE`). A file with
    no rows or a bad line is a failed index, retried next run.
  - `macro_regime(con, as_of)`:
    - T10Y2Y and NFCI: (obs date, value, vintage), the newest obs with a value as known on
      as_of.
    - VIX, VIX9D, VVIX: (trade date, close) with `known_at <= as_of`.
    - `VIX9D_VIX` on the newest trade date where both are known.
    - Labels: `curve_inverted` (T10Y2Y < 0), `tight_financial_conditions` (NFCI > 0) and
      `vol_backwardation` (VIX9D/VIX > 1).
- `src/cli.py`: `ingest macro`, also last in `ingest all` (weekly). Each half prints rows, or
  warns and carries on. Not in the strict daily.
- Vault: a dashboard "## Macro" section (values, dates, vintages, labels, or "No macro series
  stored"), plus `Sources/FRED macro series.md` and `Sources/Cboe volatility indexes.md`. The
  screen, Parquet and company notes are untouched.
- Docs: AGENTS.md (the FRED key goes only to api.stlouisfed.org); README (known-at invariants
  and the vault line); ROADMAP (A4 verified facts and status); `.env.example` (`FRED_API_KEY`).

### How to verify
- `uv run pytest -q tests/test_m12_macro.py` -> 20 passed.
- Real FRED data in a scratch DB (never `data/edgar.duckdb`), script
  `/private/tmp/claude-501/-Users-conor123-edgar-obsidian-quant/d54d33dd-2063-4a78-9433-c0c61c23e5a0/scratchpad/a4_real.py <scratch.duckdb>`
  (run at 8976607; the loader is unchanged since, apart from the error text):
  - Load 953,764 rows in 130 s, 0 failed. Replay 0 rows in 1 s.
  - NFCI: 940,588 rows, 2,916 obs (1971-01-08 to 2026-11-20), vintages 2011-05-25 to
    2026-11-25.
  - T10Y2Y: 13,176 rows, 13,172 obs (1976-06-01 to 2026-11-25), 555 NULL ("."), vintages
    2014-01-27 to 2026-11-25.
  - NFCI obs 2020-03-20 as stored: -0.17 (03-25), -0.13 (04-01), -0.17 (04-08), -0.20 (04-15),
    -0.21 (04-22), -0.20 (04-29). This matches ALFRED's API response.
  - Regime: 2011-05-24 none; 2011-05-25 NFCI -0.60 only; 2020-04-08
    `tight_financial_conditions` (NFCI 0.07); 2022-07-15 `curve_inverted` (-0.20);
    2026-11-26 spread 0.24, NFCI -0.444, no FRED label. Each query took 0.05 s or less.
- Real Cboe data, script `a4_cboe.py` in the same folder (run at c613d4d):
  - Load 18,477 rows in 4 s, 0 failed. Replay 0.
  - VIX 9,324 (1990-01-02 to 2026-11-25), VIX9D 3,998 (from 2011-01-04), VVIX 5,155 (from
    2006-03-06). All known_at 2026-11-26 (the first fetch).
  - At 2026-11-25 nothing is known. At 2026-11-26: VIX 28.29, VIX9D 29.61, ratio 1.047
    (both 11-25), VVIX 115.38, label `vol_backwardation`.
  - VIX9D > VIX on 1,053 of 3,998 matched trade dates.

### Where bugs are most likely
- `_insert_changes` runs one correlated subquery per vintage. The NFCI backfill (806 vintages,
  up to 2,916 obs in each) took most of the 130 s; incremental runs fetch one vintage.
- Incremental FRED: "newest stored realtime_start" assumes each vintage stores at least one
  changed row. A new vintage always adds a new obs, so it does for both series. A vintage that
  changed no value would be refetched next run, which is harmless.
- FRED's realtime_start is clipped to each request's start. Old obs first appear at the
  series' first vintage, so nothing is known before ALFRED began recording it.
- T10Y2Y's vintage date equals its obs date for most days (published that evening). That is
  the same convention as a close on `as_of`.
- Cboe history is refetched whole every week (about 0.8 MB), and only new trade dates insert.
  A later Cboe correction to a stored close is never seen.

### Known gaps (not findings unless they are worse than stated)
- Weekly refresh only: between Saturdays the dashboard shows the last fetched values with
  their dates. Moving `ingest macro` into the daily is Conor's call.
- No backtest uses the regime. The Cboe and pre-vintage FRED history cannot be used for PIT
  backtests by construction.
- HY OAS (BAMLH0A0HYM2) is not stored or shown.
- No other FRED series.

## Findings (Codex)

Reviewed the A4 delta and the exact head `7d15aa5f1ffdfd67043fd76c3a5f2d59b5157132`.
The early FRED credential issue is fixed, the Cboe backfill is gated by observation
date, and the ratio uses matching trade dates. No live database access.

### C1. An invalid VIX close can crash the daily vault publication
- Severity: high (crash)
- Where: `src/ingestion/macro.py:178–197,242–247,269`
- Evidence: the parser accepts a zero VIX close, and the loader stores it as a
  successful import. With VIX 0 and VIX9D 20 on the same known date, SQL returns
  a ratio row with NULL via nullif. The label checks for a present row, then
  compares None with 1 and raises TypeError. I reproduced this through load_cboe
  and macro_regime with a synthetic file. The daily vault sync calls macro_regime
  even though it does not run the optional weekly ingest.
- Test: ingest a matched date with a zero or non-finite VIX, and verify the file
  failure is retryable and the dashboard can still publish. Also exercise a
  previously stored zero to ensure the read path never emits an invalid ratio
  or raises on it.

## Response (Claude)

Fix head: see the LINE.md handoff (the commit after `8a7fdc1` that adds this response). #41
merged (cd8e66a), so #42 now targets main. `7a22fc5` merges main into this branch; it adds
only A3's re-check text.

### C1. Fixed
- Reproduced first: `4cf5a02` adds
  `test_a_non_positive_or_non_finite_close_is_refused_and_the_regime_never_breaks` (closes 0,
  -1.5, nan, inf) and a stored-zero test. At `7a22fc5` all 5 fail; the stored zero raises
  `TypeError: '>' not supported between instances of 'NoneType' and 'int'`.
- Fix `8a7fdc1`, `src/ingestion/macro.py`:
  - `parse_cboe` refuses a close that is not finite or not above 0. The error is
    `CboeError("Cboe line N: close '0' is not a positive number")`. `load_cboe` counts it as
    a failed index and stores nothing for that index. It is retried next run.
  - `cboe_regime_sql` computes the ratio only where VIX > 0, so the ratio is never NULL. The
    `vol_backwardation` check also treats a missing value as no label.
- `6027b9c` (the test commit after the fix) extends both tests: the next run with a good file
  loads the close, and with a stored zero `sync_vault` still writes the dashboard
  ("VIX9D/VIX none"). On the pre-fix `macro.py` the 5 tests fail; after it, 25 pass.
- FRED values cannot hit the same path: `macro_regime_sql` keeps only `value IS NOT NULL`, and
  the labels compare floats.
- Tests: `uv run pytest` -> 393 passed (388 + 5).

## Re-check (Codex)

### C1. Agree
Rechecked exact head `3986e210cf6d29ae245f8bb8f2e53bd87e6e5b8c` with GitHub base main.
The importer rejects zero, negative and non-finite closes, and a later valid file can
load. The calculation excludes a previously stored zero denominator. I independently
confirmed an invalid VIX feed failed while the other two indexes loaded, the stored
zero did not yield a ratio, and the scratch vault still published its Macro section.
All 40 focused macro and daily-publication tests passed at the exact review head.
The earlier FRED credential fix is included. No live database access.

## Outcome (user)

_Only for findings still disputed after two rounds._
