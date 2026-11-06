"""M9 operations: DuckDB backup with retention, run log, health report, run lock, crash resume."""
import fcntl
import os
import plistlib
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import duckdb
import pytest

from src import ops
from src.db.schema import connect

TODAY = date(2026, 11, 7)


def make_db(tmp_path: Path, n: int = 3) -> Path:
    db = tmp_path / "data" / "edgar.duckdb"
    db.parent.mkdir()
    con = connect(str(db))
    con.executemany("INSERT INTO loaded_filings (cik, accn) VALUES (?, ?)", [(i, f"a{i}") for i in range(n)])
    con.close()
    return db


def backup(db, dest, today=TODAY, **kw):
    return ops.backup(db, dest, today, other_disk_only=False, **kw)


# --- backup -------------------------------------------------------------------------

def test_backup_not_configured_is_skipped_with_a_reason(tmp_path):
    b = ops.backup(make_db(tmp_path), None, TODAY)
    assert b["status"] == "skipped" and "EOQ_BACKUP_DIR" in b["message"]


def test_missing_folder_is_skipped_and_never_created(tmp_path):
    dest = tmp_path / "Volumes" / "External" / "backups"            # drive unplugged
    b = backup(make_db(tmp_path), dest)
    assert b["status"] == "skipped" and "not found" in b["message"]
    assert not (tmp_path / "Volumes").exists()


def test_folder_on_the_database_disk_is_refused(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    b = ops.backup(make_db(tmp_path), dest, TODAY)                    # same disk as tmp_path
    assert b["status"] == "skipped" and "same disk" in b["message"]
    assert list(dest.iterdir()) == []


def test_not_enough_room_is_skipped(tmp_path, monkeypatch):
    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda p: (100, 99, 1))
    b = backup(make_db(tmp_path), dest)
    assert b["status"] == "skipped" and "room" in b["message"]
    assert list(dest.iterdir()) == []


def test_backup_is_a_checked_copy_named_by_date(tmp_path):
    db = make_db(tmp_path, n=5)
    dest = tmp_path / "backups"
    dest.mkdir()
    b = backup(db, dest)
    copy = dest / f"edgar-{TODAY}.duckdb"
    assert b["status"] == "ok" and b["path"] == str(copy) and b["bytes"] == copy.stat().st_size
    with duckdb.connect(str(copy), read_only=True) as c:
        assert c.execute("SELECT count(*) FROM loaded_filings").fetchall() == [(5,)]
    assert not [p for p in dest.iterdir() if p.name.endswith(".part")]


def test_retention_keeps_four_and_never_touches_other_files(tmp_path):
    db = make_db(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir()
    old = [dest / f"edgar-2026-10-{d:02d}.duckdb" for d in (3, 10, 17, 24, 31)]
    others = [dest / "Godzilla.mp4", dest / "edgar-notes.txt", dest / "edgar-2026-01-01.duckdb.bak",
              dest / "EDGAR-2026-01-01.duckdb"]
    for p in old + others:
        p.write_bytes(b"x")
    b = backup(db, dest)
    assert b["status"] == "ok"
    kept = sorted(p.name for p in dest.iterdir() if ops.BACKUP_NAME.match(p.name))
    assert kept == ["edgar-2026-10-17.duckdb", "edgar-2026-10-24.duckdb", "edgar-2026-10-31.duckdb",
                    f"edgar-{TODAY}.duckdb"]
    assert all(p.exists() for p in others)
    assert b["removed"] == ["edgar-2026-10-03.duckdb", "edgar-2026-10-10.duckdb"]


def test_failed_copy_removes_nothing(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir()
    old = [dest / f"edgar-2026-10-{d:02d}.duckdb" for d in (3, 10, 17, 24)]
    for p in old:
        p.write_bytes(b"x")

    def boom(src, dst):
        Path(dst).write_bytes(b"half")
        raise OSError("drive removed")
    monkeypatch.setattr(ops.shutil, "copyfile", boom)
    b = backup(db, dest)
    assert b["status"] == "failed" and "drive removed" in b["message"]
    assert all(p.exists() for p in old) and not list(dest.glob("*.part"))


def test_database_in_use_by_another_process_fails_the_backup(tmp_path):
    db = make_db(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir()
    holder = subprocess.Popen([sys.executable, "-c",
                               f"import duckdb, time; c = duckdb.connect({str(db)!r}); print('open', flush=True); "
                               "time.sleep(30)"], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "open"
        b = backup(db, dest)
        assert b["status"] == "failed" and list(dest.iterdir()) == []
    finally:
        holder.kill()
        holder.wait()


# --- run log, health, lock ---------------------------------------------------------

def test_failed_step_stops_the_run_and_the_next_run_resumes(tmp_path):
    con = connect(":memory:")
    calls = []

    def ok(name):
        return lambda: calls.append(name)

    def crash():
        calls.append("b")
        raise RuntimeError("SEC timed out")

    first = ops.run_steps(con, "weekly", [("a", ok("a")), ("b", crash), ("c", ok("c"))], TODAY)
    assert first["status"] == "failed" and calls == ["a", "b"]
    assert [s["status"] for s in first["steps"]] == ["ok", "failed", "not run"]
    assert any("b failed" in w and "SEC timed out" in w for w in first["warnings"])
    second = ops.run_steps(con, "weekly", [("a", ok("a")), ("b", ok("b")), ("c", ok("c"))], TODAY)
    assert second["status"] == "ok" and calls[2:] == ["a", "b", "c"]
    assert con.execute("SELECT job, status FROM run_log ORDER BY started").fetchall() == [
        ("weekly", "failed"), ("weekly", "ok")]


def test_a_step_that_exits_is_recorded_not_fatal(tmp_path):
    con = connect(":memory:")

    def exits():
        sys.exit("set EOQ_VAULT_DIR in .env or pass --vault")
    r = ops.run_steps(con, "weekly", [("sync-vault", exits)], TODAY)
    assert r["status"] == "failed" and "EOQ_VAULT_DIR" in r["steps"][0]["message"]


def test_health_warns_on_drift_failures_stale_prices_throttling_and_backup(tmp_path):
    con = connect(":memory:")
    con.execute("INSERT INTO prices VALUES (1, 'A', DATE '2026-11-06', 10, NULL, 't'), "
                "(1, 'A', DATE '2026-11-05', 10, NULL, 't')")
    ops.run_steps(con, "daily", [], TODAY)
    clean = ops.run_steps(con, "daily", [], TODAY)
    assert clean["warnings"] == [] and clean["counts"]["prices"] == 2
    con.execute("DELETE FROM prices WHERE price_date = DATE '2026-11-06'")
    con.execute("INSERT INTO form4_filings VALUES ('x', NULL, NULL, NULL, 'xml', 'failed', 'boom', now())")
    con.execute("INSERT INTO price_status VALUES (1, 'A', 'error', NULL, NULL, 'timeout', now())")
    r = ops.run_steps(con, "weekly", [], date(2026, 11, 12), throttled=lambda: 3,
                      backup={"status": "skipped", "message": "backup folder not found: /Volumes/External/x"})
    text = "\n".join(r["warnings"])
    assert "prices fell from 2 to 1" in text
    assert "1 Form 4 filings failed" in text and "1 companies failed their price refresh" in text
    assert "latest price is 2026-11-05 (7 days old)" in text
    assert "SEC throttled 3 requests" in text
    assert "backup skipped: backup folder not found" in text
    md = ops.render_health(r)
    assert "## Warnings" in md and "| prices | 2 | 1 | -1 |" in md


def test_second_run_while_one_holds_the_lock_is_refused(tmp_path):
    lock = tmp_path / ".run.lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ops.RunLocked):
            with ops.run_lock(lock):
                pass
    with ops.run_lock(lock):
        pass                                                         # free again


# --- forced crash -------------------------------------------------------------------

CHILD = r"""
import sys, time
from pathlib import Path
from src.db.schema import connect
from src.ingestion import form4_sources as fs
db, log, xml = sys.argv[1], sys.argv[2], Path(sys.argv[3]).read_bytes()
con = connect(db)
def get(url):
    with open(log, "a") as f:
        f.write(url + "\n")
    time.sleep(float(sys.argv[4]))
    return b"<SEC-DOCUMENT>\n<XML>\n" + xml.split(b"\n", 1)[1] + b"\n</XML>\n</SEC-DOCUMENT>"
fs.fetch_filings(con, get, batch_size=5)
"""


def test_killed_form4_fetch_resumes_without_duplicates_or_refetches(tmp_path):
    n = 40
    db = tmp_path / "edgar.duckdb"
    con = connect(str(db))
    con.execute("INSERT INTO price_status VALUES (2488, 'AMD', 'ok', NULL, NULL, NULL, now())")
    con.executemany("INSERT INTO form4_index VALUES ('2026q4', ?, 2488, '4', DATE '2026-11-02', ?)",
                    [(f"0000002488-26-{i:06d}", f"edgar/data/2488/0000002488-26-{i:06d}.txt") for i in range(n)])
    con.close()
    log, script = tmp_path / "calls.log", tmp_path / "child.py"
    script.write_text(CHILD)
    xml = Path(__file__).parent / "fixtures" / "form4_10b5_1.xml"
    child = subprocess.Popen([sys.executable, str(script), str(db), str(log), str(xml), "0.02"],
                             cwd=Path(__file__).parent.parent, env={**os.environ, "PYTHONPATH": "."})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and (not log.exists() or len(log.read_text().splitlines()) < 17):
        time.sleep(0.01)
    child.send_signal(signal.SIGKILL)                                # mid-batch, mid-run
    child.wait()
    first = len(log.read_text().splitlines())
    assert 17 <= first < n
    con = connect(str(db))
    done = con.execute("SELECT count(*) FROM form4_filings WHERE status = 'done'").fetchall()[0][0]
    assert done % 5 == 0 and done < n                                # only whole batches committed
    con.close()
    subprocess.run([sys.executable, str(script), str(db), str(log), str(xml), "0"], check=True,
                   cwd=Path(__file__).parent.parent, env={**os.environ, "PYTHONPATH": "."})
    con = connect(str(db))
    assert con.execute("SELECT count(*), count(DISTINCT accn) FROM form4_filings WHERE status = 'done'").fetchall() == [(n, n)]
    per = con.execute("SELECT count(*) FROM form4_transactions").fetchall()[0][0] // n
    assert con.execute("SELECT count(*) FROM form4_transactions").fetchall() == [(per * n,)] and per > 0
    urls = log.read_text().splitlines()
    assert len(set(urls)) == n
    assert len(urls) - n == first - done < 5 + 1                     # refetched: only the uncommitted batch


# --- SEC client and schedule ----------------------------------------------------------

def test_sec_client_counts_throttled_responses(monkeypatch):
    from src.ingestion import sec

    class R:
        def __init__(self, code):
            self.status_code, self.content = code, b"ok"

        def close(self):
            pass

        def raise_for_status(self):
            pass
    codes = iter([429, 503, 200])
    c = sec.SecClient("Test test@example.com", rate_per_s=1000)
    monkeypatch.setattr(c._session, "get", lambda *a, **k: R(next(codes)))
    monkeypatch.setattr(sec.time, "sleep", lambda s: None)
    assert c.get("https://www.sec.gov/x") == b"ok" and c.throttled == 2


def test_schedule_writes_weekly_and_daily_launchd_jobs_without_installing(tmp_path):
    paths = ops.write_schedule(tmp_path / "launchd", repo=Path("/repo"), uv="/bin/uv")
    weekly, daily = (plistlib.loads(p.read_bytes()) for p in paths)
    assert weekly["Label"] == "com.edgar-obsidian-quant.weekly" and weekly["WorkingDirectory"] == "/repo"
    assert weekly["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]
    assert weekly["ProgramArguments"][-2:] == ["run", "weekly"] and "--env-file" in weekly["ProgramArguments"]
    assert weekly["StartCalendarInterval"] == {"Weekday": 6, "Hour": 7, "Minute": 0}
    assert daily["ProgramArguments"][-2:] == ["run", "daily"]
    assert [d["Weekday"] for d in daily["StartCalendarInterval"]] == [1, 2, 3, 4, 5]
    assert weekly["StandardOutPath"] == "/repo/data/logs/weekly.log"
    assert weekly["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"    # a killed run still leaves its log
    assert [p.parent for p in paths] == [tmp_path / "launchd"] * 2          # written here, not installed


# --- the run command ------------------------------------------------------------------

def test_run_command_logs_reports_to_the_vault_and_skips_a_missing_backup(tmp_path, monkeypatch, capsys):
    from src import cli, config
    db = make_db(tmp_path)
    vault = tmp_path / "vault"
    monkeypatch.setattr(config, "DATA_DIR", db.parent)
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "VAULT_DIR", vault)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "Volumes" / "External" / "backups")
    monkeypatch.setitem(cli.RUN_STEPS, "weekly", [["stats"]])
    cli.main(["run", "weekly"])
    out = capsys.readouterr().out
    assert "backup skipped: backup folder not found" in out and "| stats | ok |" in out
    note = (vault / "Reports" / "Health.md").read_text()
    assert "type: health" in note and "status: ok" in note and "backup skipped" in note
    assert not (tmp_path / "Volumes").exists()
    monkeypatch.setitem(cli.RUN_STEPS, "daily", [["facts", "NOPE", "revenue"]])     # exits: unknown ticker
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "daily"])
    assert e.value.code == 1
    con = connect(str(db))
    assert con.execute("SELECT job, status FROM run_log ORDER BY started").fetchall() == [
        ("weekly", "ok"), ("daily", "failed")]


# --- live weekly run, 2026-11-05 -------------------------------------------------------

def test_sec_client_retries_a_dropped_connection(monkeypatch):
    """The first live weekly run died on one RemoteDisconnected from SEC."""
    import requests
    from src.ingestion import sec

    class R:
        status_code, content = 200, b"ok"

        def raise_for_status(self):
            pass
    calls = []

    def get(*a, **k):
        calls.append(1)
        if len(calls) <= 2:
            raise requests.ConnectionError("('Connection aborted.', RemoteDisconnected(...))")
        return R()
    c = sec.SecClient("Test test@example.com", rate_per_s=1000)
    monkeypatch.setattr(c._session, "get", get)
    monkeypatch.setattr(sec.time, "sleep", lambda s: None)
    assert c.get("https://www.sec.gov/x") == b"ok" and len(calls) == 3 and c.dropped == 2


def test_sec_client_gives_up_on_a_connection_that_keeps_dropping(monkeypatch):
    import requests
    from src.ingestion import sec

    def get(*a, **k):
        raise requests.ConnectionError("down")
    c = sec.SecClient("Test test@example.com", rate_per_s=1000)
    monkeypatch.setattr(c._session, "get", get)
    monkeypatch.setattr(sec.time, "sleep", lambda s: None)
    with pytest.raises(requests.ConnectionError):
        c.get("https://www.sec.gov/x")
    assert c.dropped == 5


def test_snapshot_tables_may_shrink_a_little_but_ledgers_never(tmp_path):
    con = connect(":memory:")
    con.execute("INSERT INTO prices VALUES (1, 'A', DATE '2026-11-06', 10, NULL, 't')")
    con.executemany("INSERT INTO tickers (ticker, cik, fetched_on) VALUES (?, ?, DATE '2026-11-01')", [(f"T{i}", i) for i in range(200)])
    con.executemany("INSERT INTO loaded_filings (cik, accn) VALUES (?, ?)", [(i, f"a{i}") for i in range(200)])
    ops.run_steps(con, "weekly", [], TODAY)
    con.execute("DELETE FROM tickers WHERE ticker = 'T0'")                 # one delisting: 0.5%
    con.execute("DELETE FROM loaded_filings WHERE accn = 'a0'")            # a ledger row lost
    w = ops.run_steps(con, "weekly", [], TODAY)["warnings"]
    assert not any(x.startswith("tickers") for x in w)
    assert "loaded_filings fell from 200 to 199" in w
    con.execute("DELETE FROM tickers WHERE ticker IN ('T1', 'T2', 'T3')")    # 1.5% in one run
    assert "tickers fell from 199 to 196" in ops.run_steps(con, "weekly", [], TODAY)["warnings"]
