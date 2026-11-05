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


# --- review M6 round 1 (Codex C1-C3) -------------------------------------------------

def test_company_fallback_collision_preserves_existing_note(con, tmp_path):        # C1
    folder = tmp_path / "Companies"
    folder.mkdir()
    (folder / "ACME.md").write_text("---\ncik: 42\n---\nfirst\n")
    other = folder / f"ACME ({CIK}).md"
    original = "---\ncik: 99\nstatus: personal\n---\nuser content\n"
    other.write_text(original)
    sync_vault(con, tmp_path, AS_OF)
    assert other.read_text() == original
    assert (folder / "ACME.md").read_text() == "---\ncik: 42\n---\nfirst\n"
    assert [p for p in folder.glob("*.md") if fm(p).get("cik") == CIK]



def test_renamed_executive_does_not_duplicate_event(con, tmp_path):                # C2
    sync_vault(con, tmp_path, AS_OF)
    person = next((tmp_path / "Executives").glob("*.md"))
    person.rename(person.with_name("Renamed.md"))
    sync_vault(con, tmp_path, AS_OF)
    events = list((tmp_path / "Events").glob("*.md"))
    assert len(events) == 1 and fm(events[0])["insider"] == "[[Renamed]]"
    assert not sum(sync_vault(con, tmp_path, AS_OF).written.values())


def test_renamed_event_note_is_updated_not_duplicated(con, tmp_path):             # C2, same cause
    sync_vault(con, tmp_path, AS_OF)
    event = next((tmp_path / "Events").glob("*.md"))
    event.rename(event.with_name("My buy note.md"))
    sync_vault(con, tmp_path, AS_OF)
    assert [p.name for p in (tmp_path / "Events").glob("*.md")] == ["My buy note.md"]
    assert "[[My buy note]]" in next((tmp_path / "Companies").glob("*.md")).read_text()


def test_long_name_does_not_merge_distinct_filing_events(con, tmp_path):          # C3
    name = "LAST " + "A" * 110
    xml = form4_xml(77, name, "CEO",
                    [("2024-08-15", "P", 10000, 12, "A", 50000, "D", "", False)], doc_flag=0)
    for accn in ("0001234-24-000101", "0001234-24-000102"):
        store(con, parse_form4(xml, accn, date(2024, 8, 16)))
    sync_vault(con, tmp_path, AS_OF)
    events = [p for p in (tmp_path / "Events").glob("*.md") if fm(p).get("owner_cik") == 77]
    assert len(events) == 2
    assert all(fm(p)["accn"] in p.stem for p in events)


def test_renamed_filing_is_updated_and_links_follow(con, tmp_path):                 # C4
    sync_vault(con, tmp_path, AS_OF)
    folder = tmp_path / "Filings"
    accn = "0001234-24-000001"
    (folder / f"{accn}.md").rename(folder / "My filing.md")
    sync_vault(con, tmp_path, AS_OF)
    assert [p.name for p in folder.glob("*.md") if fm(p).get("accn") == accn] == ["My filing.md"]
    assert "[[My filing]]" in next((tmp_path / "Events").glob("*.md")).read_text()


def test_renamed_source_filing_is_linked_from_the_company(con, tmp_path):           # C4, sources
    sync_vault(con, tmp_path, AS_OF)
    folder = tmp_path / "Filings"
    src = next(p for p in sorted(folder.glob("*.md")) if fm(p)["form"] != "4")
    accn = fm(src)["accn"]
    src.rename(folder / "My 10-K.md")
    sync_vault(con, tmp_path, AS_OF)
    assert [p.name for p in folder.glob("*.md") if fm(p).get("accn") == accn] == ["My 10-K.md"]
    company = (tmp_path / "Companies" / "ACME.md").read_text()
    assert "[[My 10-K]]" in company and f"[[{accn}]]" not in company
    assert not sum(sync_vault(con, tmp_path, AS_OF).written.values())
