# Review: M5 Backtest harness
Status: awaiting-review

## Request (Claude)

- Branch: `feat/m5-backtest`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/11
- Diff: `git diff b44f67f..0578ff5` (code; the commit after 0578ff5 adds only this file)
- Tests: `uv run pytest` -> 91 passed

### What changed
- `src/backtest.py` (new): month ends; `snapshots()` caches `metrics_sql` per date as Parquet with
  a fingerprint (counts of data known by the date + SQL text); `returns_sql()` forward 1/3/6/12m
  total returns via ASOF joins on `prices` and cumulative split-adjusted `dividends`;
  `panel_sql()` keeps closes >= $1; `evaluate()` excess vs equal-weight universe, hit rate, costs,
  one-month strategy curve (CAGR, drawdown, turnover with cash), halves flag;
  `survivorship_gap()`; `render_report()` / `write_report()` (reports/ + vault via `upsert_note`);
  `run()` with the leak check.
- `src/screen.py`: `screen_sql` split into `metrics_sql` (inputs, binds $as_of) and `rank_sql`
  (filters, status, rank per as_of over any table). `Filters` fields can be None/False (off).
  `min_drawdown` now defaults to None (decision from the results).
- `src/db/pit.py`: policy `leaky_period_end` (facts visible from period_end, first-reported order).
- `src/db/queries.py`: issues `penny_price` (close < $1) and `price_break` (adjusted close up 10x
  or more in one day within 365 days); `cluster_buys` requires `txn_date <= filed`, reads
  `form4_effective($as_of, p_since := $as_of - window)`, rounds `usd_bought` to cents.
- `src/db/schema.py`: `form4_effective(p_as_of, p_since := DATE '0001-01-01')`.
- `src/ingestion/form4_sources.py`: data sets without `AFF10B5ONE` (before 2023) load with NULL.
- `src/cli.py`: `backtest` command; screen prints "no drawdown rule" when off.
- Tests: `tests/test_m5_backtest.py` (new, 11), additions in `tests/test_m4_screen.py` and
  `tests/test_form4_sources.py`.

### How to verify
- `uv run pytest` -> 91 passed.
- Report: `reports/backtest-2026-11-04.md`. Universe CAGR 9.3%; default variant 3m excess +4.1%,
  12m +15.2%; leak check value variant real +0.1% vs leaky +1.9%.
- Optional, only if no other job holds the DB: `uv run python -m src.cli backtest --start 2012-01-31`
  takes about 1 minute from the cache (snapshots in `data/backtest/`). Do not delete the cache:
  rebuilding it takes 44 minutes.
- `uv run python -m src.cli screen --as-of 2026-11-03` -> 3 shortlisted (DINO, GIII, LRN).

### Where bugs are most likely
- Look-ahead in the backtest: anything in `metrics_sql` or `returns_sql` that reads data after
  the rebalance date (the `companies` table and tickers are today's snapshot, known; anything
  else is a finding). `split_factor` uses all splits, intended to cancel in ratios.
- `returns_sql`: the dividend window (ex_date after entry, on or before exit), ASOF join edge
  cases, `target <= last price date` as "known", the `price_break` window (entry, exit].
- `evaluate`: equal weights, hit rate definition (pick return after costs > universe mean),
  turnover with cash, CAGR on months with a known 1m universe return, halves split at the
  median date with picks.
- `_fingerprint` missing an input, so a stale snapshot is reused.
- `form4_effective` with `p_since`: the claim that it is exact for `cluster_buys` (no trade is
  reported before it happens; amendments are filed after what they replace).
- `rank_sql` with filters switched off: NULL handling in `pass_*` and status.

### Known gaps (not findings unless they are worse than stated)
- Survivorship: 57% of active non-financial filers have no price on an average date.
- About 180 picks over 15 years for the default variant: results are a hint, not proof.
- SIC codes and tickers are current snapshots, not as of each date.
- Insider data before 2023 has no 10b5-1 checkbox; only plan footnotes exclude plan trades.
- The leak check passes on the value variant only; for the default variant leaky scored worse.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
