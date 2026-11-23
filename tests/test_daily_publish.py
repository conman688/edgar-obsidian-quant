"""Daily dashboard: after a successful weekday Form 4 import, the daily run recomputes the
screen from stored prices and fundamentals and publishes it to the Obsidian vault (sync-vault).
Saturday's weekly stays the full refresh (prices, facts, 10-K text)."""
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from src import cli, config
from src.db.schema import connect
from src.obsidian.sync import sync_vault, write_journal
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company

NEW_ACCN = "0001234-24-000099"


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

    def run_daily(today: date = AS_OF, ingest=None):
        class Today(date):
            @classmethod
            def today(cls):
                return today
        monkeypatch.setattr(cli, "date", Today)
        monkeypatch.setattr(cli, "cmd_ingest", ingest or imported)
        try:
            cli.main(["run", "daily"])
            code = 0
        except SystemExit as e:
            code = e.code
        with duckdb.connect(str(db), read_only=True) as c:
            row = c.execute("SELECT status, steps FROM run_log ORDER BY started DESC LIMIT 1").fetchall()[0]
        return code, row

    return {"db": db, "vault": vault, "run": run_daily}


def imported(args, con):
    """A Form 4 import that loaded one new purchase."""
    assert args.what == "form4"
    add_buy(con, CIK, NEW_ACCN, filed=AS_OF - timedelta(days=1))


def sec_down(args, con):
    raise RuntimeError("SEC unreachable")


def steps(row) -> list[tuple[str, str]]:
    import json
    return [(s["name"], s["status"]) for s in json.loads(row[1])]


def test_daily_is_form4_then_screen_then_sync_and_weekly_stays_the_full_refresh():
    assert cli.RUN_STEPS["daily"] == [["ingest", "form4"], ["screen"], ["sync-vault"]]
    assert cli.RUN_STEPS["weekly"] == [["ingest", "all"], ["screen"], ["fetch-text"], ["sync-vault"]]


def test_successful_daily_import_publishes_the_shortlist_and_company_notes(env):
    code, row = env["run"]()
    assert code == 0 and row[0] == "ok"
    assert steps(row) == [("ingest form4", "ok"), ("screen", "ok"), ("sync-vault", "ok")]
    vault = env["vault"]
    company = fm(vault / "Companies" / "ACME.md")
    assert company["status"] == "shortlist" and company["rank"] == 1 and company["as_of"] == AS_OF
    assert company["price_date"] == date(2024, 8, 30)            # the stored close it was valued on
    events = [fm(p) for p in (vault / "Events").glob("*.md")]
    assert NEW_ACCN in {e["accn"] for e in events}                 # today's import is on the dashboard
    assert (env["db"].parent / "screens" / f"screen-{AS_OF}.parquet").exists()
    dash = (vault / "Dashboard.md").read_text()
    assert f"Synced {AS_OF}" in dash


def test_failed_daily_import_publishes_nothing(env):
    assert env["run"](AS_OF - timedelta(days=1))[0] == 0          # yesterday's dashboard
    before = vault_files(env["vault"])
    code, row = env["run"](AS_OF, ingest=sec_down)
    assert code == 1 and row[0] == "failed"
    assert steps(row) == [("ingest form4", "failed"), ("screen", "not run"), ("sync-vault", "not run")]
    assert vault_files(env["vault"]) == before                    # dashboard and notes untouched
    assert not (env["db"].parent / "screens" / f"screen-{AS_OF}.parquet").exists()
    health = (env["vault"] / "Reports" / "Health.md").read_text()
    assert "status: failed" in health and "SEC unreachable" in health


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


def test_dashboard_shows_the_dates_of_the_stored_data(env, tmp_path):
    con = connect(str(env["db"]))
    sync_vault(con, env["vault"], AS_OF)
    con.close()
    dash = (env["vault"] / "Dashboard.md").read_text()
    # Prices refresh only on Saturdays: a weekday dashboard says how old its close is.
    assert "Latest close 2024-08-30" in dash
    assert "Form 4 filed through 2024-08-16" in dash
    assert "XBRL facts filed through 2024-08-01" in dash
    assert 'price_date AS "Close date"' in dash
