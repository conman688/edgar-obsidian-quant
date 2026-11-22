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

`--write --expect-hash H` re-derives the plan under the same lock and writes only if its hash is
H, the reviewed one. A plan with nothing to write is a no-op, so a second write changes nothing.
Inside one transaction it then checks that the target's rows are exactly the old rows plus the
plan's, that every other CIK's rows are unchanged, and that a fresh plan has nothing left to write;
otherwise it rolls back. Writing still needs the user's separate yes: the hash gate cannot ask.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
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


def apply(con, cik: int, source: dict, expect_hash: str) -> tuple[int, str]:
    """Write the plan if its hash is the reviewed one. (exit code, message)."""
    m = plan(con, cik, source)
    p = m["plan"]
    if p["blocked"]:
        return 2, f"blocked, nothing written: {p['blocked']}"
    if _nothing_to_write(p):
        return 0, f"nothing to write (already applied); plan hash {m['hash']}"
    if m["hash"] != expect_hash:
        return 2, f"plan hash {m['hash']} does not match the reviewed {expect_hash}: nothing written"
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
    n = {t: len(p[t]["insert"]) for t in TABLES}
    return 0, f"written: {n}, first_date {p['price_status']['set_first_date']}; plan hash {m['hash']}"


def summary(m: dict) -> str:
    p = m["plan"]
    parts = [f"{t}: insert {len(p[t]['insert'])}, keep_live {len(p[t]['keep_live'])}, "
             f"already_present {len(p[t]['already_present'])}" for t in TABLES]
    return "\n".join(parts + [f"price_status.set_first_date: {p['price_status']['set_first_date']}",
                              f"blocked: {p['blocked']}", f"manifest sha256: {m['hash']}"])


def _refuse_out(path: str) -> str:
    return f"refused: --out {path} already exists (or is a link); nothing read or written"


def write_new(path: str, text: str) -> None:
    """Create `path` and write `text`; never truncate or follow an existing name (review C1)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.repair", description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="the database file, e.g. data/edgar.duckdb")
    ap.add_argument("--cik", type=int, required=True)
    ap.add_argument("--prices", required=True, help="preserved prices Parquet (SHA256SUMS beside it)")
    ap.add_argument("--dividends", required=True)
    ap.add_argument("--splits", required=True)
    ap.add_argument("--out", help="write the manifest JSON to this new file (an existing path is refused)")
    ap.add_argument("--write", action="store_true", help="write the plan (default: dry run)")
    ap.add_argument("--expect-hash", help="the reviewed manifest sha256; required with --write")
    a = ap.parse_args(argv)
    if a.write and not a.expect_hash:
        ap.error("--write needs --expect-hash, the reviewed manifest sha256")
    if a.out and os.path.lexists(a.out):      # the database, an input, a link: never overwritten
        print(_refuse_out(a.out))
        return 2
    try:
        source = read_source({"prices": a.prices, "dividends": a.dividends, "splits": a.splits}, a.cik)
    except SourceError as e:
        print(f"source refused: {e}")
        return 2
    db = Path(a.db)
    if not db.exists():
        print(f"{db} does not exist")
        return 2
    try:
        with ops.run_lock(db.parent / ".run.lock"):
            con = duckdb.connect(str(db), read_only=not a.write, config=DB_CONFIG)
            try:
                v, known = schema_version(con), max(n for n, _ in MIGRATIONS)
                if not MIN_SCHEMA <= v <= known:
                    print(f"schema version {v} is outside {MIN_SCHEMA}..{known}: nothing read or written")
                    return 2
                if a.write:
                    code, msg = apply(con, a.cik, source, a.expect_hash)
                    print(msg)
                    return code
                m = plan(con, a.cik, source)
            finally:
                con.close()
    except ops.RunLocked as e:
        print(f"not started: {e}")
        return 3
    m["context"]["db"] = str(db)
    if a.out:
        try:
            write_new(a.out, json.dumps(m, indent=1, sort_keys=True, allow_nan=False) + "\n")
        except FileExistsError:                # appeared after the check above
            print(f"refused: --out {a.out} already exists (or is a link); the database was read "
                  f"only, nothing written")
            return 2
    print(summary(m))
    print("dry run: nothing written" + (f"; manifest in {a.out}" if a.out else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
