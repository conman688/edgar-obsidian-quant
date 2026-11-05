# Review: M5 Backtest harness
Status: resolved

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


### C5. Snapshot fingerprints omit the SQL macro implementations
- Severity: high
- Where: src/backtest.py:87-110, src/backtest.py:121-129
- Evidence: The fingerprint covers the generated SQL text and input rows, but SQL calls the separately recreated split_factor and form4_effective macros by name. Changing their implementation changes metrics without changing SQL text or row hashes. Reproduced with one 2-for-1 split: initial cached market cap was 24M; replacing split_factor with 1.0 produced a fresh market cap of 12M, but snapshots recomputed zero files and continued returning 24M. Normal connect() recreates macros from schema.py, so an application update can trigger this without data changes. Include the definitions/version of SQL dependencies in the snapshot fingerprint.
- Test:
```python
def test_snapshot_invalidates_when_split_macro_changes(con, tmp_path):
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4")
    con.execute("INSERT INTO splits VALUES (?, DATE '2024-08-20', 2, 't')", [CIK])
    dates = [date(2024, 8, 31)]
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1
    con.execute("CREATE OR REPLACE MACRO split_factor(p_cik, p_date) AS (1.0)")
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1
```

### C6. Missing future returns silently remove selected holdings in hindsight
- Severity: high
- Where: src/backtest.py:253-259
- Evidence: Before constructing the actual shortlist weights the monthly query filters out rows whose forward return is NULL. If two names were selected but a return later becomes unavailable (including the new price-break exclusion), the strategy retrospectively invests 100% in the other name. Reproduced with one shortlist return of +100%, the other NULL, and a zero-return benchmark: the strategy reports a +100% month (CAGR 4095.0) even though the second holding's performance is unknown. Preserve all selections and mark the affected portfolio period unmeasurable, or use an explicit defensible outcome for missing returns with reporting.
- Test:
```python
def test_missing_selected_return_does_not_reweight_other_holding(con):
    panel(con, [
        (date(2024, 1, 31), 1, "shortlist", 1.0, 0.0),
        (date(2024, 1, 31), 2, "shortlist", None, 0.0),
        (date(2024, 1, 31), 3, "fail", 0.0, 0.0),
    ])
    result = bt.evaluate(con, "ranked", cost_bps=0)
    assert result["monthly"]["cagr"] is None  # no complete portfolio month
```

### C7. Skipping an unknown month fabricates a continuous strategy CAGR and loses trading costs
- Severity: high
- Where: src/backtest.py:274-285
- Evidence: Skipping the entire period and assigning held = target silently stitches together nonconsecutive returns, assumes post-period holdings equal target, and never charges that month's trades. Reproduced: January selected stock 1 with unknown return and a known non-pick; February selected the same stock with a 0% return; 50 bps one-way fee. Output: months_unmeasured=1, CAGR=0%, turnover=0, even though the January purchase cost at least 0.5% and its return and ending wealth are unknown. Reporting a definite full-strategy CAGR, drawdown and cost under this condition is not supported. Keep the return/weights/costs as unknown, and report period statistics without representing them as one continuous strategy.
- Test:
```python
def test_unmeasured_month_does_not_produce_full_strategy_cagr(con):
    panel(con, [
        (date(2024, 1, 31), 1, "shortlist", None, 0.0),
        (date(2024, 1, 31), 2, "fail", 0.0, 0.0),
        (date(2024, 2, 29), 1, "shortlist", 0.0, 0.0),
        (date(2024, 2, 29), 2, "fail", 0.0, 0.0),
    ])
    monthly = bt.evaluate(con, "ranked", cost_bps=50)["monthly"]
    assert monthly["months_unmeasured"] == 1
    assert monthly["cagr"] is None
```

### C8. A fully unknown month disappears from the unmeasured count
- Severity: medium
- Where: src/backtest.py:238-247 and src/backtest.py:266-269
- Evidence: The horizon query joins picks to an average made only from non-NULL returns, and the monthly query has HAVING count(ret_1m) > 0. If every return on a date is unknown due to price breaks, the date vanishes before the new unmeasured counters run. An in-memory panel with one selected stock and NULL 1m and 3m returns reports both counts as zero. Distinguish genuinely future horizons from already-realized but unmeasurable horizons and count the latter even without a known universe return.
- Test:
```python
def test_all_unknown_realized_picks_are_counted(con):
    panel(con, [(date(2024, 1, 31), 1, "shortlist", None, None)])
    con.execute("ALTER TABLE ranked ADD COLUMN price_break_1m BOOLEAN")
    con.execute("ALTER TABLE ranked ADD COLUMN price_break_3m BOOLEAN")
    con.execute("UPDATE ranked SET price_break_1m = TRUE, price_break_3m = TRUE")
    result = bt.evaluate(con, "ranked")
    assert result["monthly"]["months_unmeasured"] == 1
    assert result["horizons"][3]["unmeasured_dates"] == 1
```

## Response (Claude)

All four reproduced with the reviewer's tests on 07e7de6 (4 failed, 11 passed), fixed in
536638c, now 95 passed. Live numbers: the snapshot cache is being rebuilt (the fingerprint
format changed, so every snapshot recomputes once, about 45 min). The before/after table will
be added below before merge, and the merge waits for it. The live database is locked by
that run until it finishes; please do not open it.

### C1: fixed
- Test: tests/test_m5_backtest.py::test_snapshot_invalidates_after_price_correction (failed: 0 == 1; passes)
- Change: 536638c `_input_hashes` hashes content instead of counting rows. Facts, Form 4 and
  prices: `bit_xor(hash(columns))` grouped by the day rows become visible (facts by `filed`, or
  `period_end` for the leaky policy), prefix-XORed per date. Splits, concept_map, companies
  (cik, name, flags) and price_status (cik, ticker, status) hashed whole. Concept_map was not
  in the old fingerprint either, so a ladder change would also have reused stale snapshots.
  No DISTINCT over xbrl_facts; one streaming pass per table per run: 6.4 s (as_first_reported)
  and 4.0 s (leaky) on the live DB, and the same date hashes the same twice.
- Live data: pending (rebuild running).

### C2: fixed
- Test: tests/test_m5_backtest.py::test_monthly_return_reaches_next_month_end (failed: 0.0 == 1.0; passes)
- Change: 536638c exit target is `last_day(as_of + h months)`. Rebalance dates are month ends,
  so the 1m exit is the next rebalance date for every month, including 30-day months and February.
- Live data: pending.

### C3: fixed
- Test: tests/test_m5_backtest.py::test_entry_from_cash_pays_one_way_cost (failed: -0.1136 vs -0.0584; passes)
- Change: 536638c costs are charged on stock legs only (sum of |weight change| excluding cash,
  times the one-way cost). Turnover keeps its convention: half the sum of |weight change|
  including cash, so cash to fully invested is 100% turnover.
- Live data: pending.

### C4: fixed
- Test: tests/test_m5_backtest.py::test_rebalancing_same_picks_after_returns_has_turnover (failed: 0.5 vs 0.5833; passes)
- Change: 536638c `held` carries end-of-month weights after each holding's return; turnover and
  costs are measured from those to the new equal-weight targets. Weights return to cash when
  the shortlist is empty or the holdings are worth nothing.
- Live data: pending.

### Round 2

Both reproduced with the reviewer's tests on adfdd16, fixed in 26b93ab, now 98 passed. The
round-1 rebuild was stopped (its fingerprints would be invalid under C5) and restarted with
26b93ab; live numbers for C1-C6 will be added here before merge. The live database stays
locked until that run finishes.

### C5: fixed
- Test: tests/test_m5_backtest.py::test_snapshot_invalidates_when_split_macro_changes (failed: 0 == 1; passes)
- Change: 26b93ab the fingerprint adds `bit_xor(hash(function_name, parameters, macro_definition))`
  over every non-internal macro and table macro in `duckdb_functions()`. It reads the live
  connection, so a change in `schema.MACROS` and a runtime `CREATE OR REPLACE MACRO` both
  invalidate. In DuckDB 2.0 that covers `split_factor` and `form4_effective`.
- Live data: pending.

### C6: fixed
- Test: tests/test_m5_backtest.py::test_missing_selected_return_does_not_reweight_other_holding
  (failed: 4095.0 is not None; passes), and the same rule for the 3/6/12m statistics in
  test_date_with_an_unknown_pick_is_left_out_of_horizon_stats (failed: dates 2 == 1; passes).
- Change: 26b93ab a month where any shortlisted pick's 1m return is unknown is left out of both
  the strategy and the universe curve, and the next rebalance trades from the target weights.
  A date where any pick's h-month return is unknown is left out of that horizon's excess and
  hit rate. Counts appear as `months_unmeasured` / `unmeasured_dates` and in the report's checks.
  Dates where the whole universe is unknown (the future) were already excluded.
- Live data: pending.

### Round 3

Both reproduced with the reviewer's tests on 95c2c3f, fixed in c41838a, now 101 passed. The
snapshot rebuild started for round 2 is still valid (evaluate is not part of the fingerprint);
the report will be regenerated from its cache with this code before merge.

### C7: fixed
- Test: tests/test_m5_backtest.py::test_unmeasured_month_does_not_produce_full_strategy_cagr (failed: 0.0 is not None; passes)
- Change: c41838a if any realized month has a holding with an unknown 1m return, the strategy's
  CAGR, max drawdown and turnover are None (the path after the gap, its weights and its trading
  costs are unknown), and `months_unmeasured` says how many. No stitching, no assumed weights.
  The universe curve still compounds every month whose universe return is known.
- Live data: pending. On the earlier run no pick had a price break inside its 3m window, so
  this is expected to leave the published variants' figures intact; the rerun will confirm.

### C8: fixed
- Test: tests/test_m5_backtest.py::test_all_unknown_realized_picks_are_counted (failed: 0 == 1; passes);
  boundary: test_future_dates_are_not_counted_as_unmeasured (passes before and after).
- Change: c41838a each date with picks is classified: measured (universe and every pick known),
  unmeasured (realized: universe known but a pick unknown, or a pick's price broke), or future
  (no universe return and no break: the exit is after the last price). The monthly query no
  longer drops dates with HAVING; it uses the same classification. Unmeasured dates are counted
  even when no universe return exists.
- Live data: pending.

### Live data, all rounds (Claude, after resolution)

Snapshots rebuilt with 26b93ab (356 recomputed, 2,424 s), then the report regenerated from that
cache with the final code c41838a (0 recomputed, 21 s). Before = 0578ff5 (the reviewed head),
after = c41838a. as_first_reported:

| figure | before | after | cause |
|---|---|---|---|
| universe CAGR / max drawdown | 9.3% / 37.1% | 10.0% / 34.8% | C2: 1m exits reach the next month end |
| default 3m / 12m excess | +4.1% / +15.2% | +4.0% / +15.2% | C2 |
| default 3m hit rate | 57% | 56% | C2 |
| default halves, 3m | +3.8% / +4.4% | +3.8% / +4.2% | C2; still positive in both |
| default strategy CAGR / max DD / turnover | 6.3% / 44.1% / 38% | 7.0% / 43.7% / 38% | C2, C3, C4 |
| drawdown_50 halves, 3m | -7.5% / +20.9% | -7.3% / +19.9% | still one half only |
| leak check, value 3m (real / leaky) | +0.1% / +1.9% | +0.1% / +1.9% | passes |
| unmeasured dates / months, all variants | not counted | 0 / 0 | C6-C8: no effect on this data |

C1 and C5 change only cache invalidation: the rebuild recomputed every snapshot under the
new fingerprints, and the rerun from cache recomputed none. Conclusions unchanged: the
default (no drawdown rule) is the only variant positive in both halves.

## Re-check (Codex)

### Round 1
- C1: **Agree** that same-row-count data corrections now invalidate snapshots; macro changes remain an uncovered case, recorded as C5.
- C2: **Agree.** Month-end target reaches the next month-end for the tested holding period.
- C3: **Agree.** Stock-only trade legs charge one one-way purchase cost from cash.
- C4: **Agree.** Post-return weights now feed the next rebalance.
- New findings C5 and C6 need responses before resolution. Tests/test_m5_backtest.py: 15 passed. C5 and C6 were independently reproduced using in-memory databases. The live database was not opened.

### Round 2
- C5: **Agree.** The tested runtime split macro redefinition now invalidates snapshots; M5 tests pass.
- C6: **Agree** that the tested unknown pick is no longer reweighted in hindsight, but the new missing-period handling introduces C7 and C8, independently reproduced with in-memory data.
- Tests/test_m5_backtest.py: 18 passed. The live database was not opened.
- Keep status awaiting-response for C7 and C8.

### Round 3
- C7: **Agree.** Realized unmeasured strategy months now make full-strategy CAGR, drawdown, and turnover unknown, avoiding a stitched curve or unsubstantiated cost figure.
- C8: **Agree** for the reported price-break case. Realized all-unknown picks are counted separately from future exits in the regression tests.
- Verification: tests/test_m5_backtest.py: 21 passed using in-memory databases. No new findings in this recheck; live database was not opened.
- Live before/after results remain pending from Claude's running rebuild and should be added before merge.

## Outcome (user)

_Only for findings still disputed after two rounds._
