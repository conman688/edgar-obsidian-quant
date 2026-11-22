"""Restore one company's preserved price history: CIK 835011 (MGP Ingredients, MGPI -> CASK).

The 2026-11-07 weekly deleted its MGPI prices and dividends when the primary ticker changed
(fixed for the future by PR #30). The rows were preserved as Parquet from the pre-loss backup
(`cik835011-preserve-2026-11-14/` in the checks folder, with SHA256SUMS).

Dry run by default. It opens the database with plain duckdb, read-only, 2 GB and 2 threads, under
the run lock, never through `src.db.connect` (which would migrate), and prints a manifest:

- `plan`: every key and value the write would insert, every key where the live row is kept and
  why, the price_status change, and anything that blocks the write. Hashed.
- `hash`: sha256 of the plan as canonical JSON (sorted keys, no spaces, no NaN).
- `context`: what was read, not hashed: schema version, the target's live rows, held source
  actions, and row count and xor-of-row-hashes for every other CIK, to compare after a write.

Policy (also in the plan, so the hash covers it): see POLICY.

`--write --expect-hash H --backup-dir D --result R.json` does everything under one run-lock hold
and one read-write connection (review PRICES-cik835011-apply C1): plan; refuse unless the hash is
H; CHECKPOINT and copy the database to a new file in D (another disk), checked by sha256; re-plan
and compare with the first plan; write; re-plan. DuckDB's file lock keeps every other process out
from the plan to the commit, so the backup is exactly the pre-write state. Inside one transaction
the write checks that the target's rows are exactly the old rows plus the plan's, that every other
CIK's rows are unchanged, and that a fresh plan has nothing left to write; otherwise it rolls back.
R records the outcome (refused, nothing_to_write, committed or error), the backup's path and
sha256, the pre- and post-write baselines and the database's sha256 after the write. A plan with
nothing to write is a no-op. Stdout is not the record (C2): `--classify REVIEWED.json`, a dry run,
names the state as not_applied, applied or unknown.

`--rollback --result-of R.json` puts the backup back only if: R says committed, there is no WAL,
the database is byte-identical to the state the write left, the backup matches its sha256, and no
process holds the database. It stages the backup as a new file beside the database, keeps the
repaired file under a new name and swaps them with one os.replace (C3).

Writing or rolling back still needs the user's separate yes: the gates cannot ask.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path

import duckdb

from src import ops
from src.db.schema import MIGRATIONS

DB_CONFIG = {"memory_limit": "2GB", "threads": 2}
MIN_SCHEMA = 8          # run_log (8) is the oldest live schema this was written against
POLICY = ("Insert a source row only where its key is absent from the live table. Where the live "
          "table already holds the key, the live row is kept unchanged (live wins: current rows and "
          "source corrections); it is listed as already_present if identical, else as keep_live with "
          "both values. Nothing is deleted. Nothing is updated except price_status.first_date, lowered "
          "with least() to the earliest inserted price date. Restored rows keep their own source "
          "ticker. Only rows of the target CIK are written; a live split inside the restored range "
          "that the source lacks blocks the write.")

# table -> (key column, value columns, Parquet/live column types in order)
TABLES = {
    "prices": ("price_date", ("ticker", "close_raw", "volume_raw", "source"),
               [("cik", "INTEGER"), ("ticker", "VARCHAR"), ("price_date", "DATE"), ("close_raw", "DOUBLE"),
                ("volume_raw", "DOUBLE"), ("source", "VARCHAR")]),
    "dividends": ("ex_date", ("amount_raw", "source"),
                  [("cik", "INTEGER"), ("ex_date", "DATE"), ("amount_raw", "DOUBLE"), ("source", "VARCHAR")]),
    "splits": ("ex_date", ("ratio", "source"),
               [("cik", "INTEGER"), ("ex_date", "DATE"), ("ratio", "DOUBLE"), ("source", "VARCHAR")]),
}
NULLABLE = {"volume_raw"}
CHECKED = ("prices", "dividends", "splits", "price_status", "price_actions_held")


class SourceError(ValueError):
    """The preserved snapshot is not what it claims to be: wrong checksum, CIK, key or type."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sums(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SourceError(f"{path} is missing: the source cannot be verified")
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            out[name.strip()] = digest
    return out


def _bad(v) -> bool:
    return v is None or (isinstance(v, float) and v != v)


def read_source(paths: dict[str, Path], cik: int) -> dict:
    """{'files': {name: sha256}, 'rows': {table: {key date: [values]}}} from the preserved Parquet.

    Each file must match the SHA256SUMS beside it, have exactly the live table's columns and
    types, hold only `cik`, one row per key, and no missing or NaN value (volume may be NULL)."""
    con = duckdb.connect()
    try:
        files, rows = {}, {}
        for table, (key, values, types) in TABLES.items():
            p = Path(paths[table])
            digest, listed = _sha256(p), _sums(p.parent / "SHA256SUMS").get(p.name)
            if digest != listed:
                raise SourceError(f"{p}: SHA256 {digest} does not match SHA256SUMS ({listed})")
            got = [(n, t) for n, t, *_ in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(p)]).fetchall()]
            if got != types:
                raise SourceError(f"{p}: columns {got}, expected {types}")
            cols = ", ".join(n for n, _ in types)
            data = con.execute(f"SELECT {cols} FROM read_parquet(?) ORDER BY {key}", [str(p)]).fetchall()
            other = sorted({r[0] for r in data if r[0] != cik}, key=str)
            if other:
                raise SourceError(f"{p}: rows of other CIK(s) {other}")
            by_key: dict[date, list] = {}
            for r in data:
                rec = dict(zip((n for n, _ in types), r))
                if rec[key] in by_key:
                    raise SourceError(f"{p}: duplicate key {rec[key]}")
                if _bad(rec[key]) or any(_bad(rec[c]) for c in values if c not in NULLABLE):
                    raise SourceError(f"{p}: missing or NaN value in {rec}")
                if any(isinstance(rec[c], float) and rec[c] != rec[c] for c in values):
                    raise SourceError(f"{p}: NaN value in {rec}")
                by_key[rec[key]] = [rec[c] for c in values]
            files[p.name] = digest
            rows[table] = by_key
        return {"files": files, "rows": rows}
    finally:
        con.close()


def _tables(con) -> set[str]:
    return {t for (t,) in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database()").fetchall()}


def schema_version(con) -> int:
    return con.execute("SELECT max(version) FROM schema_migrations").fetchall()[0][0]


def _live(con, table: str, cik: int) -> list[tuple]:
    key, values, types = TABLES[table]
    got = [(n, t) for n, t, *_ in con.execute(f"DESCRIBE {table}").fetchall()]
    if got != types:
        raise RuntimeError(f"live {table} has columns {got}, expected {types}")
    return con.execute(f"SELECT {key}, {', '.join(values)} FROM {table} WHERE cik = ? ORDER BY {key}, "
                       f"{', '.join(values)}", [cik]).fetchall()


def fingerprints(con, cik: int, other: bool) -> dict[str, list]:
    """[row count, xor of row hashes] per checked table, for the other CIKs or for the target."""
    op = "<>" if other else "="
    return {t: list(con.execute(f"SELECT count(*), bit_xor(hash(x)) FROM {t} x WHERE cik {op} ?", [cik]).fetchall()[0])
            for t in CHECKED if t in _tables(con)}


def _iso(v):
    return v.isoformat() if isinstance(v, date) else v


def plan(con, cik: int, source: dict) -> dict:
    """The exact manifest against the database as it is now. Reads only."""
    blocked, out = [], {"cik": cik, "policy": POLICY, "source": dict(sorted(source["files"].items()))}
    live_rows = {}
    for table in TABLES:
        rows = _live(con, table, cik)
        live: dict[date, list] = {}
        for k, *v in rows:
            if k in live:
                blocked.append(f"live {table} holds key {k.isoformat()} more than once")
            live[k] = list(v)
        live_rows[table] = rows
        src = source["rows"][table]
        out[table] = {
            "insert": [[k.isoformat(), *src[k]] for k in sorted(src) if k not in live],
            "keep_live": [[k.isoformat(), src[k], live[k]] for k in sorted(src) if k in live and live[k] != src[k]],
            "already_present": [k.isoformat() for k in sorted(src) if k in live and live[k] == src[k]],
        }
    restored = sorted(source["rows"]["prices"])
    if restored:
        for k, (ratio, _) in sorted(dict((k, v) for k, *v in live_rows["splits"]).items()):
            if restored[0] <= k <= restored[-1] and k not in source["rows"]["splits"]:
                blocked.append(f"live split {k.isoformat()} (ratio {ratio}) is inside the restored range "
                               f"and not in the source")
    status = con.execute("SELECT ticker, status, first_date, last_date, message, fetched_at FROM price_status "
                         "WHERE cik = ?", [cik]).fetchall()
    inserted = [date.fromisoformat(r[0]) for r in out["prices"]["insert"]]
    first = None
    if not status:
        blocked.append("no price_status row for the target")
    elif inserted and (status[0][2] is None or inserted[0] < status[0][2]):
        first = inserted[0].isoformat()
    out["price_status"] = {"set_first_date": first}
    out["blocked"] = blocked
    held = None
    if "price_actions_held" in _tables(con):
        held = [[_iso(v) for v in r] for r in con.execute(
            "SELECT kind, ex_date, value, ticker, source, held_at FROM price_actions_held WHERE cik = ? "
            "ORDER BY ex_date, kind, ticker", [cik]).fetchall()]
    context = {
        "schema_version": schema_version(con),
        "target": {t: {"rows": len(r), "first": _iso(r[0][0]) if r else None, "last": _iso(r[-1][0]) if r else None,
                       "tickers": sorted({x[1] for x in r}) if t == "prices" else None}
                   for t, r in live_rows.items()},
        "target_price_status": [_iso(v) for v in status[0]] if status else None,
        "held_actions": held,
        "target_fingerprints": fingerprints(con, cik, other=False),
        "other_ciks": fingerprints(con, cik, other=True),
    }
    return {"hash": manifest_hash(out), "plan": out, "context": context}


def manifest_hash(p: dict) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _nothing_to_write(p: dict) -> bool:
    return not any(p[t]["insert"] for t in TABLES) and p["price_status"]["set_first_date"] is None


REQUIRE_OTHER_DISK = True   # the pre-write backup must survive the loss of the database's disk
BACKUP_PREFIX = "edgar-pre-repair-"   # never matches ops.BACKUP_NAME, so no rotation prunes it


class Refused(RuntimeError):
    """A write or rollback gate did not hold: nothing was written or replaced."""


def _after_backup() -> None:
    """Called between the verified backup and the write. A no-op; tests inject a writer here."""


def _wal(db: Path) -> Path:
    return db.with_name(db.name + ".wal")


def _sha256_fh(fh) -> str:
    h = hashlib.sha256()
    fh.seek(0)
    for chunk in iter(lambda: fh.read(1 << 24), b""):
        h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    """Never call this on the database while this process has it open in DuckDB: closing any
    descriptor of a file drops all of the process's fcntl locks on it, DuckDB's included."""
    with open(path, "rb") as f:
        return _sha256_fh(f)


def _copy_new(fh, dst: Path) -> None:
    """Copy from an open file into a file that must not exist yet; fsync it; remove it on failure."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            fh.seek(0)
            for chunk in iter(lambda: fh.read(1 << 24), b""):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


def backup_under(con, db: Path, fh, dest: Path, cik: int) -> dict:
    """Copy the database while `con`, a read-write connection, holds DuckDB's file lock: no other
    process can open the file, so the copy is exactly the state the write starts from. `fh` is a
    read handle opened before `con` and closed after it; the copy and its check read only through
    it, because opening and closing the file again would release DuckDB's lock."""
    if not dest.is_dir():
        raise Refused(f"backup folder {dest} does not exist")
    if REQUIRE_OTHER_DISK and os.stat(dest).st_dev == os.stat(db).st_dev:
        raise Refused(f"backup folder {dest} is on the same disk as the database")
    if shutil.disk_usage(dest).free < os.stat(db).st_size * ops.ROOM:
        raise Refused(f"not enough room in {dest}")
    con.execute("CHECKPOINT")
    if os.path.lexists(_wal(db)):
        raise Refused(f"WAL {_wal(db)} exists after CHECKPOINT")
    target = dest / f"{BACKUP_PREFIX}{cik}-{datetime.now():%Y%m%dT%H%M%S%f}.duckdb"
    _copy_new(fh, target)
    digest = sha256_file(target)
    if digest != _sha256_fh(fh):
        target.unlink()
        raise Refused("the backup copy does not match the database")
    return {"path": str(target), "sha256": digest, "bytes": target.stat().st_size}


def _baseline(context: dict) -> dict:
    keep = ("schema_version", "target", "target_price_status", "held_actions", "target_fingerprints", "other_ciks")
    return {k: context[k] for k in keep}


def write(con, db: Path, fh, cik: int, source: dict, expect_hash: str, backup_dir: Path) -> dict:
    """Plan, back up, write: one lock hold and one connection, so nothing can change in between
    (review PRICES-cik835011-apply C1). Returns the result record; raises Refused before any write."""
    m = plan(con, cik, source)
    p = m["plan"]
    if p["blocked"]:
        raise Refused(f"blocked: {p['blocked']}")
    if _nothing_to_write(p):
        return {"status": "nothing_to_write", "plan_hash": m["hash"],
                "message": f"nothing to write (already applied); plan hash {m['hash']}"}
    if m["hash"] != expect_hash:
        raise Refused(f"plan hash {m['hash']} does not match the reviewed {expect_hash}")
    backup = backup_under(con, db, fh, backup_dir, cik)
    _after_backup()
    again = plan(con, cik, source)
    if again["hash"] != m["hash"] or _baseline(again["context"]) != _baseline(m["context"]):
        raise Refused("the database changed between the plan and the backup")
    _commit(con, cik, source, again)
    post = plan(con, cik, source)
    n = {t: len(p[t]["insert"]) for t in TABLES}
    return {"status": "committed", "plan_hash": m["hash"], "written": n,
            "set_first_date": p["price_status"]["set_first_date"], "backup": backup,
            "pre": _baseline(m["context"]), "post": _baseline(post["context"]),
            "message": f"written: {n}, first_date {p['price_status']['set_first_date']}; plan hash {m['hash']}"}


def _commit(con, cik: int, source: dict, m: dict) -> None:
    """Insert the plan's rows in one transaction, check the result, commit or roll back."""
    p = m["plan"]
    before_target = {t: _live(con, t, cik) for t in TABLES}
    before_status = con.execute("SELECT * FROM price_status WHERE cik = ?", [cik]).fetchall()
    con.begin()
    try:
        before_other = fingerprints(con, cik, other=True)
        for table, (key, values, _) in TABLES.items():
            rows = [[cik, date.fromisoformat(r[0]), *r[1:]] for r in p[table]["insert"]]
            if rows:
                cols = ", ".join(("cik", key) + values)
                con.executemany(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(rows[0]))})", rows)
        if p["price_status"]["set_first_date"]:
            con.execute("UPDATE price_status SET first_date = least(first_date, ?) WHERE cik = ?",
                        [date.fromisoformat(p["price_status"]["set_first_date"]), cik])
        problems = []
        if fingerprints(con, cik, other=True) != before_other:
            problems.append("rows of another CIK changed")
        for table in TABLES:
            want = sorted(before_target[table] + [(date.fromisoformat(r[0]), *r[1:]) for r in p[table]["insert"]],
                          key=repr)
            if sorted(_live(con, table, cik), key=repr) != want:
                problems.append(f"{table}: target rows are not the old rows plus the plan's")
        after_status = con.execute("SELECT * FROM price_status WHERE cik = ?", [cik]).fetchall()
        if [r[:3] + r[4:] for r in after_status] != [r[:3] + r[4:] for r in before_status]:
            problems.append("price_status changed beyond first_date")
        again = plan(con, cik, source)["plan"]
        if not _nothing_to_write(again) or again["blocked"]:
            problems.append("a fresh plan after the write still has work")
        if problems:
            raise RuntimeError("; ".join(problems))
        con.commit()
    except BaseException:
        con.rollback()
        raise


def classify(m: dict, reviewed: dict) -> str:
    """The target's state against the reviewed manifest, from a read-only plan: not_applied (the
    reviewed plan is still pending), applied (exactly its rows are present), or unknown."""
    if m["hash"] == reviewed["hash"]:
        return "not_applied"
    p, rp = m["plan"], reviewed["plan"]
    if p["blocked"] or p["price_status"]["set_first_date"] is not None:
        return "unknown"
    for t in TABLES:
        want = sorted(set(rp[t]["already_present"]) | {r[0] for r in rp[t]["insert"]})
        if p[t]["insert"] or sorted(p[t]["already_present"]) != want or p[t]["keep_live"] != rp[t]["keep_live"]:
            return "unknown"
    first = rp["price_status"]["set_first_date"]
    if first is not None and (m["context"]["target_price_status"] or [None] * 3)[2] != first:
        return "unknown"
    return "applied"


def rollback(db: Path, result: dict) -> str:
    """Put the pre-write backup back (review PRICES-cik835011-apply C3). Every gate must hold:
    the write committed; no WAL; the database is byte-identical to the state the write left; the
    backup is byte-identical to what was recorded; no process holds the database. The backup is
    staged as a new file on the database's volume, the repaired file is kept under a new name
    (a hard link), then one os.replace swaps them. Raises Refused before any change."""
    if result.get("status") != "committed":
        raise Refused(f"the result says {result.get('status')!r}, not 'committed'")
    if os.path.lexists(_wal(db)):
        raise Refused(f"WAL {_wal(db)} exists: a writer did not finish; escalate")
    if sha256_file(db) != result["post_sha256"]:
        raise Refused("the database changed since the repair (an intervening write); escalate")
    backup = Path(result["backup"]["path"])
    if sha256_file(backup) != result["backup"]["sha256"]:
        raise Refused(f"{backup} does not match its recorded sha256")
    try:
        duckdb.connect(str(db), read_only=True, config=DB_CONFIG).close()
    except duckdb.Error as e:
        raise Refused(f"the database is open in another process: {e}") from None
    stamp = f"{datetime.now():%Y%m%dT%H%M%S%f}"
    stage, kept = db.with_name(f".{db.name}.rollback-{stamp}.part"), db.with_name(f"{db.name}.repaired-{stamp}")
    with open(backup, "rb") as f:
        _copy_new(f, stage)
    try:
        if sha256_file(stage) != result["backup"]["sha256"]:
            raise Refused("the staged copy does not match the backup")
        os.link(db, kept)
    except BaseException:
        stage.unlink(missing_ok=True)
        raise
    try:
        os.replace(stage, db)
    except BaseException:
        stage.unlink(missing_ok=True)
        kept.unlink(missing_ok=True)     # a second name for the unchanged database file
        raise
    dfd = os.open(db.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    if sha256_file(db) != result["backup"]["sha256"] or os.path.lexists(_wal(db)):
        raise RuntimeError(f"after the swap {db} is not the backup; the repaired state is at {kept}")
    return f"rolled back to {backup}; the repaired state is kept at {kept}"


def summary(m: dict) -> str:
    p = m["plan"]
    parts = [f"{t}: insert {len(p[t]['insert'])}, keep_live {len(p[t]['keep_live'])}, "
             f"already_present {len(p[t]['already_present'])}" for t in TABLES]
    return "\n".join(parts + [f"price_status.set_first_date: {p['price_status']['set_first_date']}",
                              f"blocked: {p['blocked']}", f"manifest sha256: {m['hash']}"])


def _say(msg: str) -> None:
    """Print, but a closed stdout never changes the outcome: the result file is the record
    (review PRICES-cik835011-apply C2)."""
    try:
        print(msg)
    except OSError:
        pass


def _new_json(path: str, flag: str) -> str | None:
    """Why `path` may not be written, or None. A manifest or result is a new .json file (review
    C1, C2): database-owned names (the database, its .wal, the run lock) never end in .json, so they
    stay reserved even when absent and whatever the volume's case rules."""
    if os.path.lexists(path):
        return f"refused: {flag} {path} already exists (or is a link); nothing read or written"
    if Path(path).suffix.lower() != ".json":
        return (f"refused: {flag} {path} must be a new .json file (database, WAL and run-lock names are "
                f"reserved); nothing read or written")
    return None


def write_new(path: str, text: str) -> None:
    """Create `path` and write `text`; never truncate or follow an existing name (review C1)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _record(path: str, obj: dict) -> None:
    """Rewrite our own result file (created by write_new at the start of the run)."""
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(obj, indent=1, sort_keys=True, allow_nan=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.repair", description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="the database file, e.g. data/edgar.duckdb")
    ap.add_argument("--cik", type=int, required=True)
    ap.add_argument("--prices", required=True, help="preserved prices Parquet (SHA256SUMS beside it)")
    ap.add_argument("--dividends", required=True)
    ap.add_argument("--splits", required=True)
    ap.add_argument("--out", help="write the manifest to this new .json file (an existing path is refused)")
    ap.add_argument("--classify", help="dry run: name the state against this reviewed manifest .json")
    ap.add_argument("--write", action="store_true", help="back up, then write the plan (default: dry run)")
    ap.add_argument("--expect-hash", help="the reviewed manifest sha256; required with --write")
    ap.add_argument("--backup-dir", help="existing folder on another disk for the pre-write copy; with --write")
    ap.add_argument("--result", help="new .json file recording the write's outcome; with --write")
    ap.add_argument("--rollback", action="store_true", help="put a write's backup back (gated, atomic)")
    ap.add_argument("--result-of", help="the write's result .json; required with --rollback")
    a = ap.parse_args(argv)
    if sum(map(bool, (a.write, a.rollback, a.classify))) > 1:
        ap.error("--write, --rollback and --classify are separate runs")
    if a.write and not (a.expect_hash and a.backup_dir and a.result):
        ap.error("--write needs --expect-hash (the reviewed manifest sha256), --backup-dir and --result")
    if a.rollback and not a.result_of:
        ap.error("--rollback needs --result-of, the write's result file")
    db = Path(a.db)
    for flag, path in (("--out", a.out), ("--result", a.result)):
        if path and (why := _new_json(path, flag)):
            _say(why)
            return 2
    if a.rollback:
        try:
            with ops.run_lock(db.parent / ".run.lock"):
                msg = rollback(db, json.loads(Path(a.result_of).read_text()))
        except ops.RunLocked as e:
            _say(f"not started: {e}; nothing replaced")
            return 3
        except Refused as e:
            _say(f"refused: {e}; nothing replaced")
            return 2
        _say(msg)
        return 0
    try:
        source = read_source({"prices": a.prices, "dividends": a.dividends, "splits": a.splits}, a.cik)
    except SourceError as e:
        _say(f"source refused: {e}")
        return 2
    if not db.exists():
        _say(f"{db} does not exist")
        return 2
    if a.write:
        return _write_run(a, db, source)
    try:
        with ops.run_lock(db.parent / ".run.lock"):
            con = duckdb.connect(str(db), read_only=True, config=DB_CONFIG)
            try:
                v, known = schema_version(con), max(n for n, _ in MIGRATIONS)
                if not MIN_SCHEMA <= v <= known:
                    _say(f"schema version {v} is outside {MIN_SCHEMA}..{known}: nothing read or written")
                    return 2
                m = plan(con, a.cik, source)
            finally:
                con.close()
    except ops.RunLocked as e:
        _say(f"not started: {e}")
        return 3
    m["context"]["db"] = str(db)
    if a.out:
        try:
            write_new(a.out, json.dumps(m, indent=1, sort_keys=True, allow_nan=False) + "\n")
        except FileExistsError:                # appeared after the check above
            _say(f"refused: --out {a.out} already exists (or is a link); the database was read "
                 f"only, nothing written")
            return 2
    _say(summary(m))
    if a.classify:
        reviewed = json.loads(Path(a.classify).read_text())
        if manifest_hash(reviewed["plan"]) != reviewed["hash"]:
            _say(f"refused: {a.classify} does not hash to its own recorded hash")
            return 2
        _say(f"state: {classify(m, reviewed)} (against reviewed {reviewed['hash']})")
    _say("dry run: nothing written" + (f"; manifest in {a.out}" if a.out else ""))
    return 0


def _write_run(a, db: Path, source: dict) -> int:
    """--write: one result file per attempt, created before anything is read, rewritten at the end."""
    try:
        write_new(a.result, json.dumps({"status": "started", "started": datetime.now().isoformat()}) + "\n")
    except FileExistsError:
        _say(f"refused: --result {a.result} already exists (or is a link); nothing read or written")
        return 2
    code = 0
    try:
        if os.path.lexists(_wal(db)):
            raise Refused(f"WAL {_wal(db)} exists: a writer did not finish; escalate")
        with ops.run_lock(db.parent / ".run.lock"):
            # Held open for the connection's whole life and closed after it: closing any descriptor
            # of the database would drop DuckDB's fcntl lock while the connection is still open.
            with open(db, "rb") as fh:
                con = duckdb.connect(str(db), read_only=False, config=DB_CONFIG)
                try:
                    v, known = schema_version(con), max(n for n, _ in MIGRATIONS)
                    if not MIN_SCHEMA <= v <= known:
                        raise Refused(f"schema version {v} is outside {MIN_SCHEMA}..{known}")
                    record = write(con, db, fh, a.cik, source, a.expect_hash, Path(a.backup_dir))
                finally:
                    con.close()
            if record["status"] == "committed":
                record["post_sha256"] = sha256_file(db)
                record["post_wal"] = os.path.lexists(_wal(db))
    except ops.RunLocked as e:
        record, code = {"status": "refused", "message": f"not started: {e}; nothing written"}, 3
    except Refused as e:
        record, code = {"status": "refused", "message": f"refused: {e}; nothing written"}, 2
    except Exception as e:
        record, code = {"status": "error", "message": f"error: {e!r}; classify the state read-only "
                                                      f"(--classify) before anything else"}, 1
    record.update(db=str(db), cik=a.cik, expect_hash=a.expect_hash, finished=datetime.now().isoformat())
    _record(a.result, record)
    _say(record["message"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
