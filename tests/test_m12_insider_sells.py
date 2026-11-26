"""M12-A1: insider sells, option exercises and gifts from Form 4 rows already stored.

Informational only: the new columns and `signals` never change status, rank or the
shortlist (ROADMAP M12 ground rules).
"""
from datetime import date
from pathlib import Path

import pytest

from src.db.queries import insider_sells
from src.db.schema import connect
from src.ingestion.form4_insider import parse_form4, store
from src.obsidian.sync import sync_vault
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK, form4_xml
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company, screen_rows


@pytest.fixture
def con():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())
    return c


def add_txn(con, accn, owner, d, code="S", shares=10_000, price=12.0, ad="D", filed=None, plan=0,
            footnote="", fn=False, issuer=CIK, name=None):
    """One Form 4 by `owner` with one non-derivative row. plan: the 10b5-1 checkbox (None = absent)."""
    xml = form4_xml(owner, name or f"OWNER {owner}", "Chief Operating Officer",
                    [(d, code, shares, price, ad, 50_000, "D", "", fn)], doc_flag=plan, footnote=footnote)
    xml = xml.replace(f"<issuerCik>{CIK}<".encode(), f"<issuerCik>{issuer}<".encode())
    store(con, parse_form4(xml, accn, filed or date.fromisoformat(d)))


def row(con, as_of=AS_OF):
    return screen_rows(con, as_of)["ACME"]


def sells(con, as_of, window_days=90):
    rel = insider_sells(con, as_of, window_days)
    return [dict(zip(rel.columns, r)) for r in rel.fetchall()]


# --- per-sale classification --------------------------------------------------------

def test_discretionary_sell_is_counted(con):
    add_txn(con, "s1", 501, "2024-08-20")
    r = row(con)
    assert r["insider_sell_90d_usd"] == pytest.approx(120_000)
    assert r["n_sellers"] == 1 and r["sellers"] == ["OWNER 501"]
    assert r["form4_sell_accns"] == ["s1"]
    assert r["signals"] == []


def test_plan_sell_is_planned_not_discretionary(con):
    add_txn(con, "s1", 501, "2024-08-20", plan=1)
    add_txn(con, "s2", 502, "2024-08-21", plan=0, fn=True, footnote="Sold under a Rule 10b5-1 plan.")
    r = row(con)
    assert r["insider_sell_planned_90d_usd"] == pytest.approx(240_000)
    assert r["insider_sell_90d_usd"] is None and r["n_sellers"] is None


def test_missing_plan_box_after_april_2023_is_unknown(con):
    add_txn(con, "s1", 501, "2024-08-20", plan=None)
    r = row(con)
    assert r["insider_sell_unknown_plan_90d_usd"] == pytest.approx(120_000)
    assert r["insider_sell_90d_usd"] is None


def test_missing_plan_box_before_april_2023_is_discretionary(con):
    add_txn(con, "s1", 501, "2022-08-20", plan=None)
    [s] = sells(con, date(2022, 9, 1))
    assert s["plan"] == "discretionary" and not s["routine"]


def test_routine_seller_is_not_discretionary(con):
    # Cohen, Malloy and Pomorski (2012): sold in the same calendar month in each of the 3 prior years.
    for y in (2021, 2022, 2023):
        add_txn(con, f"r{y}", 501, f"{y}-08-10", plan=None if y < 2023 else 0)
    add_txn(con, "s1", 501, "2024-08-20")
    r = row(con)
    assert r["insider_sell_routine_90d_usd"] == pytest.approx(120_000)
    assert r["insider_sell_90d_usd"] is None and r["n_sellers"] is None
    [s] = [s for s in sells(con, AS_OF) if s["accn"] == "s1"]
    assert s["routine"] and s["plan"] == "discretionary"


def test_routine_history_reaches_three_years_before_the_window_start(con):
    # The history scan is bounded for speed; a year-long window still sees all 3 prior years.
    for y in (2021, 2022, 2023):
        add_txn(con, f"r{y}", 501, f"{y}-01-05", plan=None if y < 2023 else 0)
    add_txn(con, "s1", 501, "2024-01-20")
    [s] = [s for s in sells(con, date(2025, 1, 10), window_days=365) if s["accn"] == "s1"]
    assert s["routine"]


def test_a_gap_year_breaks_the_routine(con):
    for y in (2021, 2023):
        add_txn(con, f"r{y}", 501, f"{y}-08-10", plan=None if y < 2023 else 0)
    add_txn(con, "s1", 501, "2024-08-20")
    assert row(con)["insider_sell_90d_usd"] == pytest.approx(120_000)


def test_routine_history_counts_only_this_issuer(con):
    for y in (2021, 2022, 2023):
        add_txn(con, f"r{y}", 501, f"{y}-08-10", plan=None if y < 2023 else 0, issuer=9999)
    add_txn(con, "s1", 501, "2024-08-20")
    assert row(con)["insider_sell_90d_usd"] == pytest.approx(120_000)


def test_routine_history_uses_the_same_sale_rules(con):
    # Review A1 early finding (Codex): offering sales in prior years must not make a routine.
    for y in (2021, 2022, 2023):
        add_txn(con, f"r{y}", 501, f"{y}-08-10", plan=None if y < 2023 else 0, fn=True,
                footnote="Shares sold in an underwritten public offering.")
    add_txn(con, "s1", 501, "2024-08-20")
    [s] = sells(con, AS_OF)
    assert s["accn"] == "s1" and not s["routine"]
    assert row(con)["insider_sell_90d_usd"] == pytest.approx(120_000)


def test_offering_sales_and_price_typos_are_not_sells(con):
    add_txn(con, "s1", 501, "2024-08-20", fn=True, footnote="Shares sold in an underwritten public offering.")
    add_txn(con, "s2", 502, "2024-08-15", price=1_200.0)           # total in the price field (close 12)
    add_txn(con, "s3", 503, "2024-08-20", ad="A")                  # code S must dispose
    add_txn(con, "s4", 504, "2024-08-25", filed=date(2024, 8, 20)) # trade dated after its filing
    r = row(con)
    assert r["insider_sell_90d_usd"] is None and r["insider_sell_planned_90d_usd"] is None
    assert r["insider_sell_unknown_plan_90d_usd"] is None


# --- point in time ------------------------------------------------------------------

def test_sell_filed_after_as_of_is_never_read(con):
    add_txn(con, "s1", 501, "2024-08-30", filed=date(2024, 9, 4))
    assert row(con)["insider_sell_90d_usd"] is None
    assert sells(con, AS_OF) == []
    assert row(con, date(2024, 9, 4))["insider_sell_90d_usd"] == pytest.approx(120_000)


def test_sell_outside_the_window_is_not_read(con):
    add_txn(con, "s1", 501, "2024-05-01")
    assert row(con)["insider_sell_90d_usd"] is None


# --- per-issuer signals -------------------------------------------------------------

def test_two_discretionary_sellers_make_a_cluster_sell(con):
    add_txn(con, "s1", 501, "2024-08-20")
    add_txn(con, "s2", 502, "2024-08-21", shares=5_000)
    r = row(con)
    assert r["n_sellers"] == 2 and r["insider_sell_90d_usd"] == pytest.approx(180_000)
    assert r["signals"] == ["cluster_sell"]


def test_buy_while_a_peer_sells(con):
    add_buy(con, CIK, "b1")                                         # CEO buys 10,000 at 12 on 08-15
    add_txn(con, "s1", 501, "2024-08-20", shares=5_000)
    r = row(con)
    assert r["signals"] == ["buy_while_peers_sell"]
    assert r["net_insider_flow_90d_usd"] == pytest.approx(120_000 - 60_000)


def test_buyer_selling_their_own_shares_is_not_a_peer(con):
    add_buy(con, CIK, "b1")
    add_txn(con, "s1", CIK * 10, "2024-08-20", shares=5_000, name=f"BUYER {CIK}")
    assert row(con)["signals"] == []


def test_net_flow_counts_buys_below_the_cluster_bar(con):
    add_txn(con, "b1", 601, "2024-08-15", code="P", shares=1_000, ad="A")   # $12k: no cluster buy
    add_txn(con, "s1", 501, "2024-08-20", shares=500)
    r = row(con)
    assert r["insider_buy_90d_usd"] is None
    assert r["net_insider_flow_90d_usd"] == pytest.approx(12_000 - 6_000)


def test_exercises_and_gifts_are_context_only(con):
    add_txn(con, "m1", 501, "2024-08-20", code="M", ad="A", price=3.0)
    add_txn(con, "g1", 502, "2024-08-20", code="G", price=0.0)
    add_txn(con, "f1", 503, "2024-08-20", code="F")                 # tax withholding: not counted
    r = row(con)
    assert r["n_exercisers"] == 1 and r["n_gifters"] == 1
    assert r["insider_sell_90d_usd"] is None and r["signals"] == []


def test_sells_never_change_status_rank_or_shortlist():
    def build(with_sells):
        c = connect(":memory:")
        add_company(c, CIK, "ACME", cheap_company())
        add_company(c, CIK + 1, "BETA", cheap_company())
        add_buy(c, CIK, "b1")
        add_buy(c, CIK + 1, "b2")
        if with_sells:
            for i, owner in enumerate((501, 502, 503)):
                add_txn(c, f"s{i}", owner, "2024-08-20", shares=900_000)
        return {t: (r["status"], r["rank"], r["failed_filters"], r["issues"], r["flags"])
                for t, r in screen_rows(c).items()}
    assert build(True) == build(False)


# --- vault --------------------------------------------------------------------------

def test_company_note_lists_sells_and_a_second_sync_writes_nothing(con, tmp_path: Path):
    add_buy(con, CIK, "b1")
    add_txn(con, "0001234-24-000501", 501, "2024-08-20", shares=5_000)
    add_txn(con, "0001234-24-000502", 502, "2024-08-21", shares=1_000, plan=1)
    sync_vault(con, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    meta = dict(split_note(note.read_text())[0])
    assert meta["insider_sell_90d_usd"] == 60_000
    assert meta["net_insider_flow_90d_usd"] == 60_000
    assert meta["signals"] == ["buy_while_peers_sell"]
    body = note.read_text()
    assert "## Insider sells, last 12 months" in body
    assert "| 2024-08-20 | OWNER 501 | 5,000 | 12.00 | $60k | discretionary |" in body
    assert "| 2024-08-21 | OWNER 502 | 1,000 | 12.00 | $12k | 10b5-1 plan |" in body
    assert "000123424000501" in body                                # EDGAR link to the filing
    assert sum(sync_vault(con, tmp_path, AS_OF).written.values()) == 0
