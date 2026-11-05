"""M5 backtest: forward returns, portfolio statistics, leak detection, snapshot cache, report."""
from datetime import date

import pytest

from src import backtest as bt
from src.db.pit import concept
from src.db.schema import connect
from src.ingestion.companyfacts import load_docs
from tests.test_failure_modes import CIK, doc, fact
from tests.test_m4_screen import add_buy, add_company, cheap_company


@pytest.fixture
def con():
    return connect(":memory:")


def px(con, cik, rows):
    con.executemany("INSERT INTO prices (cik, ticker, price_date, close_raw, source) VALUES (?, 'X', ?, ?, 't')",
                    [(cik, date.fromisoformat(d), v) for d, v in rows])


def test_month_ends():
    assert bt.month_ends(date(2024, 1, 15), date(2024, 4, 30)) == \
        [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31), date(2024, 4, 30)]


def test_forward_returns_adjust_for_splits_and_dividends(con):
    px(con, CIK, [("2024-01-31", 100.0), ("2024-02-29", 52.0), ("2024-04-30", 55.0)])
    con.execute("INSERT INTO splits VALUES (?, DATE '2024-02-15', 2.0, 't')", [CIK])
    con.execute("INSERT INTO dividends VALUES (?, DATE '2024-02-10', 1.0, 't')", [CIK])   # pre-split basis
    con.execute("CREATE TABLE snap AS SELECT ?::INTEGER AS cik, DATE '2024-01-31' AS as_of, "
                "DATE '2024-01-31' AS price_date, 100.0 AS close_raw", [CIK])
    r = dict(zip(*[[c for c in con.sql(bt.returns_sql("snap")).columns],
                   con.sql(bt.returns_sql("snap")).fetchall()[0]]))
    assert r["ret_1m"] == pytest.approx((52 + 0.5) / 50 - 1)    # 100 pre-split = 50; $1 dividend = $0.50
    assert r["ret_3m"] == pytest.approx((55 + 0.5) / 50 - 1)
    assert r["ret_6m"] is None                                  # July is after the last price: unknown
    assert r["exit_gap_3m"] is False


def test_stock_that_stops_trading_exits_at_last_price_and_is_counted(con):
    px(con, CIK, [("2024-01-31", 10.0), ("2024-02-29", 8.0)])
    px(con, 99, [("2024-01-31", 1.0), ("2024-06-28", 1.0)])     # market goes on
    con.execute("CREATE TABLE snap AS SELECT ?::INTEGER AS cik, DATE '2024-01-31' AS as_of, "
                "DATE '2024-01-31' AS price_date, 10.0 AS close_raw", [CIK])
    rel = con.sql(bt.returns_sql("snap"))
    r = dict(zip(rel.columns, rel.fetchall()[0]))
    assert r["ret_3m"] == pytest.approx(-0.2) and r["exit_gap_3m"] is True


def panel(con, rows):
    con.execute("CREATE OR REPLACE TABLE ranked (as_of DATE, cik INTEGER, status VARCHAR, "
                "ret_1m DOUBLE, ret_3m DOUBLE, ret_6m DOUBLE, ret_12m DOUBLE, exit_gap_3m BOOLEAN)")
    con.executemany("INSERT INTO ranked VALUES (?, ?, ?, ?, ?, NULL, NULL, FALSE)", rows)


def test_portfolio_statistics(con):
    d1, d2, d3 = date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)
    panel(con, [
        (d1, 1, "shortlist", 0.10, 0.20), (d1, 2, "fail", 0.00, 0.00), (d1, 3, "fail", -0.10, -0.20),
        (d2, 1, "shortlist", -0.20, 0.05), (d2, 2, "shortlist", 0.00, 0.10), (d2, 3, "fail", 0.20, 0.00),
        (d3, 1, "fail", 0.05, None), (d3, 2, "fail", 0.05, None),
    ])
    s = bt.evaluate(con, "ranked", cost_bps=0)
    h3 = s["horizons"][3]
    # d1: picks 0.20 vs universe 0.00; d2: picks 0.075 vs universe 0.05; d3 has no 3m returns.
    assert h3["dates"] == 2 and h3["picks"] == 3
    assert h3["excess"] == pytest.approx(((0.20 - 0.0) + (0.075 - 0.05)) / 2)
    assert h3["hit_rate"] == pytest.approx(2 / 3)                # 0.20 > 0, 0.05 = 0.05 is no hit, 0.10 > 0.05
    m = s["monthly"]
    # Strategy: +10%, then -10% (avg of -20%, 0%), then cash 0%. Universe: 0%, 0%, +5%.
    assert m["months"] == 3
    assert m["max_drawdown"] == pytest.approx(1 - 1.1 * 0.9 / 1.1)
    # Turnover: cash -> {1} = 1.0, {1} -> {1,2} = 0.5, {1,2} -> cash = 1.0.
    assert m["turnover"] == pytest.approx((1.0 + 0.5 + 1.0) / 3)


def test_costs_reduce_returns(con):
    d1 = date(2024, 1, 31)
    panel(con, [(d1, 1, "shortlist", 0.10, 0.20), (d1, 2, "fail", 0.0, 0.0)])
    free, costly = bt.evaluate(con, "ranked", cost_bps=0), bt.evaluate(con, "ranked", cost_bps=50)
    assert costly["horizons"][3]["excess"] == pytest.approx(free["horizons"][3]["excess"] - 0.01)


def test_screen_that_works_in_one_half_only_is_flagged(con):
    rows = []
    for i, d in enumerate(bt.month_ends(date(2020, 1, 1), date(2020, 8, 31))):
        win = 0.10 if i < 4 else -0.10
        rows += [(d, 1, "shortlist", win, win), (d, 2, "fail", 0.0, 0.0)]
    panel(con, rows)
    s = bt.evaluate(con, "ranked", cost_bps=0)
    assert s["halves"][3]["one_half_only"] is True


def test_leaky_policy_sees_numbers_before_they_are_filed(con):
    load_docs(con, [doc({("us-gaap", "Revenues", "USD"): [
        fact("2023-01-01", "2023-12-31", 100.0, "2024-02-15", "k")]})])
    as_of = date(2024, 1, 15)
    assert concept(con, "revenue", as_of, "as_first_reported").fetchall() == []
    assert concept(con, "revenue", as_of, "leaky_period_end").fetchall()[0][7] == 100.0


def test_snapshots_are_cached_until_the_data_changes(con, tmp_path):
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4-acme")
    dates = [date(2024, 8, 31)]
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 0          # cached
    load_docs(con, [{"cik": CIK, "facts": {"us-gaap": {"Goodwill": {"units": {"USD": [
        fact(None, "2024-06-30", 1.0, "2024-08-02", "late", form="10-Q")]}}}}}])
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1          # new fact filed before date
    path = tmp_path / "as_first_reported" / "metrics-2024-08-31.parquet"
    got = con.execute(f"SELECT ticker, ev_ebit FROM '{path}'").fetchall()
    assert got == [("ACME", pytest.approx(7 / 3))]


def test_report_written_to_reports_and_vault(con, tmp_path):
    d1 = date(2024, 1, 31)
    panel(con, [(d1, 1, "shortlist", 0.10, 0.20), (d1, 2, "fail", 0.0, 0.0)])
    results = {"default": {"as_first_reported": bt.evaluate(con, "ranked", cost_bps=0)}}
    md = bt.render_report(results, {"run_date": date(2024, 6, 1), "start": d1, "end": d1, "cost_bps": 0,
                                    "survivorship_gap": 0.25, "leak_check": None})
    path, note = bt.write_report(md, date(2024, 6, 1), tmp_path / "reports", tmp_path / "vault")
    assert path.name == "backtest-2024-06-01.md" and "| default |" in path.read_text()
    assert note.read_text().count("backtest-2024-06-01") >= 1
    assert bt.write_report(md, date(2024, 6, 1), tmp_path / "reports", tmp_path / "vault")[1] == note


def test_return_across_a_price_break_is_unknown(con):
    px(con, CIK, [("2024-01-31", 1.0), ("2024-02-05", 1.0), ("2024-02-06", 30.0), ("2024-02-29", 30.0),
                  ("2024-04-30", 30.0)])
    con.execute("CREATE TABLE snap AS SELECT ?::INTEGER AS cik, DATE '2024-01-31' AS as_of, "
                "DATE '2024-01-31' AS price_date, 1.0 AS close_raw", [CIK])
    rel = con.sql(bt.returns_sql("snap"))
    r = dict(zip(rel.columns, rel.fetchall()[0]))
    assert r["ret_1m"] is None and r["price_break_1m"] is True


def test_panel_keeps_only_investable_names(con):
    px(con, CIK, [("2024-01-31", 0.5), ("2024-02-29", 0.6)])
    px(con, 99, [("2024-01-31", 5.0), ("2024-02-29", 5.5)])
    con.execute("CREATE TABLE snap AS SELECT * FROM (VALUES (?::INTEGER, DATE '2024-01-31', DATE '2024-01-31', 0.5), "
                "(99, DATE '2024-01-31', DATE '2024-01-31', 5.0)) t(cik, as_of, price_date, close_raw)", [CIK])
    assert [r[0] for r in con.sql(f"SELECT cik FROM ({bt.panel_sql('snap')})").fetchall()] == [99]


# --- Codex review round 1 (reviews/M5-backtest.md) ---------------------------------

def test_snapshot_invalidates_after_price_correction(con, tmp_path):            # C1
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4")
    dates = [date(2024, 8, 31)]
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1
    con.execute("UPDATE prices SET close_raw = 20 WHERE cik = ? AND price_date = DATE '2024-08-30'", [CIK])
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1


def test_monthly_return_reaches_next_month_end(con):                             # C2
    px(con, CIK, [("2024-04-30", 100), ("2024-05-30", 100), ("2024-05-31", 200)])
    con.execute("CREATE TABLE snap AS SELECT ?::INTEGER cik, DATE '2024-04-30' as_of, "
                "DATE '2024-04-30' price_date, 100.0 close_raw", [CIK])
    rel = con.sql(bt.returns_sql("snap"))
    row = dict(zip(rel.columns, rel.fetchall()[0]))
    assert row["ret_1m"] == pytest.approx(1.0)


def test_entry_from_cash_pays_one_way_cost(con):                                 # C3
    panel(con, [(date(2024, 1, 31), 1, "shortlist", 0.0, 0.0)])
    result = bt.evaluate(con, "ranked", cost_bps=50)
    assert result["monthly"]["cagr"] == pytest.approx(0.995 ** 12 - 1)


def test_rebalancing_same_picks_after_returns_has_turnover(con):                 # C4
    panel(con, [
        (date(2024, 1, 31), 1, "shortlist", 1.0, 0.0),
        (date(2024, 1, 31), 2, "shortlist", 0.0, 0.0),
        (date(2024, 2, 29), 1, "shortlist", 0.0, 0.0),
        (date(2024, 2, 29), 2, "shortlist", 0.0, 0.0),
    ])
    result = bt.evaluate(con, "ranked", cost_bps=0)
    assert result["monthly"]["turnover"] == pytest.approx((1 + 1 / 6) / 2)


# --- Codex review round 2 ---------------------------------------------------------

def test_snapshot_invalidates_when_split_macro_changes(con, tmp_path):            # C5
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4")
    con.execute("INSERT INTO splits VALUES (?, DATE '2024-08-20', 2, 't')", [CIK])
    dates = [date(2024, 8, 31)]
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1
    con.execute("CREATE OR REPLACE MACRO split_factor(p_cik, p_date) AS (1.0)")
    assert bt.snapshots(con, dates, "as_first_reported", tmp_path) == 1


def test_missing_selected_return_does_not_reweight_other_holding(con):          # C6
    panel(con, [
        (date(2024, 1, 31), 1, "shortlist", 1.0, 0.0),
        (date(2024, 1, 31), 2, "shortlist", None, 0.0),
        (date(2024, 1, 31), 3, "fail", 0.0, 0.0),
    ])
    result = bt.evaluate(con, "ranked", cost_bps=0)
    assert result["monthly"]["cagr"] is None  # no complete portfolio month


def test_date_with_an_unknown_pick_is_left_out_of_horizon_stats(con):            # C6, same rule at 3m
    d1, d2 = date(2024, 1, 31), date(2024, 2, 29)
    panel(con, [
        (d1, 1, "shortlist", 0.0, 1.0), (d1, 2, "shortlist", 0.0, None), (d1, 3, "fail", 0.0, 0.0),
        (d2, 1, "shortlist", 0.0, 0.1), (d2, 3, "fail", 0.0, 0.0),
    ])
    h3 = bt.evaluate(con, "ranked", cost_bps=0)["horizons"][3]
    assert h3["dates"] == 1 and h3["excess"] == pytest.approx(0.1 - 0.05) and h3["unmeasured_dates"] == 1


# --- Codex review round 3 ---------------------------------------------------------

def test_unmeasured_month_does_not_produce_full_strategy_cagr(con):              # C7
    panel(con, [
        (date(2024, 1, 31), 1, "shortlist", None, 0.0),
        (date(2024, 1, 31), 2, "fail", 0.0, 0.0),
        (date(2024, 2, 29), 1, "shortlist", 0.0, 0.0),
        (date(2024, 2, 29), 2, "fail", 0.0, 0.0),
    ])
    monthly = bt.evaluate(con, "ranked", cost_bps=50)["monthly"]
    assert monthly["months_unmeasured"] == 1
    assert monthly["cagr"] is None


def test_all_unknown_realized_picks_are_counted(con):                             # C8
    panel(con, [(date(2024, 1, 31), 1, "shortlist", None, None)])
    con.execute("ALTER TABLE ranked ADD COLUMN price_break_1m BOOLEAN")
    con.execute("ALTER TABLE ranked ADD COLUMN price_break_3m BOOLEAN")
    con.execute("UPDATE ranked SET price_break_1m = TRUE, price_break_3m = TRUE")
    result = bt.evaluate(con, "ranked")
    assert result["monthly"]["months_unmeasured"] == 1
    assert result["horizons"][3]["unmeasured_dates"] == 1


def test_future_dates_are_not_counted_as_unmeasured(con):                         # C8 boundary
    panel(con, [(date(2024, 1, 31), 1, "shortlist", 0.1, 0.1), (date(2024, 1, 31), 2, "fail", 0.0, 0.0),
                (date(2024, 2, 29), 1, "shortlist", None, None), (date(2024, 2, 29), 2, "fail", None, None)])
    result = bt.evaluate(con, "ranked", cost_bps=0)
    assert result["monthly"]["months_unmeasured"] == 0 and result["monthly"]["months"] == 1
    assert result["horizons"][3]["unmeasured_dates"] == 0 and result["horizons"][3]["dates"] == 1
