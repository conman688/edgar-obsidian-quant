"""Daily dashboard: after a weekday Form 4 import with no failed filings, the daily run syncs
the vault (sync-vault recomputes the screen) from the stored prices and fundamentals.
Saturday's weekly stays the full refresh (prices, facts, 10-K text, the screen Parquet).

The daily runs the real `ingest form4` against a fake SEC client: the data set page lists no
quarters, the current quarter's form.idx lists one Form 4, and its document either loads or
fails."""
import functools
import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from src import cli, config
from src.db.schema import connect
from src.ingestion import form4_sources
from src.ingestion.companyfacts import load_docs
from src.obsidian.sync import sync_vault, write_journal
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK, fact, form4_xml
from tests.test_m4_screen import AS_OF, GAAP, add_buy, add_company, cheap_company, doc_for

NEW_ACCN = "0001234-24-000099"
NEW_PATH = f"edgar/data/{CIK}/{NEW_ACCN}.txt"
INDEX = f"4          ACME Corp          {CIK}   2024-09-02  {NEW_PATH}\n"


class FakeSec:
    throttled = dropped = 0

    def __init__(self, document_fails: bool):
        self.document_fails = document_fails

    def get(self, url: str) -> bytes:
        if url == form4_sources.DATASETS_PAGE:
            return b"<html></html>"
        if url == form4_sources.INDEX_URL.format(year=2024, n=3):
            return INDEX.encode()
        if "full-index" in url:          # other quarters: one Form 4 of an issuer outside the universe
            q = url.split("/")[-3] + url.split("/")[-2]
            return f"4          OTHER Corp          1   2023-01-03  edgar/data/1/0000000001-{q}.txt\n".encode()
        assert url == form4_sources.FILING_URL.format(path=NEW_PATH), url
        if self.document_fails:
            raise ConnectionError("document fetch failed")
        xml = form4_xml(CIK * 10 + 1, "NEW BUYER", "Chief Financial Officer",
                        [("2024-08-30", "P", 5000, 12.0, "A", 9000, "D", "", False)], doc_flag=0)
        return xml


def fm(path: Path) -> dict:
    return dict(split_note(path.read_text())[0])


def vault_files(vault: Path) -> dict:
    """Every vault file except the health report, which every run rewrites."""
    return {p.relative_to(vault): p.read_bytes() for p in sorted(vault.rglob("*"))
            if p.is_file() and p != vault / "Reports" / "Health.md"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A file DB with one shortlisted company, a configured vault, and `today` pinned."""
    db = tmp_path / "data" / "edgar.duckdb"
    db.parent.mkdir()
    con = connect(str(db))
    add_company(con, CIK, "ACME", cheap_company())
    add_buy(con, CIK, "0001234-24-000001")
    con.close()
    vault = tmp_path / "vault"
    monkeypatch.setattr(config, "DATA_DIR", db.parent)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "VAULT_DIR", vault)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "no-backups")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")

    def pin(today: date = AS_OF, document_fails: bool = False):
        class Today(date):
            @classmethod
            def today(cls):
                return today
        monkeypatch.setattr(cli, "date", Today)
        monkeypatch.setattr(cli, "_client", functools.cache(lambda: FakeSec(document_fails)))

    def run_daily(today: date = AS_OF, document_fails: bool = False):
        pin(today, document_fails)
        try:
            cli.main(["run", "daily"])
            code = 0
        except SystemExit as e:
            code = e.code
        with duckdb.connect(str(db), read_only=True) as c:
            row = c.execute("SELECT status, steps FROM run_log ORDER BY started DESC LIMIT 1").fetchall()[0]
        return code, row

    return {"db": db, "vault": vault, "run": run_daily, "pin": pin}


def steps(row) -> list[tuple[str, str]]:
    return [(s["name"], s["status"]) for s in json.loads(row[1])]


def screens(env) -> list[Path]:
    return sorted((env["db"].parent / "screens").glob("*.parquet"))


def test_daily_is_a_strict_form4_import_then_sync_and_weekly_stays_the_full_refresh():
    assert cli.RUN_STEPS["daily"] == [["ingest", "form4", "--strict"], ["sync-vault"]]
    assert cli.RUN_STEPS["weekly"] == [["ingest", "all"], ["screen"], ["fetch-text"], ["sync-vault"]]


def test_successful_daily_import_publishes_the_shortlist_and_company_notes(env):
    code, row = env["run"]()
    assert code == 0 and row[0] == "ok"
    assert steps(row) == [("ingest form4 --strict", "ok"), ("sync-vault", "ok")]
    vault = env["vault"]
    company = fm(vault / "Companies" / "ACME.md")
    assert company["status"] == "shortlist" and company["rank"] == 1 and company["as_of"] == AS_OF
    assert company["price_date"] == date(2024, 8, 30)            # the stored close it was valued on
    events = [fm(p) for p in (vault / "Events").glob("*.md")]
    assert NEW_ACCN in {e["accn"] for e in events}                 # today's import is on the dashboard
    assert f"Synced {AS_OF}" in (vault / "Dashboard.md").read_text()
    assert screens(env) == []                                      # C5: no weekday Parquet


def test_failed_form4_document_publishes_nothing_and_stays_retryable(env):   # C1
    assert env["run"](AS_OF - timedelta(days=1), document_fails=True)[0] == 1
    con = connect(str(env["db"]))
    sync_vault(con, env["vault"], AS_OF - timedelta(days=1))      # yesterday's dashboard
    con.close()
    before = vault_files(env["vault"])
    code, row = env["run"](AS_OF, document_fails=True)
    assert code == 1 and row[0] == "failed"
    assert steps(row) == [("ingest form4 --strict", "failed"), ("sync-vault", "not run")]
    assert "1 Form 4 filings failed" in json.loads(row[1])[0]["message"]
    assert vault_files(env["vault"]) == before                    # dashboard and notes untouched
    health = (env["vault"] / "Reports" / "Health.md").read_text()
    assert "status: failed" in health
    con = connect(str(env["db"]))
    assert con.execute("SELECT status FROM form4_filings WHERE accn = ?", [NEW_ACCN]).fetchall() == [("failed",)]
    assert [a for a, *_ in form4_sources.pending_filings(con)] == [NEW_ACCN]   # retried next run
    con.close()
    code, row = env["run"](AS_OF)                                  # next run loads it and publishes
    assert code == 0 and steps(row) == [("ingest form4 --strict", "ok"), ("sync-vault", "ok")]


def test_strict_is_only_the_daily_gate(env, capsys):                               # C1: other entry points
    env["pin"](AS_OF, document_fails=True)
    con = connect(str(env["db"]))
    cli._step(cli.build_parser(), ["ingest", "form4"], con)       # no --strict: reports and returns
    assert "1 failed" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="1 Form 4 filings failed"):
        cli._step(cli.build_parser(), ["ingest", "form4", "--strict"], con)
    con.close()


def test_daily_without_a_vault_stays_import_only(env, monkeypatch):                # C2
    monkeypatch.setattr(config, "VAULT_DIR", None)
    code, row = env["run"]()
    assert code == 0 and row[0] == "ok"
    assert steps(row) == [("ingest form4 --strict", "ok")]
    assert not env["vault"].exists() and screens(env) == []


def test_daily_publish_keeps_journals_and_the_users_text(env):
    con = connect(str(env["db"]))
    journal = write_journal(con, env["vault"], "ACME", AS_OF - timedelta(days=1))
    sync_vault(con, env["vault"], AS_OF - timedelta(days=1))
    con.close()
    journal.write_text(journal.read_text().replace("## Thesis\n", "## Thesis\n\nCash pile is underpriced.\n"))
    note = env["vault"] / "Companies" / "ACME.md"
    note.write_text(note.read_text() + "\nMy own notes on ACME.\n")
    j_before = journal.read_bytes()
    assert env["run"]()[0] == 0
    assert journal.read_bytes() == j_before                       # never rewritten
    assert "My own notes on ACME." in note.read_text()            # outside the AUTO block: the user's
    assert fm(note)["as_of"] == AS_OF
    assert f"[[{journal.stem}]]" in (env["vault"] / "Dashboard.md").read_text()   # then-vs-now row


def test_daily_on_stale_prices_quarantines_instead_of_shortlisting(env):
    later = AS_OF + timedelta(days=6)                             # latest close 2024-08-30: 10 days old
    assert env["run"](later)[0] == 0
    company = fm(env["vault"] / "Companies" / "ACME.md")
    assert company["status"] == "quarantine" and "stale_price" in company["issues"]
    assert company["price_date"] == date(2024, 8, 30)


def test_dashboard_dates_are_database_wide_and_not_coverage(env):                 # C3
    con = connect(str(env["db"]))
    # A later fact of an unrelated issuer: the newest record in the database, not an input of ACME.
    load_docs(con, [doc_for(999, {(GAAP, "Revenues", "USD"): [fact("2023-01-01", "2023-12-31", 1, "2024-09-02", "z")]})])
    sync_vault(con, env["vault"], AS_OF)
    con.close()
    dash = (env["vault"] / "Dashboard.md").read_text()
    assert ("Newest dated records stored in the database (all companies, not only those listed): "
            "close 2024-08-30, Form 4 filed 2024-08-16, XBRL fact filed 2024-09-02.") in dash
    assert "through" not in dash.split("## Shortlist")[0]
    assert "each company note lists its own close date and sources" in dash
    assert 'price_date AS "Close date"' in dash
