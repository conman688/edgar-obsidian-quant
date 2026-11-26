"""M12-A2: 8-K red-flag events from the submissions.zip the weekly run already downloads.

Known-at date is the SEC filing date. Informational only: `signals` never change status,
rank or the shortlist. Item values below are live SEC values pinned by Codex on 2026-11-25
(LINE.md, m12-a2-sec-8k-items-sample.json): legacy numbering before 2004-08-23 ("12,7"), the
dotted items after, and an acceptance time the evening before its filing date.
"""
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from src.db.schema import connect
from src.ingestion import reference
from src.ingestion.events_8k import load_8k_zip
from src.obsidian.sync import sync_vault
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company, screen_rows

SHA = "a" * 64


def arrays(rows):
    """SEC column arrays from (accn, form, filed, accepted, report_date, items) rows."""
    keys = ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "reportDate", "items")
    out = {k: [r[i] for r in rows] for i, k in enumerate(keys)}
    out["primaryDocument"] = [f"{r[0]}.htm" for r in rows]
    return out


def make_zip(path: Path, companies: dict) -> Path:
    """companies: cik -> (recent rows, [paged rows, ...], sic). Writes the SEC file layout."""
    with zipfile.ZipFile(path, "w") as z:
        for cik, (recent, pages, sic) in companies.items():
            files = [{"name": f"CIK{cik:010d}-submissions-{i:03d}.json"} for i in range(1, len(pages) + 1)]
            doc = {"cik": str(cik), "name": f"Co {cik}", "sic": sic, "tickers": [], "exchanges": [],
                   "filings": {"recent": arrays(recent), "files": files}}
            z.writestr(f"CIK{cik:010d}.json", json.dumps(doc))
            for f, rows in zip(files, pages):
                z.writestr(f["name"], json.dumps(arrays(rows)))
    return path


def ev(accn, filed, items, form="8-K", accepted=None):
    return (accn, form, filed, accepted or f"{filed}T20:00:00.000Z", filed, items)


@pytest.fixture
def con():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())
    return c


def load(con, tmp_path, recent, pages=(), sic="3571", cik=CIK, sha=SHA):
    path = make_zip(tmp_path / f"submissions-{sha[:4]}.zip", {cik: (recent, list(pages), sic)})
    return load_8k_zip(con, path, sha)


def signals(con, as_of=AS_OF):
    return screen_rows(con, as_of)["ACME"]["signals"]


# --- ingest -------------------------------------------------------------------------

def test_loads_recent_and_paged_8ks_with_raw_items(con, tmp_path):
    n = load(con, tmp_path,
             recent=[ev("a-26", "2024-06-03", "8.01,9.01"), ("k-24", "10-K", "2024-02-15", "", "", "")],
             pages=[[ev("a-04", "2004-07-14", "12,7"), ev("a-06", "2006-08-11", "2.02,3.01,9.01")],
                    [ev("a-12", "2012-07-02", "2.06,9.01", form="8-K/A")]])
    assert n == 4
    rows = con.sql("SELECT accn, form, filed, items FROM filings_8k ORDER BY filed").fetchall()
    assert rows == [("a-04", "8-K", date(2004, 7, 14), "12,7"), ("a-06", "8-K", date(2006, 8, 11), "2.02,3.01,9.01"),
                    ("a-12", "8-K/A", date(2012, 7, 2), "2.06,9.01"), ("a-26", "8-K", date(2024, 6, 3), "8.01,9.01")]


def test_blank_items_are_unknown_not_empty(con, tmp_path):
    load(con, tmp_path, recent=[ev("a1", "2024-06-03", "")])
    assert con.sql("SELECT items FROM filings_8k").fetchall() == [(None,)]


def test_acceptance_time_is_kept_beside_the_filing_date(con, tmp_path):
    load(con, tmp_path, recent=[ev("0001193125-08-220563", "2008-10-31", "3.02,3.03,5.02,5.03,9.01",
                                   accepted="2008-10-30T23:31:03.000Z")])
    [(filed, accepted)] = con.sql("SELECT filed, accepted FROM filings_8k").fetchall()
    assert filed == date(2008, 10, 31) and str(accepted).startswith("2008-10-30 23:31:03")


def test_replay_inserts_nothing_and_a_new_snapshot_adds_only_new_filings(con, tmp_path):
    rows = [ev("a1", "2024-06-03", "4.02")]
    assert load(con, tmp_path, rows) == 1
    assert load(con, tmp_path, rows) == 0                                    # same snapshot: ledger
    assert load(con, tmp_path, rows + [ev("a2", "2024-07-01", "4.01")], sha="b" * 64) == 1
    assert con.sql("SELECT count(*) FROM filings_8k").fetchall() == [(2,)]


def test_individuals_without_sic_or_ticker_are_skipped(con, tmp_path):
    assert load(con, tmp_path, [ev("a1", "2024-06-03", "4.02")], sic="") == 0


def test_companies_ingest_reruns_until_8ks_are_loaded_for_the_snapshot(con, tmp_path):
    path = make_zip(tmp_path / "s.zip", {CIK: ([ev("a1", "2024-06-03", "4.02")], [], "3571")})
    reference.load_companies_zip(con, path, SHA)
    assert not reference.is_loaded(con, SHA)        # profiles done, 8-Ks not yet: download again
    load_8k_zip(con, path, SHA)
    assert reference.is_loaded(con, SHA)


# --- screen signals -----------------------------------------------------------------

@pytest.mark.parametrize("items,signal", [
    ("4.02,9.01", "8k_non_reliance"), ("4.01,9.01", "8k_auditor_change"), ("2.06", "8k_impairment"),
    ("3.01,9.01", "8k_listing_notice"), ("5.02", "8k_officer_change"),
])
def test_each_red_flag_item_is_a_signal(con, tmp_path, items, signal):
    load(con, tmp_path, [ev("a1", "2024-08-01", items)])
    assert signals(con) == [signal]


def test_items_match_whole_tokens_only(con, tmp_path):
    load(con, tmp_path, [ev("a1", "2024-08-01", "2.02,7.01,9.01"), ev("a2", "2024-08-02", "4.0")])
    assert signals(con) == []


def test_legacy_item_numbers_are_not_read_as_modern_items(con, tmp_path):
    # Before 2004-08-23 Item 4 was the auditor change; it is not coerced into 4.01.
    from src.db.queries import events_8k_sql
    load(con, tmp_path, [ev("a1", "2004-07-14", "4,7"), ev("a2", "2004-07-20", "4.01")])
    rows = con.sql(events_8k_sql(), params={"as_of": date(2004, 8, 1)}).fetchall()
    assert [(r[0], r[4]) for r in rows] == [("a2", "4.01")]


def test_8k_filed_after_as_of_is_never_read(con, tmp_path):
    # Accepted 23:31 UTC the evening before: the filing date, not the acceptance, is known-at.
    load(con, tmp_path, [ev("a1", "2024-09-04", "4.02", accepted="2024-09-03T23:31:03.000Z")])
    assert signals(con) == []
    assert signals(con, date(2024, 9, 4)) == ["8k_non_reliance"]


def test_windows_a_year_for_red_flags_and_90_days_for_officer_changes(con, tmp_path):
    load(con, tmp_path, [ev("a1", "2023-09-10", "4.02"), ev("a2", "2024-05-01", "5.02")])
    assert signals(con) == ["8k_non_reliance"]                       # 359 days; 5.02 was 125 days ago
    assert signals(con, date(2024, 9, 20)) == []                     # 376 days


def test_8k_signals_keep_a_fixed_order(con, tmp_path):
    load(con, tmp_path, [ev("a1", "2024-08-01", "5.02,4.01"), ev("a2", "2024-08-02", "4.02")])
    assert signals(con) == ["8k_non_reliance", "8k_auditor_change", "8k_officer_change"]


def test_8ks_never_change_status_rank_or_shortlist(tmp_path):
    def build(with_events):
        c = connect(":memory:")
        add_company(c, CIK, "ACME", cheap_company())
        add_buy(c, CIK, "b1")
        if with_events:
            load(c, tmp_path, [ev("a1", "2024-08-01", "4.01,4.02,2.06,3.01,5.02")])
        return {t: (r["status"], r["rank"], r["failed_filters"], r["issues"], r["flags"])
                for t, r in screen_rows(c).items()}
    assert build(True) == build(False)


# --- vault --------------------------------------------------------------------------

def test_company_note_lists_red_flag_8ks_and_a_second_sync_writes_nothing(con, tmp_path):
    add_buy(con, CIK, "b1")
    load(con, tmp_path, [ev("0001234-24-000901", "2024-08-01", "4.02,9.01"),
                         ev("0001234-24-000902", "2024-08-05", "2.02,9.01")])
    vault = tmp_path / "vault"
    sync_vault(con, vault, AS_OF)
    note = vault / "Companies" / "ACME.md"
    meta = dict(split_note(note.read_text())[0])
    assert meta["signals"] == ["8k_non_reliance"]
    body = note.read_text()
    assert "## 8-K events" in body
    assert "| 2024-08-01 | 8-K | 4.02 non-reliance on prior financial statements |" in body
    assert "000123424000901" in body                                 # EDGAR link
    assert "Last results 8-K (Item 2.02): 2024-08-05" in body
    assert "8-K filed 2024-08-05" in (vault / "Dashboard.md").read_text()
    assert sum(sync_vault(con, vault, AS_OF).written.values()) == 0
