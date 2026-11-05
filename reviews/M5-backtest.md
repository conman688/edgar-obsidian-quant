# Review: M5 Backtest harness
Status: awaiting-response

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

### C1. A corrected input does not invalidate cached snapshots
- Severity: high
- Where: src/backtest.py:65-75
- Evidence: The fingerprint uses row counts, so changing an existing price, fact, split ratio, classification, or ticker can leave it unchanged. Reproduced: after the first snapshot, changing ACME's 2024-08-30 close from 12 to 20 made the next snapshots call return 0; it continued to use the old price and selection metrics. Introduce a revision/fingerprint covering updates to all inputs without counting distinct whole fact rows.
- Test:
```python
def test_snapshot_invalidates_after_price_correction(con, tmp_path):
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4")
    dates = [date(2024, 8, 31)]
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1
    con.execute("UPDATE prices SET close_raw = 20 WHERE cik = ? AND price_date = DATE '2024-08-30'", [CIK])
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1
```

### C2. A one-month position can end before the next monthly rebalance
- Severity: high
- Where: src/backtest.py:121-129
- Evidence: Adding one calendar month to April 30 yields May 30, but the next month-end snapshot is May 31. With prices 100 on April 30 and May 30, and 200 on May 31, ret_1m is 0% instead of the monthly strategy's 100%, leaving a day's return out of its compounded curve. Align the monthly holding-period exit with the next rebalance's effective trading date.
- Test:
```python
def test_monthly_return_reaches_next_month_end(con):
    px(con, CIK, [("2024-04-30", 100), ("2024-05-30", 100), ("2024-05-31", 200)])
    con.execute("CREATE TABLE snap AS SELECT ?::INTEGER cik, DATE '2024-04-30' as_of, DATE '2024-04-30' price_date, 100.0 close_raw", [CIK])
    rel = con.sql(bt.returns_sql("snap"))
    row = dict(zip(rel.columns, rel.fetchall()[0]))
    assert row["ret_1m"] == pytest.approx(1.0)
```

### C3. Cash-to-stock trades are charged twice the one-way cost
- Severity: medium
- Where: src/backtest.py:219-225
- Evidence: The absolute weight changes count both the cash decrease and stock increase as trades. With a 50 bps one-way fee, entering one unchanged-price stock from cash costs 1% instead of 0.5%; reproduced one-month CAGR is (0.99 ** 12 - 1), not (0.995 ** 12 - 1). Exclude the cash leg from transaction costs while keeping the desired turnover convention explicit.
- Test:
```python
def test_entry_from_cash_pays_one_way_cost(con):
    panel(con, [(date(2024, 1, 31), 1, "shortlist", 0.0, 0.0)])
    result = bt.evaluate(con, "ranked", cost_bps=50)
    assert result["monthly"]["cagr"] == pytest.approx(0.995 ** 12 - 1)
```

### C4. Turnover ignores weight drift between rebalances
- Severity: medium
- Where: src/backtest.py:221-229
- Evidence: prev stores target weights, not end-of-month weights after each holding's return. Two equally weighted stocks returning 100% and 0% become 2/3 and 1/3; restoring 1/2 each trades 1/6 of portfolio value, yet the second month reports zero turnover and cost. Carry per-asset end weights through the rebalance.
- Test:
```python
def test_rebalancing_same_picks_after_returns_has_turnover(con):
    panel(con, [
        (date(2024, 1, 31), 1, "shortlist", 1.0, 0.0),
        (date(2024, 1, 31), 2, "shortlist", 0.0, 0.0),
        (date(2024, 2, 29), 1, "shortlist", 0.0, 0.0),
        (date(2024, 2, 29), 2, "shortlist", 0.0, 0.0),
    ])
    result = bt.evaluate(con, "ranked", cost_bps=0)
    assert result["monthly"]["turnover"] == pytest.approx((1 + 1 / 6) / 2)
```

Verification: existing tests/test_m5_backtest.py: 11 passed. C1-C3 reproduced with an isolated synthetic script; C4 reproduced by running evaluate on the stated two-month panel (actual average turnover 0.5). Live database was not opened.


## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
