"""Daily dashboard: on weekdays, a Form 4 import with no failed filings, then a price refresh
with no failed companies, then sync-vault (which recomputes the screen) on that day's closes and
the stored fundamentals. Saturday's weekly stays the full refresh (facts, 10-K text, the screen
Parquet).

The daily runs the real `ingest form4` against a fake SEC client: the data set page lists no
quarters, the current quarter's form.idx lists one Form 4, and its document either loads or
fails. It runs the real `ingest prices` with a fake provider in place of Yahoo."""
import functools
import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from src import cli, config
from src.db.schema import connect
from src.ingestion import form4_sources, prices
from src.ingestion.companyfacts import load_docs
from src.obsidian.sync import sync_vault, write_journal
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK, fact, form4_xml
from tests.test_m4_screen import AS_OF, GAAP, add_buy, add_company, cheap_company, doc_for
from tests.test_prices import yahoo

NEW_ACCN = "0001234-24-000099"
NEW_PATH = f"edgar/data/{CIK}/{NEW_ACCN}.txt"
INDEX = f"4          ACME Corp          {CIK}   2024-09-02  {NEW_PATH}\n"
LABOR_DAY = date(2024, 9, 2)           # NYSE closed: the provider has no row for it
CLOSE_TODAY = 11.0                     # the close on each new trading day (stored: 12.0 on 2024-08-30)


class FakeYahoo:
    """In place of yfinance. mode 'ok': the 2024-08-30 close plus a close for `today` when it is a
    trading day. 'no_new': nothing after 2024-08-30 (halted). 'down': the provider fails."""
    def __init__(self):
        self.mode, self.today, self.calls, self.batch_size = "ok", AS_OF, 0, 50

    def __call__(self, tickers: list[str], start: date) -> dict:
        self.calls += 1
        if self.mode == "down":
            raise ConnectionError("Yahoo unreachable")
        rows = [("2024-08-30", 12.0, 1000.0, 0.0, 0.0)]
        if self.mode == "ok" and self.today.weekday() < 5 and self.today != LABOR_DAY:
            rows.append((self.today.isoformat(), CLOSE_TODAY, 1000.0, 0.0, 0.0))
        return {t: yahoo([r for r in rows if date.fromisoformat(r[0]) >= start]) for t in tickers}


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
    con.execute("INSERT INTO tickers VALUES (?, 'ACME', 'ACME Corp', 'Nasdaq', DATE '2024-08-01')", [CIK])
    con.close()
    vault = tmp_path / "vault"
    yf = FakeYahoo()
    real_load = prices.load
    monkeypatch.setattr(prices, "load", lambda *a, **k: real_load(
        *a, **{**k, "download": yf, "pause_s": 0, "batch_size": yf.batch_size}))
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
        yf.today = today

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

    return {"db": db, "vault": vault, "run": run_daily, "pin": pin, "yahoo": yf}


def steps(row) -> list[tuple[str, str]]:
    return [(s["name"], s["status"]) for s in json.loads(row[1])]


def screens(env) -> list[Path]:
    return sorted((env["db"].parent / "screens").glob("*.parquet"))


OK3 = [("ingest form4 --strict", "ok"), ("ingest prices --strict", "ok"), ("sync-vault", "ok")]


def test_daily_is_form4_then_prices_then_sync_and_weekly_stays_the_full_refresh():
    assert cli.RUN_STEPS["daily"] == [["ingest", "form4", "--strict"], ["ingest", "prices", "--strict"],
                                      ["sync-vault"]]
    assert cli.RUN_STEPS["weekly"] == [["ingest", "all"], ["screen"], ["fetch-text"], ["sync-vault"]]


def test_successful_daily_import_publishes_the_shortlist_and_company_notes(env):
    code, row = env["run"]()
    assert code == 0 and row[0] == "ok"
    assert steps(row) == OK3
    vault = env["vault"]
    company = fm(vault / "Companies" / "ACME.md")
    assert company["status"] == "shortlist" and company["rank"] == 1 and company["as_of"] == AS_OF
    assert company["price_date"] == AS_OF                          # valued on today's close
    assert company["mcap"] == CLOSE_TODAY * 1_000_000
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
    assert steps(row) == [("ingest form4 --strict", "failed"), ("ingest prices --strict", "not run"),
                          ("sync-vault", "not run")]
    assert "1 Form 4 filings failed" in json.loads(row[1])[0]["message"]
    assert vault_files(env["vault"]) == before                    # dashboard and notes untouched
    health = (env["vault"] / "Reports" / "Health.md").read_text()
    assert "status: failed" in health
    con = connect(str(env["db"]))
    assert con.execute("SELECT status FROM form4_filings WHERE accn = ?", [NEW_ACCN]).fetchall() == [("failed",)]
    assert [a for a, *_ in form4_sources.pending_filings(con)] == [NEW_ACCN]   # retried next run
    con.close()
    code, row = env["run"](AS_OF)                                  # next run loads it and publishes
    assert code == 0 and steps(row) == OK3


def test_strict_is_only_the_daily_gate(env, capsys):                               # C1: other entry points
    env["pin"](AS_OF, document_fails=True)
    con = connect(str(env["db"]))
    cli._step(cli.build_parser(), ["ingest", "form4"], con)       # no --strict: reports and returns
    assert "1 failed" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="1 Form 4 filings failed"):
        cli._step(cli.build_parser(), ["ingest", "form4", "--strict"], con)
    con.close()


def test_daily_without_a_vault_is_the_legacy_form4_import(env, monkeypatch):      # C2
    """Nothing to publish: the daily stays what it was, `ingest form4` alone, warning on failures."""
    monkeypatch.setattr(config, "VAULT_DIR", None)
    code, row = env["run"]()
    assert code == 0 and row[0] == "ok"
    assert steps(row) == [("ingest form4", "ok")]
    assert env["yahoo"].calls == 0                                 # no price fetch
    assert not env["vault"].exists() and screens(env) == []


def test_daily_without_a_vault_warns_and_retries_a_failed_filing(env, monkeypatch):     # C2 re-check
    monkeypatch.setattr(config, "VAULT_DIR", None)
    env["pin"](AS_OF, document_fails=True)
    cli.main(["run", "daily"])                                     # no SystemExit: exit 0
    con = connect(str(env["db"]))
    status, steps_json, warnings = con.execute(
        "SELECT status, steps, warnings FROM run_log ORDER BY started DESC LIMIT 1").fetchall()[0]
    assert status == "ok" and steps((status, steps_json)) == [("ingest form4", "ok")]
    assert "1 Form 4 filings failed (retried next run)" in json.loads(warnings)
    assert [a for a, *_ in form4_sources.pending_filings(con)] == [NEW_ACCN]
    con.close()
    assert env["yahoo"].calls == 0 and not env["vault"].exists()


@pytest.mark.parametrize("today, warns", [(date(2024, 9, 6), False),    # Fri: last Friday's close, 7 days
                                          (date(2024, 9, 9), True)])    # Mon: 10 days
def test_daily_without_a_vault_keeps_the_weekly_price_age_cadence(env, monkeypatch, today, warns):
    monkeypatch.setattr(config, "VAULT_DIR", None)
    code, row = env["run"](today)
    assert code == 0
    with duckdb.connect(str(env["db"]), read_only=True) as c:
        warnings = json.loads(c.execute("SELECT warnings FROM run_log ORDER BY started DESC LIMIT 1").fetchall()[0][0])
    assert any(w.startswith("latest price is") for w in warnings) is warns, warnings


def test_daily_with_prices_warns_at_four_days(env):
    env["yahoo"].mode = "no_new"                                  # refreshed, but no new close
    code, row = env["run"](date(2024, 9, 4))                       # Wed: Friday's close, 5 days
    assert code == 0 and steps(row) == OK3
    with duckdb.connect(str(env["db"]), read_only=True) as c:
        warnings = json.loads(c.execute("SELECT warnings FROM run_log ORDER BY started DESC LIMIT 1").fetchall()[0][0])
    assert "latest price is 2024-08-30 (5 days old)" in warnings


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


def test_provider_failure_publishes_nothing_and_keeps_the_stored_closes(env, capsys):
    assert env["run"](AS_OF - timedelta(days=1))[0] == 0          # yesterday's dashboard
    before = vault_files(env["vault"])
    env["yahoo"].mode = "down"
    code, row = env["run"](AS_OF)
    assert code == 1 and row[0] == "failed"
    assert steps(row) == [("ingest form4 --strict", "ok"), ("ingest prices --strict", "failed"),
                          ("sync-vault", "not run")]
    assert "1 companies failed their price refresh" in json.loads(row[1])[1]["message"]
    assert vault_files(env["vault"]) == before
    con = connect(str(env["db"]))
    assert con.execute("SELECT status, last_date FROM price_status").fetchall() == [("error", date(2024, 8, 30))]
    assert con.execute("SELECT count(*) FROM form4_transactions WHERE accn = ?", [NEW_ACCN]).fetchall() == [(1,)]
    cli._step(cli.build_parser(), ["ingest", "prices"], con)      # without --strict: reports, returns
    assert "1 errors" in capsys.readouterr().out
    con.close()
    env["yahoo"].mode = "ok"                                       # provider back: the next run publishes
    code, row = env["run"](AS_OF)
    assert code == 0 and steps(row) == OK3
    assert fm(env["vault"] / "Companies" / "ACME.md")["price_date"] == AS_OF


def test_no_trading_day_publishes_on_the_last_close(env):
    code, row = env["run"](LABOR_DAY)
    assert code == 0 and steps(row) == OK3 and env["yahoo"].calls == 1
    company = fm(env["vault"] / "Companies" / "ACME.md")
    assert company["as_of"] == LABOR_DAY and company["price_date"] == date(2024, 8, 30)
    assert company["status"] == "shortlist"                       # 3 days old: fresh
    con = connect(str(env["db"]))                                 # a holiday is not a failure
    assert con.execute("SELECT status, last_date FROM price_status").fetchall() == [("ok", date(2024, 8, 30))]
    con.close()
    assert not [w for w in json.loads(row[1]) if w["status"] != "ok"]
    health = (env["vault"] / "Reports" / "Health.md").read_text()
    assert "latest price" not in health


def test_daily_on_stale_prices_quarantines_instead_of_shortlisting(env):
    env["yahoo"].mode = "no_new"                                  # halted: no close after 2024-08-30
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
            "close 2024-08-30, Form 4 filed 2024-08-16, XBRL fact filed 2024-09-02, 8-K filed none, short interest settled none, 13F quarter none.") in dash
    assert "Prices refresh on weekdays" in dash
    assert "through" not in dash.split("## Shortlist")[0]
    assert "each company note lists its own close date and sources" in dash
    assert 'price_date AS "Close date"' in dash


def test_provider_outage_stops_the_strict_daily_early_and_releases_the_lock(env):   # C6
    import fcntl
    assert env["run"](AS_OF - timedelta(days=1))[0] == 0          # yesterday's dashboard
    before = vault_files(env["vault"])
    con = connect(str(env["db"]))                                 # 5 more companies, priced first (no history)
    for c in range(101, 106):
        con.execute("INSERT INTO companies (cik, name, is_financial, is_shell) VALUES (?, ?, FALSE, FALSE)", [c, f"Co {c}"])
        con.execute("INSERT INTO tickers VALUES (?, ?, ?, 'NYSE', DATE '2024-08-01')", [c, f"T{c}", f"Co {c}"])
        con.execute("INSERT INTO loaded_filings (cik, accn) VALUES (?, ?)", [c, f"x{c}"])
    con.close()
    env["yahoo"].mode, env["yahoo"].batch_size, env["yahoo"].calls = "down", 1, 0
    code, row = env["run"](AS_OF)
    assert code == 1 and env["yahoo"].calls == prices.OUTAGE_BATCHES     # not all 6 batches
    assert steps(row) == [("ingest form4 --strict", "ok"), ("ingest prices --strict", "failed"),
                          ("sync-vault", "not run")]
    assert "3 not attempted" in json.loads(row[1])[1]["message"]
    assert vault_files(env["vault"]) == before
    con = connect(str(env["db"]))
    assert con.execute("SELECT status, last_date FROM price_status WHERE cik = ?", [CIK]).fetchall() == [
        ("ok", date(2024, 8, 30))]                                 # not attempted: stored close kept
    con.close()
    with open(env["db"].parent / ".run.lock", "a") as f:          # the run released its lock
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env["yahoo"].mode = "ok"
    code, row = env["run"](AS_OF)
    assert code == 0 and steps(row) == OK3
    assert fm(env["vault"] / "Companies" / "ACME.md")["price_date"] == AS_OF
