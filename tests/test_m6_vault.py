"""M6 vault sync: writer round-trips user frontmatter, notes keyed by CIK, idempotent sync."""
from datetime import date
from pathlib import Path

import pytest

from src.obsidian.writer import upsert_note


def test_user_frontmatter_comments_and_order_survive_a_sync(tmp_path: Path):
    p = tmp_path / "ACME.md"
    upsert_note(p, {"ticker": "ACME", "ev_ebit": 5.1}, "auto v1")
    text = p.read_text().replace("ticker: ACME", "ticker: ACME\n# my rating, 1-5\nconviction: 4  # after the call")
    p.write_text(text)
    assert upsert_note(p, {"ticker": "ACME", "ev_ebit": 4.2}, "auto v2")
    out = p.read_text()
    assert "# my rating, 1-5" in out and "conviction: 4  # after the call" in out
    assert "ev_ebit: 4.2" in out
    assert not upsert_note(p, {"ticker": "ACME", "ev_ebit": 4.2}, "auto v2")   # unchanged: no write


# --- sync -------------------------------------------------------------------------

from src.db.schema import connect                                   # noqa: E402
from src.ingestion.form4_insider import parse_form4, store           # noqa: E402
from src.obsidian.sync import sync_vault                             # noqa: E402
from src.obsidian.writer import split_note                           # noqa: E402
from tests.test_failure_modes import CIK, form4_xml                  # noqa: E402
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company   # noqa: E402


@pytest.fixture
def con():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())
    add_buy(c, CIK, "0001234-24-000001")
    return c


def fm(path: Path) -> dict:
    return dict(split_note(path.read_text())[0])


def test_first_sync_writes_every_kind_of_note_and_second_writes_none(con, tmp_path):
    first = sync_vault(con, tmp_path, AS_OF)
    assert first.written["Companies"] == 1 and first.written["Executives"] == 1
    assert first.written["Events"] == 1 and first.written["Filings"] >= 2 and first.written["Dashboard"] == 1
    company = fm(tmp_path / "Companies" / "ACME.md")
    for key in ("ticker", "cik", "ev_ebit", "net_cash_to_mcap", "share_cagr_3y", "insider_buy_90d_usd",
                "drawdown_52w", "as_of"):
        assert key in company
    assert company["status"] == "shortlist" and company["ev_ebit"] == pytest.approx(7 / 3, rel=1e-3)
    assert (tmp_path / "Journal").is_dir()
    second = sync_vault(con, tmp_path, AS_OF)
    assert sum(second.written.values()) == 0 and sum(second.unchanged.values()) == sum(first.written.values())


def test_hand_edits_survive_a_resync_with_new_data(con, tmp_path):
    sync_vault(con, tmp_path, AS_OF)
    p = tmp_path / "Companies" / "ACME.md"
    p.write_text(p.read_text().replace("ticker: ACME", "ticker: ACME\nconviction: high  # my call")
                 + "\nMy thesis: the market hates it.\n")
    con.execute("UPDATE prices SET close_raw = 11 WHERE cik = ? AND price_date = DATE '2024-08-30'", [CIK])
    assert sync_vault(con, tmp_path, AS_OF).written["Companies"] == 1
    text = p.read_text()
    assert "My thesis: the market hates it." in text and "conviction: high  # my call" in text
    assert fm(p)["ev_ebit"] == pytest.approx(6 / 3, rel=1e-3)        # mcap 11M + 1M debt - 6M cash = 6M


def test_three_name_variants_of_one_filer_make_one_executive_note(con, tmp_path):
    for i, name in enumerate(["SMITH JOHN A", "Smith John A.", "John A. Smith"]):
        xml = form4_xml(77, name, "Chief Financial Officer",
                        [(f"2024-08-1{i}", "P", 5000, 12.0, "A", 5000 * (i + 1), "D", "", False)], doc_flag=0)
        store(con, parse_form4(xml, f"0001234-24-00010{i}", date(2024, 8, 12 + i)))
    sync_vault(con, tmp_path, AS_OF)
    notes = [p for p in (tmp_path / "Executives").glob("*.md") if fm(p)["cik"] == 77]
    assert len(notes) == 1
    assert {"SMITH JOHN A", "Smith John A."} <= set(fm(notes[0])["aliases"])
    assert len(list((tmp_path / "Events").glob("*.md"))) == 4                # 3 filings by 77 + 1 by the CEO


def test_company_note_is_found_by_cik_after_the_user_renames_it(con, tmp_path):
    sync_vault(con, tmp_path, AS_OF)
    (tmp_path / "Companies" / "ACME.md").rename(tmp_path / "Companies" / "Acme Corp (watch).md")
    stats = sync_vault(con, tmp_path, AS_OF)
    assert stats.written["Companies"] == 0                                    # same note, unchanged
    assert [p.name for p in (tmp_path / "Companies").glob("*.md")] == ["Acme Corp (watch).md"]
    event = next((tmp_path / "Events").glob("*.md"))
    assert fm(event)["company"] == "[[Acme Corp (watch)]]"                    # links follow the rename
    assert sum(sync_vault(con, tmp_path, AS_OF).written.values()) == 0


def test_company_that_leaves_the_shortlist_keeps_its_note_and_is_updated(con, tmp_path):
    sync_vault(con, tmp_path, AS_OF)
    con.execute("UPDATE prices SET close_raw = 29 WHERE cik = ?", [CIK])    # EV/EBIT 8: fails
    sync_vault(con, tmp_path, AS_OF)
    assert fm(tmp_path / "Companies" / "ACME.md")["status"] == "fail"


def test_dashboard_has_the_three_dataview_tables(con, tmp_path):
    sync_vault(con, tmp_path, AS_OF)
    text = (tmp_path / "Dashboard.md").read_text()
    assert text.count("```dataview") == 4                                    # shortlist, quarantine, buys, reviews
    assert 'FROM "Companies"' in text and 'FROM "Events"' in text and 'FROM "Journal"' in text


@pytest.mark.parametrize("sec_name, shown", [
    ("SMITH JOHN A", "John A Smith"),
    ("Oliver George", "George Oliver"),                       # data sets: mixed case, still Last First
    ("Perdue George Ervin III", "George Ervin Perdue III"),   # suffix stays last and upper case
    ("DOE JANE JR", "Jane Doe Jr"),
    ("ACME CAPITAL PARTNERS LP", "Acme Capital Partners LP"),  # an entity: not reordered
])
def test_form4_owner_names_read_first_name_first(sec_name, shown):
    from src.obsidian.writer import PersonRegistry
    assert PersonRegistry.display_name(sec_name) == shown


def test_owner_without_a_name_gets_a_cik_name(tmp_path):
    from src.obsidian.writer import PersonRegistry
    assert PersonRegistry(connect(":memory:")).register(42, "", 1234) == "Owner 42"
