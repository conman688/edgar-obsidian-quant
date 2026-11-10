"""M11 Phase 1: a reason for every quarantine issue, from stored facts only (ROADMAP M11).

Phase 1 changes no status, rank or shortlist. It never fills a value.
"""
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.db.schema import connect
from src.obsidian.sync import sync_vault
from src.quarantine import REASONS
from src.screen import Filters, screen, screen_table, write_parquet
from tests.test_failure_modes import CIK, fact
from tests.test_m4_screen import AS_OF, DEI, GAAP, add_buy, add_company, cheap_company, inst

IFRS = "ifrs-full"


@pytest.fixture
def con():
    return connect(":memory:")


def quarantined(con, cik, ticker, tags):
    add_company(con, cik, ticker, tags)
    add_buy(con, cik, f"f4-{cik}")


def rows_by_ticker(con, as_of=AS_OF):
    return {r["ticker"]: r for r in screen_table(con, as_of).to_pylist()}


def checks(row) -> dict:
    return {c["issue"]: c for c in row["issue_checks"]}


def ten_q_only_flows(tags):
    """EBIT, CFO and capex only from the Q2 10-Q (no annual base anywhere in the facts)."""
    for tag in ("OperatingIncomeLoss", "NetCashProvidedByUsedInOperatingActivities",
                "PaymentsToAcquirePropertyPlantAndEquipment"):
        tags[(GAAP, tag, "USD")] = [fact("2024-01-01", "2024-06-30", 1_000_000, "2024-08-01", "q2", "10-Q", fp="Q2")]
    return tags


# --- tag candidates (debt) ------------------------------------------------------

def test_debt_candidates_are_shown_as_components_and_never_used(con):
    # C1: convertible notes $1M + other notes $9M; neither is a complete total.
    tags = cheap_company(debt=False)
    tags[(GAAP, "ConvertibleNotesPayable", "USD")] = [inst("2024-06-30", 1_000_000)]
    tags[(GAAP, "NotesPayable", "USD")] = [inst("2024-06-30", 9_000_000)]
    tags[(GAAP, "OperatingLeaseLiability", "USD")] = [inst("2024-06-30", 5_000_000)]   # excluded: Lease
    tags[(GAAP, "NotesReceivableNet", "USD")] = [inst("2024-06-30", 7_000_000)]        # excluded: Receivable
    tags[(GAAP, "NotesPayable", "USD")].append(inst("2023-12-31", 8_000_000, "2024-02-15", "k"))  # other period
    quarantined(con, CIK, "ACME", tags)
    r = rows_by_ticker(con)["ACME"]
    assert r["status"] == "quarantine" and r["debt"] is None and "missing_debt" in r["issues"]
    c = checks(r)["missing_debt"]
    assert c["reason"] == "tag_candidates"
    got = sorted((x["tag"], x["value"], x["accn"], x["period_end"], x["kind"]) for x in c["candidates"])
    assert got == [("ConvertibleNotesPayable", 1_000_000, "q2", date(2024, 6, 30), "component"),
                   ("NotesPayable", 9_000_000, "q2", date(2024, 6, 30), "component")]


def test_debt_issue_without_candidates_is_none_found(con):
    quarantined(con, CIK, "ACME", cheap_company(debt=False))
    c = checks(rows_by_ticker(con)["ACME"])["missing_debt"]
    assert c["reason"] == "none_found" and c["candidates"] == []


def test_candidate_filed_after_as_of_is_not_shown(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "NotesPayable", "USD")] = [inst("2024-06-30", 9_000_000, "2024-09-20", "q2a")]
    quarantined(con, CIK, "ACME", tags)
    assert checks(rows_by_ticker(con)["ACME"])["missing_debt"]["reason"] == "none_found"


# --- annual base (TTM) ----------------------------------------------------------

def test_ten_q_only_facts_give_no_annual_in_stored_facts(con):
    quarantined(con, CIK, "NEWCO", ten_q_only_flows(cheap_company()))
    r = rows_by_ticker(con)["NEWCO"]
    assert {"missing_ebit", "missing_fcf"} <= set(r["issues"])
    for issue in ("missing_ebit", "missing_fcf"):
        c = checks(r)[issue]
        assert c["reason"] == "no_annual_in_stored_facts"
        assert "q2" in c["message"]
        low = c["message"].lower()
        assert "listed" not in low and "no annual report was filed" not in low


def test_ledger_accession_without_facts_gives_unknown(con):
    # C4: an accession in the ledger with no facts might be the annual; unknown wins.
    quarantined(con, CIK, "NEWCO", ten_q_only_flows(cheap_company()))
    con.execute("INSERT INTO loaded_filings (cik, accn) VALUES (?, 'ghost')", [CIK])
    c = checks(rows_by_ticker(con)["NEWCO"])["missing_ebit"]
    assert c["reason"] == "unknown" and "ghost" in c["message"]


def test_annual_in_facts_but_ebit_missing_is_none_found(con):
    tags = cheap_company()
    del tags[(GAAP, "OperatingIncomeLoss", "USD")]
    quarantined(con, CIK, "ACME", tags)
    c = checks(rows_by_ticker(con)["ACME"])["missing_ebit"]
    assert c["reason"] == "none_found"


# --- foreign filer --------------------------------------------------------------

def test_foreign_filer_issue_names_the_annual_form(con):
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [
        fact(None, "2024-06-30", 1_000_000, "2024-08-01", "f", form="20-F")]
    quarantined(con, CIK, "TSM", tags)
    c = checks(rows_by_ticker(con)["TSM"])["foreign_filer"]
    assert c["reason"] == "foreign_filer" and "20-F" in c["message"] and "f" in c["message"]


def ifrs_only_company():
    """FMX/GRAB shape: IFRS facts from a 20-F, no us-gaap shares or cash, so not flagged today."""
    tags = cheap_company()
    del tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")]
    del tags[(GAAP, "CashAndCashEquivalentsAtCarryingValue", "USD")]
    return tags


def test_unflagged_twenty_f_filer_gains_foreign_filer(con):
    tags = ifrs_only_company()
    tags[(IFRS, "Revenue", "USD")] = [fact("2023-01-01", "2023-12-31", 50_000_000, "2024-04-30", "f20", "20-F")]
    quarantined(con, CIK, "FMX", tags)
    r = rows_by_ticker(con)["FMX"]
    assert r["status"] == "quarantine"
    assert "foreign_filer" in r["issues"] and checks(r)["foreign_filer"]["reason"] == "foreign_filer"


def test_twenty_f_filed_after_as_of_changes_nothing(con):
    tags = ifrs_only_company()
    tags[(IFRS, "Revenue", "USD")] = [fact("2023-01-01", "2023-12-31", 50_000_000, "2024-09-20", "f20", "20-F")]
    quarantined(con, CIK, "FMX", tags)
    r = rows_by_ticker(con)["FMX"]
    assert "foreign_filer" not in r["issues"] and "foreign_filer" not in checks(r)


def test_ledger_gap_makes_foreign_filer_unknown_and_adds_nothing(con):
    quarantined(con, CIK, "FMX", ifrs_only_company())
    con.execute("INSERT INTO loaded_filings (cik, accn) VALUES (?, 'ghost')", [CIK])
    r = rows_by_ticker(con)["FMX"]
    assert "foreign_filer" not in r["issues"]


# --- cover count ----------------------------------------------------------------

def test_no_dei_cover_count_with_a_ten_q_gives_no_cover_count(con):
    tags = cheap_company(shares_tag=(GAAP, "CommonStockSharesOutstanding"))
    tags[(GAAP, "CommonStockSharesOutstanding", "shares")] = [inst("2023-12-31", 1_000_000, "2024-02-15", "k")]
    quarantined(con, CIK, "MIR", tags)
    r = rows_by_ticker(con)["MIR"]
    assert "stale_shares" in r["issues"] and "multi_class_risk" in r["flags"]
    c = checks(r)["stale_shares"]
    assert c["reason"] == "no_cover_count" and "q2" in c["message"]
    assert "per-class" in c["message"]            # one possible cause, never stated as confirmed
    assert "confirmed" not in c["message"].lower()


# --- invariants -----------------------------------------------------------------

def mixed_universe(con):
    add_company(con, 1001, "GOOD", cheap_company())
    add_buy(con, 1001, "f4-good")
    quarantined(con, 1002, "DEBT", cheap_company(debt=False))
    quarantined(con, 1003, "NEWCO", ten_q_only_flows(cheap_company()))
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [
        fact(None, "2024-06-30", 1_000_000, "2024-08-01", "f", form="20-F")]
    quarantined(con, 1004, "TSM", tags)
    add_company(con, 1005, "NOBUY", cheap_company())          # fails the insider filter


def test_status_rank_and_shortlist_are_unchanged(con):
    mixed_universe(con)
    rel = screen(con, AS_OF, Filters())
    before = sorted((r[0], r[1], r[2]) for r in rel.select("ticker, status, rank").fetchall())
    after = sorted((r["ticker"], r["status"], r["rank"]) for r in screen_table(con, AS_OF).to_pylist())
    assert before == after
    assert {t for t, s, _ in after if s == "shortlist"} == {"GOOD"}


def test_every_quarantine_issue_gets_exactly_one_reason(con):
    mixed_universe(con)
    for r in screen_table(con, AS_OF).to_pylist():
        if r["status"] == "quarantine":
            assert sorted(c["issue"] for c in r["issue_checks"]) == sorted(r["issues"])
            assert all(c["reason"] in REASONS and c["message"] for c in r["issue_checks"])
        else:
            assert r["issue_checks"] == []


def test_parquet_snapshot_keeps_issue_checks(con, tmp_path):
    mixed_universe(con)
    path = write_parquet(screen_table(con, AS_OF), AS_OF, Filters(), tmp_path)
    got = {r["ticker"]: r for r in pq.read_table(path).to_pylist()}
    assert checks(got["DEBT"])["missing_debt"]["reason"] == "none_found"


def test_vault_shows_the_quarantine_check_and_second_sync_writes_nothing(con, tmp_path):
    tags = cheap_company(debt=False)
    tags[(GAAP, "NotesPayable", "USD")] = [inst("2024-06-30", 9_000_000)]
    quarantined(con, CIK, "ACME", tags)
    sync_vault(con, tmp_path, AS_OF)
    note = next(Path(tmp_path, "Companies").glob("*.md")).read_text()
    assert "## Quarantine check" in note
    assert "missing_debt: tag_candidates" in note and "NotesPayable" in note
    assert "issue_checks:" in note                                   # frontmatter, for the Dashboard
    dash = Path(tmp_path, "Dashboard.md").read_text()
    assert "issue_checks" in dash
    assert sum(sync_vault(con, tmp_path, AS_OF).written.values()) == 0


# --- review round 4 (build round 1) ------------------------------------------------

def stale_share_company():
    tags = cheap_company(shares_tag=(GAAP, "CommonStockSharesOutstanding"))
    tags[(GAAP, "CommonStockSharesOutstanding", "shares")] = [inst("2023-12-31", 1_000_000, "2024-02-15", "k")]
    return tags


def test_c7_ledger_gap_makes_share_check_unknown(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    con.execute("INSERT INTO loaded_filings (cik, accn) VALUES (?, 'ghost')", [CIK])
    c = checks(rows_by_ticker(con)["MIR"])["stale_shares"]
    assert c["reason"] == "unknown" and "ghost" in c["message"]
    assert "latest" not in c["message"]


def test_c7_ledger_gap_with_a_dei_count_is_unknown_too(con):
    tags = stale_share_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2023-12-31", 1_000_000, "2024-02-15", "k")]
    quarantined(con, CIK, "MIR", tags)
    con.execute("INSERT INTO loaded_filings (cik, accn) VALUES (?, 'ghost')", [CIK])
    r = rows_by_ticker(con)["MIR"]
    assert "stale_shares" in r["issues"]
    c = checks(r)["stale_shares"]
    assert c["reason"] == "unknown" and "ghost" in c["message"]


def test_c8_debt_candidate_reported_long_before_its_period_is_ignored(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "NotesPayable", "USD")] = [inst("2024-06-30", 9_000_000, "2024-01-01", "q2")]
    quarantined(con, CIK, "ACME", tags)
    c = checks(rows_by_ticker(con)["ACME"])["missing_debt"]
    assert c["reason"] == "none_found" and c["candidates"] == []


def test_c8_debt_candidate_at_the_seven_day_boundary_is_shown(con):
    tags = cheap_company(debt=False)
    tags[(GAAP, "NotesPayable", "USD")] = [inst("2024-06-30", 9_000_000, "2024-06-23", "q2")]
    quarantined(con, CIK, "ACME", tags)
    c = checks(rows_by_ticker(con)["ACME"])["missing_debt"]
    assert c["reason"] == "tag_candidates" and [x["tag"] for x in c["candidates"]] == ["NotesPayable"]


def test_c8_future_dated_dei_count_does_not_hide_missing_cover(con):
    tags = stale_share_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2034-06-30", 1_000_000)]   # filed 2024-08-01
    quarantined(con, CIK, "MIR", tags)
    r = rows_by_ticker(con)["MIR"]
    assert r["shares_period_end"] == date(2023, 12, 31)          # the screen ignores it
    assert checks(r)["stale_shares"]["reason"] == "no_cover_count"


def test_c8_valid_dei_count_at_the_boundary_is_used_by_the_screen(con):
    tags = stale_share_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2024-08-08", 1_000_000)]  # filed + 7 days
    quarantined(con, CIK, "MIR", tags)
    r = rows_by_ticker(con)["MIR"]
    assert "stale_shares" not in r["issues"] and "missing_shares" not in r["issues"]


# --- review round 5: C9, assets and undrawn capacity are not debt candidates -----------

NON_DEBT = ["AvailableForSaleSecuritiesDebtSecurities",                       # Codex's case: securities owned
            "LineOfCreditFacilityCurrentBorrowingCapacity",                   # Codex's case: still available to borrow
            "AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue",  # CAVA, 11-07 backup
            "DebtSecuritiesAvailableForSaleExcludingAccruedInterest",         # CAVA
            "AvailableForSaleDebtSecuritiesAmortizedCostBasis",               # HUBS, CXM
            "LineOfCreditFacilityRemainingBorrowingCapacity"]
DEBT = ["ConvertibleNotesPayable", "NotesPayable", "LongTermLineOfCredit",
        "LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear"]   # maturities stay: real debt evidence


def test_c9_assets_and_capacity_are_not_candidates_debt_lines_are(con):
    tags = cheap_company(debt=False)
    for t in NON_DEBT + DEBT:
        tags[(GAAP, t, "USD")] = [inst("2024-06-30", 1_000_000)]
    quarantined(con, CIK, "ACME", tags)
    c = checks(rows_by_ticker(con)["ACME"])["missing_debt"]
    assert c["reason"] == "tag_candidates"
    assert sorted(x["tag"] for x in c["candidates"]) == sorted(DEBT)
    assert all(x["kind"] == "component" for x in c["candidates"])


def test_c9_only_non_debt_facts_give_none_found(con):
    tags = cheap_company(debt=False)
    for t in NON_DEBT:
        tags[(GAAP, t, "USD")] = [inst("2024-06-30", 1_000_000)]
    quarantined(con, CIK, "ACME", tags)
    c = checks(rows_by_ticker(con)["ACME"])["missing_debt"]
    assert c["reason"] == "none_found" and c["candidates"] == []
