"""M4 screener: valuation data-quality fixes and the ranked, traceable shortlist."""
from datetime import date

import duckdb
import pytest

from src.db.queries import valuation
from src.db.schema import connect
from src.ingestion.companyfacts import load_docs
from src.ingestion.form4_insider import parse_form4, store
from src.screen import Filters, screen, write_parquet
from tests.test_failure_modes import CIK, fact, form4_xml

AS_OF = date(2024, 9, 3)
GAAP, DEI = "us-gaap", "dei"
PRETAX = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"


@pytest.fixture
def con():
    return connect(":memory:")


def doc_for(cik: int, tags: dict) -> dict:
    facts = {}
    for (tax, tag, unit), rows in tags.items():
        facts.setdefault(tax, {})[tag] = {"units": {unit: rows}}
    return {"cik": cik, "facts": facts}


def inst(end, v, filed="2024-08-01", accn="q2"):
    return fact(None, end, v, filed, accn, form="10-Q", fp="Q2")


def annual(v, filed="2024-02-15", accn="k"):
    return fact("2023-01-01", "2023-12-31", v, filed, accn)


def flow(v):
    """FY 2023 plus equal first halves of 2024 and 2023 from the Q2 10-Q: TTM to June = v."""
    return [annual(v), fact("2024-01-01", "2024-06-30", v / 2, "2024-08-01", "q2", "10-Q", fp="Q2"),
            fact("2023-01-01", "2023-06-30", v / 2, "2024-08-01", "q2", "10-Q", fp="Q2")]


def cheap_company(debt=True, shares_tag=(DEI, "EntityCommonStockSharesOutstanding"), ebit_tag="OperatingIncomeLoss"):
    """Mcap 12M, cash 6M, debt 1M, EBIT 3M (EV/EBIT 2.3), FCF 2M."""
    tags = {
        (*shares_tag, "shares"): [inst("2024-07-25", 1_000_000)],
        (GAAP, "CashAndCashEquivalentsAtCarryingValue", "USD"): [inst("2024-06-30", 6_000_000)],
        (GAAP, ebit_tag, "USD"): flow(3_000_000),
        (GAAP, "NetCashProvidedByUsedInOperatingActivities", "USD"): flow(2_500_000),
        (GAAP, "PaymentsToAcquirePropertyPlantAndEquipment", "USD"): flow(500_000),
    }
    if debt:
        tags[(GAAP, "LongTermDebt", "USD")] = [inst("2024-06-30", 1_000_000)]
    return tags


def add_company(con, cik, ticker, tags, price_now=12.0, financial=False):
    load_docs(con, [doc_for(cik, tags)])
    con.execute("INSERT INTO companies (cik, name, is_financial, is_shell) VALUES (?, ?, ?, FALSE)",
                [cik, f"{ticker} Corp", financial])
    con.execute("INSERT INTO price_status VALUES (?, ?, 'ok', DATE '2024-01-02', ?, NULL, now())",
                [cik, ticker, AS_OF])
    con.executemany("INSERT INTO prices (cik, ticker, price_date, close_raw, source) VALUES (?, ?, ?, ?, 't')",
                    [(cik, ticker, date(2024, 1, 2), 30.0), (cik, ticker, date(2024, 8, 15), price_now),
                     (cik, ticker, date(2024, 8, 30), price_now)])


def add_buy(con, cik, accn, filed=date(2024, 8, 16), price=12.0):
    xml = form4_xml(cik * 10, f"BUYER {cik}", "Chief Executive Officer",
                    [("2024-08-15", "P", 10000, price, "A", 50000, "D", "", False)], doc_flag=0)
    store(con, parse_form4(xml.replace(f"<issuerCik>{CIK}<".encode(), f"<issuerCik>{cik}<".encode()),
                           accn, filed))


def val_row(con, as_of=AS_OF, cik=CIK):
    rel = valuation(con, as_of)
    return next(dict(zip(rel.columns, r)) for r in rel.fetchall() if r[0] == cik)


def screen_rows(con, as_of=AS_OF, filters=Filters()):
    rel = screen(con, as_of, filters)
    return {r["ticker"]: r for r in (dict(zip(rel.columns, t)) for t in rel.fetchall())}


# --- valuation fixes ---------------------------------------------------------

def test_debt_source_is_the_newest_by_period_end(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebt", "USD")] = [inst("2023-12-31", 5_000_000, "2024-02-15", "k")]
    tags[(GAAP, "LongTermDebtCurrent", "USD")] = [inst("2024-06-30", 100_000)]
    tags[(GAAP, "LongTermDebtNoncurrent", "USD")] = [inst("2024-06-30", 300_000)]
    add_company(con, CIK, "ACME", tags)
    row = val_row(con)
    assert row["debt"] == 400_000                      # the June pair, not the stale December total
    assert row["debt_period_end"] == date(2024, 6, 30)
    assert "stale_debt" not in row["issues"]


def test_newer_debt_total_beats_older_split(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebt", "USD")] = [inst("2024-06-30", 700_000)]
    tags[(GAAP, "LongTermDebtNoncurrent", "USD")] = [inst("2023-12-31", 300_000, "2024-02-15", "k")]
    add_company(con, CIK, "ACME", tags)
    row = val_row(con)
    assert row["debt"] == 700_000 and row["issues"] == []


def test_debt_adds_short_term_borrowings_and_skips_old_lines(con):
    # JNJ 2026: 10-Q tags noncurrent debt + short-term borrowings; current LTD only in the 10-K.
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebtNoncurrent", "USD")] = [inst("2024-06-30", 300_000)]
    tags[(GAAP, "ShortTermBorrowings", "USD")] = [inst("2024-06-30", 50_000)]
    tags[(GAAP, "LongTermDebtCurrent", "USD")] = [inst("2023-12-31", 900_000, "2024-02-15", "k")]
    add_company(con, CIK, "JNJ", tags)
    row = val_row(con)
    assert row["debt"] == 350_000 and row["issues"] == []


def test_debt_current_already_includes_short_term_borrowings(con):
    # VLO / COP: DebtCurrent is the whole current line; adding borrowings would double count.
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebtAndCapitalLeaseObligations", "USD")] = [inst("2024-06-30", 300_000)]
    tags[(GAAP, "DebtCurrent", "USD")] = [inst("2024-06-30", 80_000)]
    tags[(GAAP, "ShortTermBorrowings", "USD")] = [inst("2024-06-30", 50_000)]
    add_company(con, CIK, "COP", tags)
    assert val_row(con)["debt"] == 380_000


def test_capex_ladder_covers_oil_and_gas(con):
    tags = cheap_company()
    tags[(GAAP, "PaymentsToExploreAndDevelopOilAndGasProperties", "USD")] = \
        tags.pop((GAAP, "PaymentsToAcquirePropertyPlantAndEquipment", "USD"))
    add_company(con, CIK, "COP", tags)
    assert val_row(con)["fcf_ttm"] == 2_000_000


def test_foreign_filer_is_an_issue(con):
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [fact(None, "2024-06-30", 1_000_000, "2024-08-01", "f", form="20-F")]
    add_company(con, CIK, "TSM", tags)
    assert "foreign_filer" in val_row(con)["issues"]


def test_stale_cash_and_missing_debt_are_issues(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "CashAndCashEquivalentsAtCarryingValue", "USD")] = [inst("2023-12-31", 6_000_000, "2024-02-15", "k")]
    tags[(GAAP, "StockholdersEquity", "USD")] = [inst("2024-06-30", 9_000_000)]
    add_company(con, CIK, "ACME", tags)
    row = val_row(con)
    assert row["debt"] is None and row["ev"] is None
    assert {"stale_cash", "missing_debt"} <= set(row["issues"])


def test_short_term_investments_only_count_at_the_cash_date(con):
    tags = cheap_company()
    tags[(GAAP, "ShortTermInvestments", "USD")] = [inst("2023-12-31", 4_000_000, "2024-02-15", "k")]
    add_company(con, CIK, "ACME", tags)
    assert val_row(con)["cash_like"] == 6_000_000


def test_ebit_falls_back_to_pretax_income(con):
    add_company(con, CIK, "XOM", cheap_company(ebit_tag=PRETAX))
    row = val_row(con)
    assert row["ebit_ttm"] == 3_000_000 and row["ebit_tag"] == PRETAX
    assert row["ev_ebit"] == pytest.approx(7 / 3)


def test_multi_class_share_fallback_is_flagged(con):
    add_company(con, CIK, "GOOGL", cheap_company(shares_tag=(GAAP, "CommonStockSharesOutstanding")))
    add_company(con, 2000, "AAPL", cheap_company())
    assert val_row(con)["multi_class_risk"] is True
    assert val_row(con, cik=2000)["multi_class_risk"] is False


def test_company_without_shares_is_kept_with_an_issue(con):
    tags = cheap_company()
    del tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")]
    add_company(con, CIK, "META", tags)
    row = val_row(con)
    assert row["mcap"] is None and "missing_shares" in row["issues"] and row["multi_class_risk"]


def test_period_typo_in_the_future_is_ignored(con):
    # Real case: cover shares dated 2034-03-05 in a 10-K/A filed 2024-03-27. As the
    # "latest" instant it beat every real count for a decade.
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")].append(inst("2034-03-05", 9, "2024-03-27", "typo"))
    add_company(con, CIK, "ACME", tags)
    row = val_row(con)
    assert row["shares_period_end"] == date(2024, 7, 25) and row["mcap"] == 12_000_000


def test_cover_date_a_few_days_after_filing_still_counts(con):
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2024-08-03", 1_000_000, "2024-08-01")]
    add_company(con, CIK, "ACME", tags)
    assert val_row(con)["shares_period_end"] == date(2024, 8, 3)


def test_valuation_is_one_pass_over_facts(con):
    add_company(con, CIK, "ACME", cheap_company())   # an empty table is optimised away
    plan = con.execute("EXPLAIN (FORMAT JSON) " + valuation(con, AS_OF).sql_query(), {"as_of": AS_OF}).fetchall()[0][-1]
    assert plan.count("main.xbrl_facts") == 1        # was one scan per concept (11)


# --- screen ------------------------------------------------------------------

def test_screen_shortlist_quarantine_fail_and_trace(con):
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4-acme")
    add_company(con, 2000, "NODEBT", cheap_company(debt=False))
    add_buy(con, 2000, "f4-nodebt")
    add_company(con, 3000, "PRICEY", cheap_company(), price_now=29.0)       # drawdown only 3%
    add_buy(con, 3000, "f4-pricey", price=29.0)
    add_company(con, 4000, "BANK", cheap_company(), financial=True)
    add_buy(con, 4000, "f4-bank")
    add_company(con, 5000, "LATE", cheap_company())
    add_buy(con, 5000, "f4-late", filed=date(2024, 9, 4))                   # filed after as_of

    rows = screen_rows(con, filters=Filters(min_drawdown=0.5))
    assert "BANK" not in rows                                              # financials excluded
    acme = rows["ACME"]
    assert acme["status"] == "shortlist" and acme["rank"] == 1
    assert acme["ev_ebit"] == pytest.approx(7 / 3)
    assert acme["insider_buy_90d_usd"] == 120_000
    assert acme["form4_accns"] == ["f4-acme"]
    used = {(s["item"], a) for s in acme["sources"] for a in s["accns"]}
    assert {("shares", "q2"), ("cash", "q2"), ("debt", "q2"), ("ebit", "k"), ("cfo", "k"), ("capex", "k")} <= used
    assert rows["NODEBT"]["status"] == "quarantine" and rows["NODEBT"]["rank"] is None
    assert "missing_debt" in rows["NODEBT"]["issues"]
    assert rows["PRICEY"]["status"] == "fail" and "drawdown" in rows["PRICEY"]["failed_filters"]
    assert screen_rows(con)["PRICEY"]["failed_filters"] == ["ev_ebit"]     # no drawdown rule by default
    assert rows["LATE"]["status"] == "fail" and rows["LATE"]["form4_accns"] is None


def test_screen_ignores_restatement_filed_after_as_of(con):
    tags = cheap_company()
    tags[(GAAP, "OperatingIncomeLoss", "USD")].append(annual(-5_000_000, "2024-10-01", "k-a"))
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4-acme")
    assert screen_rows(con)["ACME"]["status"] == "shortlist"
    assert screen_rows(con, date(2024, 10, 2))["ACME"]["status"] == "fail"


def test_screen_filters_are_tunable(con):
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4-acme")
    assert screen_rows(con, filters=Filters(max_ev_ebit=2.0))["ACME"]["status"] == "fail"


def test_parquet_snapshot_round_trips(con, tmp_path):
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "f4-acme")
    path = write_parquet(screen(con, AS_OF).to_arrow_table(), AS_OF, Filters(), tmp_path)
    assert path.name == "screen-2024-09-03.parquet"
    got = duckdb.sql(f"SELECT ticker, status, form4_accns FROM '{path}'").fetchall()
    assert got == [("ACME", "shortlist", ["f4-acme"])]
    meta = dict(duckdb.sql(f"SELECT decode(key), decode(value) FROM parquet_kv_metadata('{path}')").fetchall())
    assert meta["as_of"] == "2024-09-03" and "max_ev_ebit" in meta["filters"]


# --- review findings on M4 ---------------------------------------------------

def test_short_term_only_debt_is_not_zero(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "ShortTermBorrowings", "USD")] = [inst("2024-06-30", 9_000_000)]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    assert screen_rows(con)["ACME"]["debt"] == 9_000_000


def test_total_does_not_hide_larger_current_debt(con):
    tags = cheap_company()  # LongTermDebt: $1M
    tags[(GAAP, "DebtCurrent", "USD")] = [inst("2024-06-30", 9_000_000)]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    assert screen_rows(con)["ACME"]["status"] != "shortlist"


def test_missing_newer_current_debt_is_suspect(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebtNoncurrent", "USD")] = [inst("2024-06-30", 300_000)]
    tags[(GAAP, "LongTermDebtCurrent", "USD")] = [inst("2023-12-31", 9_000_000, "2024-02-15", "k")]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    assert screen_rows(con)["ACME"]["status"] != "shortlist"


def test_future_cover_date_cannot_reach_shortlist(con):
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2024-09-06", 1_000_000, "2024-09-01", "later")]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    assert screen_rows(con)["ACME"]["status"] != "shortlist"


def test_mixed_capex_bridge_is_quarantined(con):
    tags = cheap_company()
    key = (GAAP, "PaymentsToAcquirePropertyPlantAndEquipment", "USD")
    fy, current, prior = tags[key]
    tags[key] = [fy, prior]
    tags[(GAAP, "PaymentsToAcquireProductiveAssets", "USD")] = [dict(current, val=200_000)]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    assert screen_rows(con)["ACME"]["status"] != "shortlist"


def test_foreign_cash_form_is_not_masked(con):
    tags = cheap_company()
    tags[(GAAP, "CashAndCashEquivalentsAtCarryingValue", "USD")] = [
        fact(None, "2024-06-30", 6_000_000, "2024-08-01", "f", form="20-F")]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    assert screen_rows(con)["ACME"]["status"] != "shortlist"


def test_old_zero_or_immaterial_debt_line_is_not_stale(con):
    # NVDA: commercial paper last tagged as 0 a year ago. KO: $0.25B of paper missing from
    # one 10-Q against $41.5B of debt. Neither makes the debt figure untrustworthy.
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebtNoncurrent", "USD")] = [inst("2024-06-30", 1_000_000)]
    tags[(GAAP, "CommercialPaper", "USD")] = [inst("2023-12-31", 0, "2024-02-15", "k")]
    tags[(GAAP, "LongTermDebtCurrent", "USD")] = [inst("2023-12-31", 20_000, "2024-02-15", "k")]
    add_company(con, CIK, "NVDA", tags)
    row = val_row(con)
    assert row["debt"] == 1_000_000 and row["issues"] == []


def test_debt_line_gone_for_over_a_year_is_not_stale(con):
    # WFRD: short-term borrowings last tagged 18 months before the newest balance sheet,
    # so at least one full 10-K has left the line out since.
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebtNoncurrent", "USD")] = [inst("2024-06-30", 1_000_000)]
    tags[(GAAP, "ShortTermBorrowings", "USD")] = [inst("2022-12-31", 500_000, "2023-02-15", "k22")]
    add_company(con, CIK, "WFRD", tags)
    assert val_row(con)["issues"] == []


def test_total_equal_to_current_plus_noncurrent_excludes_borrowings(con):
    # WMT tags DebtLongtermAndShorttermCombinedAmount = current + noncurrent LTD (39.93B)
    # and reports $10.5B of short-term borrowings beside it.
    tags = cheap_company(debt=False)
    tags[(GAAP, "DebtLongtermAndShorttermCombinedAmount", "USD")] = [inst("2024-06-30", 400_000)]
    tags[(GAAP, "LongTermDebtCurrent", "USD")] = [inst("2024-06-30", 100_000)]
    tags[(GAAP, "LongTermDebtNoncurrent", "USD")] = [inst("2024-06-30", 300_000)]
    tags[(GAAP, "ShortTermBorrowings", "USD")] = [inst("2024-06-30", 200_000)]
    add_company(con, CIK, "WMT", tags)
    row = val_row(con)
    assert row["debt"] == 600_000 and row["issues"] == []


def test_total_with_partial_split_is_compared_only_when_inconsistent(con):
    # TSLA: LongTermDebt total 7.96B and DebtCurrent 1.31B, no noncurrent line this quarter.
    # Current debt can sit inside the total, so the total stands.
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebt", "USD")] = [inst("2024-06-30", 796_000)]
    tags[(GAAP, "DebtCurrent", "USD")] = [inst("2024-06-30", 131_000)]
    add_company(con, CIK, "TSLA", tags)
    row = val_row(con)
    assert row["debt"] == 796_000 and row["issues"] == []


def test_trade_dated_after_its_filing_is_not_a_buy(con):
    # Real case: a Form 4 filed 2022-11-18 with transaction date 2033-11-18. As a "future"
    # trade it fell inside every 90-day window until 2033.
    add_company(con, CIK, "ACME", cheap_company())
    xml = form4_xml(10, "TYPO", "Chief Executive Officer",
                    [("2033-11-18", "P", 10000, 12.0, "A", 50000, "D", "", False)], doc_flag=0)
    store(con, parse_form4(xml, "f4-typo", date(2024, 8, 16)))
    assert screen_rows(con)["ACME"]["n_insiders"] is None


def test_penny_price_is_an_issue(con):
    # PPCB at $0.0002 made a +2,500,000% month in the backtest: sub-$1 quotes are not tradeable
    # in size and move a whole tick at a time.
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2024-07-25", 20_000_000)]
    add_company(con, CIK, "PPCB", tags, price_now=0.6)
    assert "penny_price" in val_row(con)["issues"]


def test_ten_fold_jump_in_a_day_is_a_price_break(con):
    # CHRD 2020: Oasis bankruptcy shares at $0.11, then new shares at $34 under the same ticker.
    add_company(con, CIK, "CHRD", cheap_company())
    con.execute("INSERT INTO prices (cik, ticker, price_date, close_raw, source) VALUES (?, 'CHRD', DATE '2024-03-01', 0.9, 't'), (?, 'CHRD', DATE '2024-03-04', 12.0, 't')", [CIK, CIK])
    assert "price_break" in val_row(con)["issues"]


CFO = "NetCashProvidedByUsedInOperatingActivities"
CFO_CONT = "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"


@pytest.mark.parametrize("plain_first", [True, False])
def test_tied_prior_year_ytd_is_picked_the_same_way(con, plain_first):
    # HOFT on 2026-11-07: two prior-year YTD rows end on the same day with starts one day
    # apart: the current 10-Q's comparative (the year's tag) and an old 10-Q's own figure
    # (another tag). row_number() ordered by period_end alone took either, run to run.
    tags = cheap_company()
    del tags[(GAAP, CFO, "USD")]
    plain = {(GAAP, CFO, "USD"): [fact("2023-01-02", "2023-06-30", 500_000, "2023-08-01", "q2old", "10-Q", fp="Q2")]}
    cont = {(GAAP, CFO_CONT, "USD"): [
        annual(2_500_000), fact("2024-01-01", "2024-06-30", 1_250_000, "2024-08-01", "q2", "10-Q", fp="Q2"),
        fact("2023-01-01", "2023-06-30", 1_250_000, "2024-08-01", "q2", "10-Q", fp="Q2")]}
    tags.update({**plain, **cont} if plain_first else {**cont, **plain})
    add_company(con, CIK, "HOFT", tags)
    row = val_row(con)
    assert row["cfo_ttm"] == 2_500_000 and row["fcf_same_tag"]
    assert row["issues"] == []
