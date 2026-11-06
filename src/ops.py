"""Unattended operations (M9): DuckDB backup, scheduled runs, run log and health report.

- `backup` copies data/edgar.duckdb to a folder on another disk (an external drive) and keeps the
  last KEEP copies. It never creates the folder (an unplugged drive would turn /Volumes/<name>
  into a plain folder on the internal disk), refuses a folder on the database's own disk, and
  deletes only its own files: `edgar-YYYY-MM-DD.duckdb`, and its own unfinished `.part` copies.
- `run_steps` runs a job's steps in order and stops at the first failure. Every load is
  idempotent with a ledger, so the next run resumes where this one stopped.
- Each run is one row in `run_log`; its row counts are the next run's baseline for drift.
- `run_lock` keeps two runs (weekly and daily, or a manual one) from overlapping.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import shutil
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import duckdb

from src import config

KEEP = 4
ROOM = 1.05                 # free space needed on the backup disk, as a multiple of the database
STALE_RUN_DAYS = 4          # latest close older than this on a run day: the price refresh is stuck
DRIFT = 0.01                # a table that shrinks by more than this in one run gets a warning
LEDGERS = ("loaded_filings", "form4_filings", "filing_sections")   # never shrink: any drop is a warning
BACKUP_NAME = re.compile(r"^edgar-\d{4}-\d{2}-\d{2}\.duckdb$")
PART_NAME = re.compile(r"^\.edgar-\d{4}-\d{2}-\d{2}\.duckdb\.part$")
TABLES = ("xbrl_facts", "loaded_filings", "companies", "tickers", "prices", "splits", "dividends",
          "price_status", "form4_transactions", "form4_filings", "form4_index", "filing_sections", "person")
SCHEDULE = {"weekly": {"Weekday": 6, "Hour": 7, "Minute": 0},                  # Saturday 07:00
            "daily": [{"Weekday": d, "Hour": 21, "Minute": 0} for d in range(1, 6)]}   # Mon-Fri 21:00


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def _db_config() -> dict:
    return {"memory_limit": config.DB_MEMORY_LIMIT, "threads": config.DB_THREADS}


def prune(dest: Path, keep: int = KEEP) -> list[str]:
    """Delete all but the newest `keep` backups. Only names matching BACKUP_NAME are touched."""
    ours = sorted(p.name for p in dest.iterdir() if p.is_file() and BACKUP_NAME.match(p.name))
    old = ours[:-keep] if keep > 0 else ours
    for name in old:
        (dest / name).unlink()
    return old


def backup(db_path: Path, dest: Path | None, today: date, keep: int = KEEP,
           other_disk_only: bool = True) -> dict:
    """Copy the database to dest/edgar-<today>.duckdb, check the copy, then keep the last `keep`.
    status: ok | skipped (not configured, folder missing, same disk, no room) | failed."""
    t0 = time.monotonic()

    def out(status: str, message: str, **kw) -> dict:
        return {"status": status, "message": message, "path": None, "bytes": 0, "removed": [],
                "seconds": round(time.monotonic() - t0, 1), **kw}

    if dest is None:
        return out("skipped", "EOQ_BACKUP_DIR not set")
    dest, db_path = Path(dest), Path(db_path)
    if not dest.is_dir():
        return out("skipped", f"backup folder not found: {dest} (drive unplugged?)")
    if other_disk_only and os.stat(dest).st_dev == os.stat(db_path).st_dev:
        return out("skipped", f"{dest} is on the same disk as the database")
    for p in dest.iterdir():
        if PART_NAME.match(p.name):
            p.unlink()                                   # an earlier backup that was cut short
    size, free = db_path.stat().st_size, shutil.disk_usage(dest)[2]
    if free < size * ROOM:
        return out("skipped", f"not enough room on {dest}: need {_gb(size * ROOM)}, {_gb(free)} free")
    target = dest / f"edgar-{today}.duckdb"
    part = dest / f".{target.name}.part"
    try:
        # The open connection holds DuckDB's file lock, so no other process can write during the copy.
        with duckdb.connect(str(db_path), config=_db_config()) as con:
            con.execute("CHECKPOINT")
            expected = con.execute("SELECT count(*) FROM loaded_filings").fetchall()[0][0]
            shutil.copyfile(db_path, part)
            with open(part, "rb+") as f:
                os.fsync(f.fileno())
        with duckdb.connect(str(part), read_only=True, config=_db_config()) as copy:
            got = copy.execute("SELECT count(*) FROM loaded_filings").fetchall()[0][0]
        if got != expected:
            raise OSError(f"copy has {got} loaded filings, database {expected}")
        os.replace(part, target)
    except Exception as e:
        part.unlink(missing_ok=True)
        return out("failed", f"{type(e).__name__}: {e}")
    removed = prune(dest, keep)
    return out("ok", f"{_gb(size)} copied to {target}", path=str(target), bytes=target.stat().st_size,
               removed=removed)


class RunLocked(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path):
    """An exclusive lock for the length of a run. The OS frees it if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLocked(f"another run holds {path}") from None
        yield
    finally:
        f.close()


def table_counts(con: duckdb.DuckDBPyConnection) -> dict:
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchall()[0][0] for t in TABLES}


def _shrank(table: str, before: int, now: int) -> bool:
    """Ledgers only grow. Other tables are snapshots (SEC's ticker list, refreshed price windows,
    replaced filings) that lose a few rows normally; only a drop beyond DRIFT is a warning."""
    return now < before and (table in LEDGERS or now < before * (1 - DRIFT))


def _warnings(con, today: date, counts: dict, before: dict, steps: list[dict], throttled: int,
              dropped: int, backup: dict | None) -> list[str]:
    w = [f"{s['name']} failed: {s['message']}" for s in steps if s["status"] == "failed"]
    w += [f"{t} fell from {before[t]:,} to {n:,}" for t, n in counts.items()
          if t in before and _shrank(t, before[t], n)]
    failed_f4 = con.execute("SELECT count(*) FROM form4_filings WHERE status = 'failed'").fetchall()[0][0]
    if failed_f4:
        w.append(f"{failed_f4:,} Form 4 filings failed (retried next run)")
    failed_px = con.execute("SELECT count(*) FROM price_status WHERE status = 'error'").fetchall()[0][0]
    if failed_px:
        w.append(f"{failed_px:,} companies failed their price refresh")
    latest = con.execute("SELECT max(price_date) FROM prices").fetchall()[0][0]
    if latest is None:
        w.append("no prices in the database")
    elif (today - latest).days > STALE_RUN_DAYS:
        w.append(f"latest price is {latest} ({(today - latest).days} days old)")
    if throttled:
        w.append(f"SEC throttled {throttled} requests (backed off and retried)")
    if dropped:
        w.append(f"SEC dropped {dropped} connections (retried)")
    if backup and backup["status"] != "ok":
        w.append(f"backup {backup['status']}: {backup['message']}")
    return w


def run_steps(con: duckdb.DuckDBPyConnection, job: str, steps: list[tuple[str, Callable[[], object]]],
              today: date, throttled: Callable[[], int] = lambda: 0, backup: dict | None = None,
              dropped: Callable[[], int] = lambda: 0) -> dict:
    """Run steps in order; after a failure the rest are 'not run'. Logs the run and returns it
    with its health warnings. A step that calls sys.exit is a failure, not the end of the run."""
    started, results, failed = datetime.now(), [], False
    for name, fn in steps:
        if failed:
            results.append({"name": name, "status": "not run", "seconds": 0, "message": None})
            continue
        t0 = time.monotonic()
        try:
            fn()
            status, message = "ok", None
        except (Exception, SystemExit) as e:
            failed, status = True, "failed"
            message = (str(e.code) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}")[:500]
        results.append({"name": name, "status": status, "seconds": round(time.monotonic() - t0, 1),
                        "message": message})
    counts = table_counts(con)
    prev = con.execute("SELECT counts FROM run_log ORDER BY started DESC LIMIT 1").fetchall()
    before = json.loads(prev[0][0]) if prev else {}
    n_throttled = throttled()
    warnings = _warnings(con, today, counts, before, results, n_throttled, dropped(), backup)
    r = {"job": job, "started": started, "finished": datetime.now(), "status": "failed" if failed else "ok",
         "steps": results, "counts": counts, "before": before, "throttled": n_throttled, "backup": backup,
         "warnings": warnings}
    con.execute("INSERT INTO run_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [started, r["finished"], job, r["status"], json.dumps(results), json.dumps(counts), n_throttled,
                 json.dumps(backup) if backup else None, json.dumps(warnings)])
    return r


def render_health(r: dict) -> str:
    lines = [f"# Health: {r['job']} run of {r['started']:%Y-%m-%d %H:%M}", "",
             f"Status: **{r['status']}**, {(r['finished'] - r['started']).total_seconds():,.0f} s.", "",
             "## Warnings", ""]
    lines += [f"- {w}" for w in r["warnings"]] or ["None."]
    lines += ["", "## Steps", "", "| Step | Status | Seconds | Message |", "|---|---|---|---|"]
    lines += [f"| {s['name']} | {s['status']} | {s['seconds']} | {(s['message'] or '').replace('|', '/')} |"
              for s in r["steps"]]
    if r["backup"]:
        b = r["backup"]
        lines += ["", "## Backup", "", f"{b['status']}: {b['message']}"
                  + (f". Removed: {', '.join(b['removed'])}" if b.get("removed") else "")]
    lines += ["", "## Row counts", "", "| Table | Before | Now | Change |", "|---|---|---|---|"]
    for t, n in r["counts"].items():
        b = r["before"].get(t)
        lines.append(f"| {t} | {'-' if b is None else f'{b:,}'} | {n:,} | {'-' if b is None else f'{n - b:+,}'} |")
    return "\n".join(lines) + "\n"


def write_schedule(out_dir: Path, repo: Path, uv: str) -> list[Path]:
    """launchd agents for `run weekly` and `run daily` (written here; installing is up to you).
    caffeinate keeps the Mac awake while a run lasts; a run missed while asleep starts on wake."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for job in ("weekly", "daily"):
        label = f"com.edgar-obsidian-quant.{job}"
        log = str(repo / "data" / "logs" / f"{job}.log")
        plist = {"Label": label, "WorkingDirectory": str(repo),
                 "ProgramArguments": ["/usr/bin/caffeinate", "-i", uv, "run", "--env-file", ".env",
                                      "python", "-m", "src.cli", "run", job],
                 "StartCalendarInterval": SCHEDULE[job], "StandardOutPath": log, "StandardErrorPath": log,
                 # Unbuffered: a run that is killed or crashes still leaves its log up to that point.
                 "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"}}
        p = out_dir / f"{label}.plist"
        p.write_bytes(plistlib.dumps(plist))
        paths.append(p)
    return paths
