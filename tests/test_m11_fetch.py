"""M11 Phase 2 requests and ledger (C5, C6): mocked SEC client, in-memory DB."""
from datetime import date

import pytest
import requests

from src.db.schema import connect
from src.ingestion.quarantine_checks import PARSERS, run_checks
from src.screen import screen_table
from tests.test_m11_docs import (count, ctx, filing_summary, index, instance, rpage, six_rows, symbol, usd)
from tests.test_m11_quarantine import AS_OF, CIK, cheap_company, quarantined, stale_share_company
from tests.test_m4_screen import GAAP, add_company, inst

BASE = f"https://www.sec.gov/Archives/edgar/data/{CIK}/q2/"     # accession 'q2' has no dashes
URLS = {"index": BASE + "index.json", "instance": BASE + "acme-20240630_htm.xml",
        "fs": BASE + "FilingSummary.xml", "r": BASE + "R2.htm"}


class FakeSec:
    def __init__(self, docs: dict):
        self.docs, self.calls = dict(docs), []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        d = self.docs.get(url)
        if isinstance(d, Exception):
            raise d
        if d is None:
            raise requests.HTTPError(f"404 {url}")
        return d


def good_docs(cover=True, zero=True) -> dict:
    rows, facts = six_rows()
    parts = [ctx("c", "2024-06-30"), ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"),
             ctx("b", "2024-07-25", "us-gaap:CommonClassBMember"), *facts]
    if cover:
        parts += [count("a", 1_000_000), count("b", 500_000), symbol("a", "MIR")]
    page = rpage(["Jun. 30, 2024"], ["us-gaap_CashAndCashEquivalentsAtCarryingValue", "us-gaap_Assets", *rows])
    return {URLS["index"]: index("acme-20240630.htm", "acme-20240630_htm.xml"),
            URLS["instance"]: instance(*parts),
            URLS["fs"]: filing_summary(("Condensed Consolidated Balance Sheets", "R2.htm", "Statements")),
            URLS["r"]: page}


@pytest.fixture
def con():
    return connect(":memory:")


def rows(con):
    return screen_table(con, AS_OF).to_pylist()


def ledger(con):
    return {fam: (st, res) for fam, st, res in con.execute(
        "SELECT family, status, result FROM quarantine_checks ORDER BY family").fetchall()}


def both_families(con):
    """No DEI cover count (no_cover_count) and no debt (missing_debt), latest filing q2."""
    tags = stale_share_company()
    del tags[(GAAP, "LongTermDebt", "USD")]
    quarantined(con, CIK, "MIR", tags)


def test_cold_filing_needing_both_families_makes_exactly_four_requests(con):
    both_families(con)
    sec = FakeSec(good_docs())
    got = run_checks(con, sec, rows(con), AS_OF)
    assert sorted(sec.calls) == sorted(URLS.values()) and len(sec.calls) == 4     # instance once, shared
    assert ledger(con) == {"m11_balance_sheet": ("ok", "debt_zero"), "m11_cover": ("ok", "classes")}
    assert con.execute("SELECT member, shares, symbol FROM cover_shares ORDER BY member").fetchall() == [
        ("us-gaap:CommonClassAMember", 1_000_000.0, "MIR"), ("us-gaap:CommonClassBMember", 500_000.0, None)]
    assert got["ok"] == 2


def test_cover_only_makes_two_requests(con):
    quarantined(con, CIK, "MIR", stale_share_company())          # has debt: no balance-sheet family
    sec = FakeSec(good_docs())
    run_checks(con, sec, rows(con), AS_OF)
    assert sorted(sec.calls) == sorted([URLS["index"], URLS["instance"]])
    assert set(ledger(con)) == {"m11_cover"}


def test_documents_already_fetched_in_this_invocation_are_reused(con):
    # M10 (or the other family) fetched index and instance first: only FilingSummary and R page.
    both_families(con)
    docs = good_docs()
    cache = {URLS["index"]: docs[URLS["index"]], URLS["instance"]: docs[URLS["instance"]]}
    sec = FakeSec(docs)
    run_checks(con, sec, rows(con), AS_OF, cache=cache)
    assert sorted(sec.calls) == sorted([URLS["fs"], URLS["r"]])


def test_failed_filing_summary_errors_one_family_and_is_retried_alone(con):
    both_families(con)
    docs = good_docs()
    docs[URLS["fs"]] = requests.ConnectionError("dropped")
    run_checks(con, FakeSec(docs), rows(con), AS_OF)
    assert ledger(con) == {"m11_balance_sheet": ("error", None), "m11_cover": ("ok", "classes")}
    sec = FakeSec(good_docs())
    run_checks(con, sec, rows(con), AS_OF)
    assert sorted(sec.calls) == sorted(URLS.values())               # 4 for the balance sheet, 0 for cover
    assert ledger(con)["m11_balance_sheet"] == ("ok", "debt_zero")


def test_completed_filing_makes_no_requests(con):
    both_families(con)
    run_checks(con, FakeSec(good_docs()), rows(con), AS_OF)
    sec = FakeSec(good_docs())
    assert sum(run_checks(con, sec, rows(con), AS_OF).values()) == 0
    assert sec.calls == []


def test_older_parser_version_is_fetched_again(con):
    both_families(con)
    run_checks(con, FakeSec(good_docs()), rows(con), AS_OF)
    con.execute("UPDATE quarantine_checks SET parser = parser - 1 WHERE family = 'm11_cover'")
    sec = FakeSec(good_docs())
    run_checks(con, sec, rows(con), AS_OF)
    assert sorted(sec.calls) == sorted([URLS["index"], URLS["instance"]])
    assert con.execute("SELECT parser FROM quarantine_checks WHERE family = 'm11_cover'").fetchone()[0] == PARSERS["m11_cover"]


def test_failed_row_is_never_fetched_even_with_multi_class_risk(con):
    add_company(con, CIK, "MIR", stale_share_company())          # no insider buy: fails the screen
    r = rows(con)
    assert r[0]["status"] == "fail" and "multi_class_risk" in r[0]["flags"]
    sec = FakeSec(good_docs())
    run_checks(con, sec, r, AS_OF)
    assert sec.calls == [] and con.execute("SELECT count(*) FROM quarantine_checks").fetchone()[0] == 0


def test_quarantined_with_only_missing_ebit_makes_no_requests(con):
    tags = cheap_company()
    del tags[(GAAP, "OperatingIncomeLoss", "USD")]
    quarantined(con, CIK, "ACME", tags)
    sec = FakeSec(good_docs())
    run_checks(con, sec, rows(con), AS_OF)
    assert sec.calls == []


@pytest.mark.parametrize("broken, key", [
    (index("a.htm"), "index"),                                        # no instance
    (index("a_htm.xml", "b_htm.xml"), "index"),                       # two instances
    (b"<xbrl><unclosed>", "instance"),                                # malformed XML
    (filing_summary(("Statements of Operations", "R4.htm", "Statements")), "fs")])   # no balance sheet
def test_document_errors_are_error_rows_never_zero(con, broken, key):
    both_families(con)
    docs = good_docs()
    docs[URLS[key]] = broken
    run_checks(con, FakeSec(docs), rows(con), AS_OF)
    st = ledger(con)["m11_balance_sheet"]
    assert st[0] == "error" and st[1] is None
    assert con.execute("SELECT message FROM quarantine_checks WHERE family = 'm11_balance_sheet'").fetchone()[0]


def test_ten_q_filed_after_as_of_is_not_used(con):
    both_families(con)
    load_late_10q(con)
    sec = FakeSec(good_docs())
    run_checks(con, sec, rows(con), AS_OF)
    assert all("/late/" not in u for u in sec.calls)


def load_late_10q(con):
    from src.ingestion.companyfacts import load_docs
    from tests.test_m4_screen import doc_for
    load_docs(con, [doc_for(CIK, {(GAAP, "CashAndCashEquivalentsAtCarryingValue", "USD"):
                                  [inst("2024-09-30", 1, "2024-10-30", "late")]})])


def test_one_transaction_per_family_keeps_a_failed_write_from_the_other(con):
    both_families(con)
    docs = good_docs()
    docs[URLS["r"]] = b"<html>no table</html>"
    run_checks(con, FakeSec(docs), rows(con), AS_OF)
    assert ledger(con) == {"m11_balance_sheet": ("error", None), "m11_cover": ("ok", "classes")}


def test_a_failed_document_is_not_requested_again_by_the_other_family(con):
    both_families(con)
    docs = good_docs()
    docs[URLS["index"]] = requests.ConnectionError("dropped")
    sec = FakeSec(docs)
    run_checks(con, sec, rows(con), AS_OF)
    assert sec.calls == [URLS["index"]]                                 # budget: one try per document per run
    assert ledger(con) == {"m11_balance_sheet": ("error", None), "m11_cover": ("error", None)}


def test_c14_nil_cover_count_does_not_stop_the_balance_sheet_family(con):
    both_families(con)
    docs = good_docs()
    docs[URLS["instance"]] = docs[URLS["instance"]].replace(
        b'<dei:EntityCommonStockSharesOutstanding contextRef="a" unitRef="shares" decimals="INF">1000000</dei:EntityCommonStockSharesOutstanding>',
        b'<dei:EntityCommonStockSharesOutstanding contextRef="a" unitRef="shares" decimals="INF" xsi:nil="true" '
        b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>')
    run_checks(con, FakeSec(docs), rows(con), AS_OF)
    got = ledger(con)
    assert got["m11_balance_sheet"] == ("ok", "debt_zero")
    assert got["m11_cover"][1] != "classes"                          # unusable, never a resolved count
    assert con.execute("SELECT count(*) FROM cover_shares WHERE shares = 0").fetchone()[0] == 0


def test_c18_other_entity_cover_contexts_give_no_cover_evidence(con):
    both_families(con)
    docs = good_docs()
    docs[URLS["instance"]] = (docs[URLS["instance"]]
                              .replace(b'<xbrli:context id="a"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000001234',
                                       b'<xbrli:context id="a"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000009999')
                              .replace(b'<xbrli:context id="b"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000001234',
                                       b'<xbrli:context id="b"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000009999'))
    run_checks(con, FakeSec(docs), rows(con), AS_OF)
    got = ledger(con)
    assert got["m11_cover"][0] == "not_found" and got["m11_balance_sheet"] == ("ok", "debt_zero")
    assert con.execute("SELECT count(*) FROM cover_shares").fetchone()[0] == 0


def test_s2_two_consolidated_balance_sheets_give_a_definite_not_zero(con):
    both_families(con)
    docs = good_docs()
    docs[URLS["fs"]] = filing_summary(("Consolidated Balance Sheets", "R2.htm", "Statements"),
                                      ("Consolidated Balance Sheets - Other", "R9.htm", "Statements"))
    sec = FakeSec(docs)
    run_checks(con, sec, rows(con), AS_OF)
    assert ledger(con)["m11_balance_sheet"] == ("ok", "not_zero")
    assert URLS["r"] not in sec.calls


def test_c20_fetch_uses_the_screens_governing_filing_not_an_amendment(con):
    from src.ingestion.companyfacts import load_docs
    from tests.test_m4_screen import doc_for
    both_families(con)
    load_docs(con, [doc_for(CIK, {(GAAP, "StockholdersEquity", "USD"):
                                  [{**inst("2024-06-30", 9_000_000, "2024-08-25", "q2a"), "form": "10-Q/A"}]})])
    sec = FakeSec(good_docs())
    run_checks(con, sec, rows(con), AS_OF)
    assert sorted(sec.calls) == sorted(URLS.values())                  # q2, the screen's governing 10-Q
    assert {a for (a,) in con.execute("SELECT DISTINCT accn FROM quarantine_checks").fetchall()} == {"q2"}


def test_c20_ledger_gap_means_no_fetch(con):
    both_families(con)
    con.execute("INSERT INTO loaded_filings (cik, accn) VALUES (?, 'ghost')", [CIK])
    sec = FakeSec(good_docs())
    run_checks(con, sec, rows(con), AS_OF)
    assert sec.calls == []
