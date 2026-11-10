"""M11 Phase 2 in the screen: 2b market cap from proved share classes, 2c debt confirmed zero.

Ledger rows are written directly here; test_m11_fetch covers how they are fetched.
"""
import json
from datetime import date, datetime

import pytest

from src.db.schema import connect
from src.screen import screen_table
from tests.test_m11_quarantine import AS_OF, CIK, checks, quarantined, stale_share_company
from tests.test_m4_screen import GAAP, cheap_company

Q2_FILED = date(2024, 8, 1)


@pytest.fixture
def con():
    return connect(":memory:")


def cover(con, classes, symbols, result="classes", filed=Q2_FILED, accn="q2", cik=CIK):
    con.execute("INSERT INTO quarantine_checks VALUES (?, ?, 'm11_cover', 1, 'ok', ?, ?, ?, NULL, ?, ?)",
                [cik, accn, filed, result, max(c[3] for c in classes), json.dumps({"symbols": symbols}), datetime.now()])
    con.executemany("INSERT INTO cover_shares VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(cik, accn, filed, m, n, s, d) for m, n, s, d in classes])


def zero(con, period_end=date(2024, 6, 30), filed=Q2_FILED, result="debt_zero", accn="q2"):
    con.execute("INSERT INTO quarantine_checks VALUES (?, ?, 'm11_balance_sheet', 1, 'ok', ?, ?, ?, 'r', ?, ?)",
                [CIK, accn, filed, result, period_end, json.dumps({"candidates": [], "method": "balance_sheet_no_debt"}),
                 datetime.now()])


def row(con, ticker="MIR", as_of=AS_OF):
    return next(r for r in screen_table(con, as_of).to_pylist() if r["ticker"] == ticker)


A = "us-gaap:CommonClassAMember"
B = "us-gaap:CommonClassBMember"
D = date(2024, 7, 25)


# --- 2b ---------------------------------------------------------------------------

def test_one_class_one_symbol_equal_to_the_priced_ticker_gives_market_cap(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    before = row(con)
    assert before["status"] == "quarantine" and "stale_shares" in before["issues"]
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"])
    r = row(con)
    assert r["mcap"] == 12_000_000 and r["shares_period_end"] == D
    assert "stale_shares" not in r["issues"] and "multi_class_unverified" not in r["issues"]
    assert "multi_class_risk" in r["flags"]                                   # stays as a flag
    assert r["status"] == "shortlist"
    sh = next(s for s in r["sources"] if s["item"] == "shares")
    assert sh["accns"] == ["q2"] and A in sh["tag"]


def test_c2_unpriced_second_class_is_unverified_never_summed(con):
    # 1M class A at $12 and 1M class B worth $100 a share with no stored class B close.
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D), (B, 1_000_000, "MIRB", D)], ["MIR", "MIRB"])
    r = row(con)
    assert "multi_class_unverified" in r["issues"] and r["status"] == "quarantine"
    assert r["mcap"] != 12_000_000 * 2


def test_voting_only_class_without_symbol_is_unverified(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D), (B, 5_759_555, None, D)], ["MIR"])
    assert "multi_class_unverified" in row(con)["issues"]


def test_disagreeing_contexts_are_unverified(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"], result="conflict")
    assert "multi_class_unverified" in row(con)["issues"]


def test_single_class_symbol_not_the_priced_ticker_is_unverified(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "OTHER", D)], ["OTHER"])
    assert "multi_class_unverified" in row(con)["issues"]


def test_c22_two_classes_stay_blocked_under_primary_only_prices(con):
    # Was a positive test that borrowed another issuer's quote (CIK 999); the plan keeps case two blocked.
    quarantined(con, CIK, "MIR", stale_share_company())
    con.execute("INSERT INTO prices (cik, ticker, price_date, close_raw, source) VALUES (999, 'MIRB', DATE '2024-08-30', 100.0, 't')")
    cover(con, [(A, 1_000_000, "MIR", D), (B, 100_000, "MIRB", D)], ["MIR", "MIRB"])
    r = row(con)
    assert "multi_class_unverified" in r["issues"] and r["shares_period_end"] == date(2023, 12, 31)


def test_c22_two_classes_on_the_same_primary_quote_are_blocked(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D), (B, 1_000_000, "MIR", D)], ["MIR"])
    assert "multi_class_unverified" in row(con)["issues"]


def test_c22_single_class_binds_to_the_as_of_quote_not_refresh_metadata(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"])
    con.execute("UPDATE price_status SET ticker = 'FUTURE'")
    r = next(x for x in screen_table(con, AS_OF).to_pylist() if x["cik"] == CIK)   # display ticker is now FUTURE
    assert r["shares_period_end"] == D and "multi_class_unverified" not in r["issues"]


def test_cover_filed_after_as_of_is_not_used(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"], filed=date(2024, 9, 20), accn="late")
    r = row(con)
    assert "stale_shares" in r["issues"] and r["shares_period_end"] == date(2023, 12, 31)


# --- 2c ---------------------------------------------------------------------------

def no_debt_company():
    tags = cheap_company(debt=False)
    return tags


def test_debt_confirmed_zero_at_bs_date_clears_missing_debt(con):
    quarantined(con, CIK, "ACME", no_debt_company())
    assert "missing_debt" in row(con, "ACME")["issues"]
    zero(con)
    r = row(con, "ACME")
    assert r["debt"] == 0 and r["debt_period_end"] == date(2024, 6, 30) and r["issues"] == []
    assert r["status"] == "shortlist"
    d = next(s for s in r["sources"] if s["item"] == "debt")
    assert d["tag"] == "balance_sheet_no_debt" and d["accns"] == ["q2"]


def test_not_zero_result_keeps_missing_debt(con):
    quarantined(con, CIK, "ACME", no_debt_company())
    zero(con, result="not_zero")
    assert "missing_debt" in row(con, "ACME")["issues"]


def test_zero_for_an_older_column_than_bs_date_is_not_used(con):
    quarantined(con, CIK, "ACME", no_debt_company())
    zero(con, period_end=date(2024, 3, 31))
    assert "missing_debt" in row(con, "ACME")["issues"]


def test_zero_filed_after_as_of_is_not_used(con):
    quarantined(con, CIK, "ACME", no_debt_company())
    zero(con, filed=date(2024, 9, 20), accn="late")
    assert "missing_debt" in row(con, "ACME")["issues"]


def test_zero_never_overrides_current_tagged_debt(con):
    quarantined(con, CIK, "ACME", cheap_company())                       # LongTermDebt 1M at 06-30
    zero(con)
    r = row(con, "ACME")
    assert r["debt"] == 1_000_000


def test_zero_replaces_stale_debt_at_the_bs_date(con):
    from tests.test_m4_screen import inst
    tags = cheap_company(debt=False)
    tags[(GAAP, "LongTermDebt", "USD")] = [inst("2023-12-31", 5_000_000, "2024-02-15", "k")]
    quarantined(con, CIK, "ACME", tags)
    assert "stale_debt" in row(con, "ACME")["issues"]
    zero(con)
    r = row(con, "ACME")
    assert r["debt"] == 0 and "stale_debt" not in r["issues"]


def test_resolved_input_can_fail_a_filter(con):
    # Status can move either way: a proved market cap may fail EV/EBIT.
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 50_000_000, "MIR", D)], ["MIR"])                     # mcap $600M against EBIT $3M
    r = row(con)
    assert r["status"] == "fail" and "ev_ebit" in r["failed_filters"]


# --- integration acceptance (Codex, round 10) -------------------------------------------

def test_cover_count_dated_after_its_filing_plus_seven_days_is_not_used(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", date(2034, 6, 30))], ["MIR"])
    r = row(con)
    assert "stale_shares" in r["issues"] and r["shares_period_end"] == date(2023, 12, 31)


def test_cover_row_from_an_older_parser_is_not_used(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"])
    con.execute("UPDATE quarantine_checks SET parser = 0")
    r = row(con)
    assert "stale_shares" in r["issues"] and "multi_class_unverified" not in r["issues"]


@pytest.mark.parametrize("n", [0, -5])
def test_non_positive_single_class_count_is_unverified(con, n):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, n, "MIR", D)], ["MIR"])
    r = row(con)
    assert "multi_class_unverified" in r["issues"] and r["shares_period_end"] == date(2023, 12, 31)


def test_split_between_cover_date_and_price_date_scales_the_count(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    con.execute("INSERT INTO splits VALUES (?, DATE '2024-08-10', 2.0, 't')", [CIK])
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"])
    assert row(con)["mcap"] == pytest.approx(2_000_000 * 12)


def test_zero_from_an_accession_the_balance_sheet_did_not_come_from_is_not_used(con):
    quarantined(con, CIK, "ACME", no_debt_company())
    zero(con, accn="other")                                            # same date, different filing
    assert "missing_debt" in row(con, "ACME")["issues"]


# --- review round 11 ----------------------------------------------------------------------

from src.ingestion.companyfacts import load_docs
from tests.test_m4_screen import DEI, doc_for, inst


def later_filing(con, tag=(GAAP, "StockholdersEquity", "USD"), day="2024-06-30", value=9_000_000, accn="q3",
                 filed="2024-08-25"):
    load_docs(con, [doc_for(CIK, {tag: [inst(day, value, filed, accn)]})])


def test_c19_old_total_dei_count_yields_to_a_newer_proved_cover(con):
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2023-01-01", 1_000_000, "2023-02-15", "k0")]
    quarantined(con, CIK, "MIR", tags)
    assert "stale_shares" in row(con)["issues"]
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"])
    r = row(con)
    assert r["shares_period_end"] == D and "stale_shares" not in r["issues"]


def test_c19_newer_total_dei_count_keeps_priority(con):
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2024-07-30", 1_000_000)]   # q2, after D
    quarantined(con, CIK, "MIR", tags)
    cover(con, [(A, 2_000_000, "MIR", D)], ["MIR"])
    r = row(con)
    assert r["shares_period_end"] == date(2024, 7, 30) and r["mcap"] == 12_000_000


def test_c19_newer_unverified_cover_blocks_an_older_total(con):
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2023-01-01", 1_000_000, "2023-02-15", "k0")]
    quarantined(con, CIK, "MIR", tags)
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"], result="conflict")
    assert "multi_class_unverified" in row(con)["issues"]


def test_c20_newer_not_found_cover_does_not_revive_the_older_proof(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"])
    later_filing(con)
    con.execute("INSERT INTO quarantine_checks VALUES (?, 'q3', 'm11_cover', 1, 'not_found', DATE '2024-08-25', NULL, NULL, "
                "'none', '{}', now())", [CIK])
    r = row(con)
    assert r["shares_period_end"] == date(2023, 12, 31) and "stale_shares" in r["issues"]


def test_c20_newer_unprocessed_filing_does_not_inherit_the_older_proof(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D)], ["MIR"])
    later_filing(con)
    assert "stale_shares" in row(con)["issues"]


def test_c20_newer_not_zero_does_not_revive_an_older_zero(con):
    quarantined(con, CIK, "ACME", cheap_company(debt=False))
    zero(con)
    later_filing(con)
    zero(con, accn="q3", filed=date(2024, 8, 25), result="not_zero")
    r = row(con, "ACME")
    assert "missing_debt" in r["issues"] and r["status"] == "quarantine"


def test_c20_newer_unprocessed_filing_does_not_inherit_an_older_zero(con):
    quarantined(con, CIK, "ACME", cheap_company(debt=False))
    zero(con)
    later_filing(con)
    assert "missing_debt" in row(con, "ACME")["issues"]


def test_c20_amendment_is_not_an_eligible_resolving_filing(con):
    quarantined(con, CIK, "ACME", cheap_company(debt=False))
    zero(con)
    load_docs(con, [doc_for(CIK, {(GAAP, "StockholdersEquity", "USD"):
                                  [{**inst("2024-06-30", 9_000_000, "2024-08-25", "q2a"), "form": "10-Q/A"}]})])
    assert row(con, "ACME")["debt"] == 0                               # the 10-Q/A does not displace q2


def test_c20_diagnostic_names_the_same_governing_filing(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 1_000_000, "MIR", D), (B, 5, None, D)], ["MIR"])
    later_filing(con)
    con.execute("INSERT INTO quarantine_checks VALUES (?, 'q3', 'm11_cover', 1, 'ok', DATE '2024-08-25', 'conflict', NULL, "
                "'r', '{\"symbols\": []}', now())", [CIK])
    r = row(con)
    assert r["m11_filing"] == "q3"
    assert "q3" in checks(r)["multi_class_unverified"]["message"]


def test_c21_zero_never_erases_current_positive_debt_with_a_dropped_line(con):
    tags = cheap_company()                                             # LongTermDebt 1M at 06-30
    tags[(GAAP, "ShortTermBorrowings", "USD")] = [inst("2023-12-31", 1_000_000, "2024-02-15", "k")]
    quarantined(con, CIK, "ACME", tags)
    assert "stale_debt" in row(con, "ACME")["issues"]
    zero(con)
    r = row(con, "ACME")
    assert r["debt"] == 1_000_000 and "stale_debt" in r["issues"] and r["status"] == "quarantine"
