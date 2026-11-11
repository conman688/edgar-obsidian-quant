"""Noncontrolling interest (NCI) in EV.

EBIT consolidates 100% of subsidiaries, so EV must include the outside owners' share:
EV = mcap + debt - cash + equity NCI + redeemable NCI, each read at the balance-sheet date.
The net-debt filter is unchanged. Base company (tests.test_m4_screen.cheap_company):
mcap 12M, debt 1M, cash 6M, EBIT 3M, so EV 7M and EV/EBIT 2.33 without NCI.
"""
import pytest

from tests.test_failure_modes import CIK
from tests.test_m4_screen import (GAAP, add_buy, add_company, cheap_company, flow, inst, screen_rows,
                                  val_row)
from tests.test_m4_screen import con  # noqa: F401 (fixture)

NCI = (GAAP, "MinorityInterest", "USD")
NONREDEEMABLE = (GAAP, "NonredeemableNoncontrollingInterest", "USD")
REDEEMABLE = (GAAP, "RedeemableNoncontrollingInterestEquityCarryingAmount", "USD")
REDEEMABLE_FV = (GAAP, "RedeemableNoncontrollingInterestEquityFairValue", "USD")


def company_with(con, extra: dict, ticker="ACME"):
    tags = cheap_company()
    tags.update(extra)
    add_company(con, CIK, ticker, tags)
    return val_row(con)


def test_company_without_nci_keeps_its_ev(con):
    row = company_with(con, {})
    assert row["ev"] == 7_000_000 and row["nci"] == 0 and row["nci_redeemable"] == 0
    assert row["issues"] == []


def test_equity_nci_at_balance_sheet_date_adds_to_ev(con):
    row = company_with(con, {NCI: [inst("2024-06-30", 2_000_000)]})
    assert row["nci"] == 2_000_000
    assert row["ev"] == 9_000_000 and row["ev_ebit"] == pytest.approx(3.0)
    assert row["net_cash_to_mcap"] == pytest.approx(5 / 12)        # the debt filter is unchanged
    assert row["issues"] == []
    src = [s for s in row["sources"] if s["item"] == "nci"]
    assert [(s["tag"], str(s["period_end"]), s["accns"]) for s in src] == [("MinorityInterest", "2024-06-30", ["q2"])]


def test_nonredeemable_tag_stands_in_for_minority_interest(con):
    row = company_with(con, {NONREDEEMABLE: [inst("2024-06-30", 1_500_000)]})
    assert row["nci"] == 1_500_000 and row["ev"] == 8_500_000


def test_redeemable_nci_adds_to_ev_separately(con):
    row = company_with(con, {NCI: [inst("2024-06-30", 2_000_000)],
                             REDEEMABLE: [inst("2024-06-30", 1_000_000)]})
    assert row["nci"] == 2_000_000 and row["nci_redeemable"] == 1_000_000
    assert row["ev"] == 10_000_000


def test_redeemable_fair_value_is_the_fallback(con):
    row = company_with(con, {REDEEMABLE_FV: [inst("2024-06-30", 500_000)]})
    assert row["nci_redeemable"] == 500_000 and row["ev"] == 7_500_000


def test_negative_nci_lowers_ev(con):
    row = company_with(con, {NCI: [inst("2024-06-30", -1_000_000)]})
    assert row["nci"] == -1_000_000 and row["ev"] == 6_000_000 and row["issues"] == []


def test_nci_pushes_a_shortlist_name_over_the_ev_ebit_limit(con):
    # IMMR on 2026-11-07: EV/EBIT 2.57 without its 260.2M NCI, 10.63 with it.
    tags = cheap_company()
    tags[NCI] = [inst("2024-06-30", 12_000_000)]                   # EV 19M / EBIT 3M = 6.33
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    row = screen_rows(con)["ACME"]
    assert row["status"] == "fail" and row["failed_filters"] == ["ev_ebit"]
    assert row["ev_ebit"] == pytest.approx(19 / 3)


def test_recent_nci_missing_at_balance_sheet_date_is_stale(con):
    # Reported in the 10-K six months before the newest balance sheet, then not tagged:
    # a missing tag is not a buyout. Not counted as current, and named as an issue.
    row = company_with(con, {NCI: [inst("2023-12-31", 2_000_000, "2024-02-15", "k")]})
    assert "stale_nci" in row["issues"]
    assert row["nci"] == 0 and row["ev"] == 7_000_000


def test_recent_redeemable_nci_missing_at_balance_sheet_date_is_stale(con):
    row = company_with(con, {REDEEMABLE: [inst("2023-12-31", 2_000_000, "2024-02-15", "k")]})
    assert "stale_nci" in row["issues"] and row["nci_redeemable"] == 0


def test_stale_nci_quarantines_a_would_be_shortlist_name(con):
    tags = cheap_company()
    tags[NCI] = [inst("2023-12-31", 2_000_000, "2024-02-15", "k")]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    assert screen_rows(con)["ACME"]["status"] == "quarantine"


def test_stale_zero_nci_is_not_an_issue(con):
    # A line last reported as 0 has nothing missing; any nonzero amount is (review C2).
    row = company_with(con, {NCI: [inst("2023-12-31", 0, "2024-02-15", "k")]})
    assert row["issues"] == [] and row["nci"] == 0


def test_small_stale_nci_near_the_cutoff_quarantines(con):
    # Review C2: 0.5M is 4.2% of the 12M market cap, yet EV/EBIT 5.83 without it, 6.25 with it.
    tags = cheap_company()
    tags[NCI] = [inst("2023-12-31", 500_000, "2024-02-15", "k")]
    tags[(GAAP, "OperatingIncomeLoss", "USD")] = flow(1_200_000)
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    row = screen_rows(con)["ACME"]
    assert "stale_nci" in row["issues"] and row["status"] == "quarantine"


def test_nci_gone_for_over_400_days_is_not_stale(con):
    # A full 10-K has left the line out since (GIII, LRN: NCI last tagged 2014-2017).
    row = company_with(con, {NCI: [inst("2022-12-31", 2_000_000, "2023-02-15", "k22")]})
    assert row["issues"] == [] and row["nci"] == 0 and row["ev"] == 7_000_000


def test_nci_filed_after_as_of_is_not_seen(con):
    row = company_with(con, {NCI: [inst("2024-06-30", 2_000_000, "2024-09-10", "q2late")]})
    assert row["nci"] == 0 and row["ev"] == 7_000_000


RED_COMMON = (GAAP, "RedeemableNoncontrollingInterestEquityCommonCarryingAmount", "USD")
RED_PREFERRED = (GAAP, "RedeemableNoncontrollingInterestEquityPreferredCarryingAmount", "USD")
RED_OTHER = (GAAP, "RedeemableNoncontrollingInterestEquityOtherCarryingAmount", "USD")


def test_redeemable_nci_tagged_only_by_class_is_summed(con):
    # KNTK, MCK on 2026-11-07: redeemable NCI only in class lines, no total.
    row = company_with(con, {RED_COMMON: [inst("2024-06-30", 1_000_000)],
                             RED_PREFERRED: [inst("2024-06-30", 500_000)],
                             RED_OTHER: [inst("2024-06-30", 250_000)]})
    assert row["nci_redeemable"] == 1_750_000 and row["ev"] == 8_750_000


def test_redeemable_total_beats_its_class_lines(con):
    row = company_with(con, {REDEEMABLE: [inst("2024-06-30", 1_000_000)],
                             RED_COMMON: [inst("2024-06-30", 600_000)],
                             RED_PREFERRED: [inst("2024-06-30", 400_000)]})
    assert row["nci_redeemable"] == 1_000_000


def test_recent_redeemable_class_line_missing_at_balance_sheet_date_is_stale(con):
    row = company_with(con, {RED_COMMON: [inst("2023-12-31", 2_000_000, "2024-02-15", "k")]})
    assert "stale_nci" in row["issues"] and row["nci_redeemable"] == 0


def test_stale_redeemable_class_beside_a_current_class_quarantines(con):
    # Review C1: one current class cannot show that another class went away.
    tags = cheap_company()
    tags[RED_COMMON] = [inst("2024-06-30", 1_000_000)]
    tags[RED_PREFERRED] = [inst("2023-12-31", 12_000_000, "2024-02-15", "k")]
    add_company(con, CIK, "ACME", tags)
    add_buy(con, CIK, "f4")
    row = screen_rows(con)["ACME"]
    assert row["nci_redeemable"] == 1_000_000
    assert "stale_nci" in row["issues"] and row["status"] == "quarantine"


def test_current_redeemable_total_covers_a_stale_class(con):
    # The total at the balance-sheet date includes every class, whichever were tagged before.
    row = company_with(con, {REDEEMABLE: [inst("2024-06-30", 3_000_000)],
                             RED_PREFERRED: [inst("2023-12-31", 2_000_000, "2024-02-15", "k")]})
    assert row["nci_redeemable"] == 3_000_000 and row["issues"] == []


def test_redeemable_class_sources_name_every_line_used(con):
    row = company_with(con, {RED_COMMON: [inst("2024-06-30", 1_000_000)],
                             RED_PREFERRED: [inst("2024-06-30", 500_000, "2024-08-02", "q2b")]})
    src = [s for s in row["sources"] if s["item"] == "nci_redeemable"]
    assert [(s["tag"], s["accns"]) for s in src] == [(
        "RedeemableNoncontrollingInterestEquityCommonCarryingAmount+"
        "RedeemableNoncontrollingInterestEquityPreferredCarryingAmount", ["q2", "q2b"])]
