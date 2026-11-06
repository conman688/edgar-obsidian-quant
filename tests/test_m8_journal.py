"""M8 decision journal: a frozen snapshot per decision, then-vs-now on the dashboard."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.db.schema import connect
from src.obsidian.sync import sync_vault, write_journal
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company

LATER = date(2024, 12, 2)


@pytest.fixture
def con():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())
    add_buy(c, CIK, "0001234-24-000001")
    return c


def fm(path: Path) -> dict:
    return dict(split_note(path.read_text())[0])


def move_on(con):
    """After the decision: a 2-for-1 split, a dividend and a new price, all known by LATER."""
    con.execute("INSERT INTO splits VALUES (?, DATE '2024-10-01', 2.0, 't')", [CIK])
    con.execute("INSERT INTO dividends VALUES (?, DATE '2024-11-01', 0.5, 't')", [CIK])
    con.execute("INSERT INTO prices (cik, ticker, price_date, close_raw, source) "
                "VALUES (?, 'ACME', DATE '2024-11-29', 9.0, 't')", [CIK])


def test_journal_note_has_the_template_and_the_screen_snapshot(con, tmp_path):
    path = write_journal(con, tmp_path, "acme", AS_OF)
    assert path == tmp_path / "Journal" / f"ACME-{AS_OF}.md"
    meta = fm(path)
    assert meta["type"] == "journal" and meta["ticker"] == "ACME" and meta["cik"] == CIK
    assert meta["decided"] == AS_OF and meta["review_date"] == AS_OF + timedelta(days=90)
    snap = meta["snapshot"]
    assert snap["status"] == "shortlist" and snap["rank"] == 1
    assert snap["ev_ebit"] == pytest.approx(7 / 3, rel=1e-3)
    assert snap["close_raw"] == 12.0 and snap["price_date"] == date(2024, 8, 30)
    assert snap["mcap"] == 12_000_000
    body = path.read_text()
    for heading in ("## Thesis", "## What the market believes", "## Why it is wrong", "## Kill criteria",
                    "## Position size", "## Review", "## Snapshot on"):
        assert heading in body
    assert "[[ACME]]" in body


def test_review_date_follows_review_days(con, tmp_path):
    meta = fm(write_journal(con, tmp_path, "ACME", AS_OF, review_days=30))
    assert meta["review_date"] == AS_OF + timedelta(days=30)


def test_snapshot_does_not_change_when_the_data_later_updates(con, tmp_path):
    path = write_journal(con, tmp_path, "ACME", AS_OF)
    path.write_text(path.read_text().replace("## Thesis\n", "## Thesis\n\nRefinery margins mean-revert.\n"))
    before = path.read_bytes()
    move_on(con)
    con.execute("UPDATE prices SET close_raw = 29 WHERE cik = ? AND price_date = DATE '2024-08-30'", [CIK])
    sync_vault(con, tmp_path, AS_OF)
    sync_vault(con, tmp_path, LATER)
    with pytest.raises(FileExistsError):
        write_journal(con, tmp_path, "ACME", AS_OF)        # same decision date: never overwritten
    assert path.read_bytes() == before
    again = write_journal(con, tmp_path, "ACME", LATER)    # a new decision is a new note
    assert again != path and fm(again)["snapshot"]["close_raw"] == 9.0


def test_unknown_ticker_is_refused(con, tmp_path):
    with pytest.raises(LookupError, match="NOPE"):
        write_journal(con, tmp_path, "NOPE", AS_OF)
    assert not list(tmp_path.rglob("*.md"))


def test_dashboard_compares_then_and_now_split_adjusted_with_dividends(con, tmp_path):
    write_journal(con, tmp_path, "ACME", AS_OF)
    move_on(con)
    sync_vault(con, tmp_path, LATER)
    dash = (tmp_path / "Dashboard.md").read_text()
    section = dash[dash.index("## Decisions"):]
    row = next(line for line in section.splitlines() if f"[[ACME-{AS_OF}]]" in line)
    # Entry 12 on the old basis = 6 on today's (2-for-1); now 9, plus 0.50 dividend.
    assert "+50.0%" in row          # price change: 9 / 6 - 1
    assert "+58.3%" in row          # total return: (9 + 0.5) / 6 - 1
    assert "shortlist" in row and "2.3" in row


def test_dashboard_leaves_out_decisions_after_the_sync_date(con, tmp_path):
    move_on(con)
    write_journal(con, tmp_path, "ACME", LATER)
    sync_vault(con, tmp_path, AS_OF)
    dash = (tmp_path / "Dashboard.md").read_text()
    assert f"[[ACME-{LATER}]]" not in dash
    assert "No decisions recorded on or before" in dash


def test_journaled_company_keeps_a_company_note_after_it_leaves_the_screen(con, tmp_path):
    con.execute("UPDATE prices SET close_raw = 29 WHERE cik = ?", [CIK])      # EV/EBIT 8: fails
    write_journal(con, tmp_path, "ACME", AS_OF)
    sync_vault(con, tmp_path, AS_OF)
    assert fm(tmp_path / "Companies" / "ACME.md")["status"] == "fail"


def test_sync_never_rewrites_journal_notes_and_repeats_write_nothing(con, tmp_path):
    path = write_journal(con, tmp_path, "ACME", AS_OF)
    before = path.read_bytes()
    move_on(con)
    first = sync_vault(con, tmp_path, LATER)
    second = sync_vault(con, tmp_path, LATER)
    assert path.read_bytes() == before
    assert "Journal" not in first.written
    assert sum(second.written.values()) == 0


def test_journal_without_a_snapshot_shows_dashes_not_an_error(con, tmp_path):
    path = write_journal(con, tmp_path, "ACME", AS_OF)
    text = path.read_text()
    path.write_text(text[:text.index("snapshot:")] + text[text.index("tags:"):])   # user deleted it
    sync_vault(con, tmp_path, AS_OF)
    row = next(line for line in (tmp_path / "Dashboard.md").read_text().splitlines() if f"[[ACME-{AS_OF}]]" in line)
    assert "- → shortlist" in row and row.rstrip().endswith("| - | - |")


def test_snapshot_close_drops_float_noise(con, tmp_path):
    con.execute("UPDATE prices SET close_raw = 88.6500015258789 WHERE cik = ?", [CIK])   # float32 in the source
    assert fm(write_journal(con, tmp_path, "ACME", AS_OF))["snapshot"]["close_raw"] == 88.65
