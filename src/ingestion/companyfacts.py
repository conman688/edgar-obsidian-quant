"""Load SEC companyfacts JSON (bulk companyfacts.zip or /api/xbrl/companyfacts) into xbrl_facts.

Bulk zip is one download instead of thousands of API calls. Each fact keeps its
`filed` and `accn`, so restatements become new rows, not overwrites.

Idempotency: rows are inserted per whole filing (cik, accn) and only for filings
not yet in loaded_filings, inside one transaction per batch. A crash mid-load
leaves every filing either fully loaded or absent; a rerun fills in the rest.
A known filing whose snapshot now has more facts gets only the missing rows.
A corrected value under the same filing with an unchanged count is not detected.
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Iterator

import duckdb
import pyarrow as pa

from src.db import ledger

SCHEMA_VERSION = 1
KEEP_TAXONOMIES = {"us-gaap", "dei", "ifrs-full"}
KEEP_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT", "10-KT/A"}
_CIK_FILE = re.compile(r"CIK(\d{10})\.json$")

ARROW_SCHEMA = pa.schema([
    ("cik", pa.int32()), ("taxonomy", pa.string()), ("tag", pa.string()), ("unit", pa.string()),
    ("period_start", pa.date32()), ("period_end", pa.date32()), ("period_days", pa.int32()),
    ("period_kind", pa.string()), ("fy", pa.int32()), ("fp", pa.string()), ("form", pa.string()),
    ("accn", pa.string()), ("filed", pa.date32()), ("frame", pa.string()), ("value", pa.float64()),
])


@dataclass
class LoadStats:
    files: int = 0
    facts_seen: int = 0
    facts_inserted: int = 0
    filings_grown: int = 0      # known filings that gained facts in this snapshot
    seconds: float = 0.0
    skipped: bool = False


def period_kind(days: int) -> str:
    if days == 0:
        return "instant"
    if 80 <= days <= 100:
        return "quarterly"
    if 170 <= days <= 195:
        return "semiannual"
    if 260 <= days <= 285:
        return "ytd9m"
    if 340 <= days <= 380:   # 52/53-week years land at 364 or 371
        return "annual"
    return "other"


def iter_fact_rows(doc: dict) -> Iterator[tuple]:
    cik = int(doc["cik"])
    for taxonomy, tags in doc.get("facts", {}).items():
        if taxonomy not in KEEP_TAXONOMIES:
            continue
        for tag, body in tags.items():
            for unit, facts in body.get("units", {}).items():
                for f in facts:
                    if f.get("form") not in KEEP_FORMS or f.get("val") is None:
                        continue
                    end = date.fromisoformat(f["end"])
                    start = date.fromisoformat(f["start"]) if f.get("start") else end
                    # SEC durations are inclusive: Jan 1 - Dec 31 is 364 days by subtraction.
                    days = (end - start).days + (1 if f.get("start") else 0)
                    yield (
                        cik, taxonomy, tag, unit, start, end, days, period_kind(days),
                        f.get("fy"), f.get("fp"), f["form"], f["accn"],
                        date.fromisoformat(f["filed"]), f.get("frame"), float(f["val"]),
                    )


def _insert_batch(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> tuple[int, int]:
    """Insert one batch. Returns (facts inserted, known filings that gained facts)."""
    cols = list(zip(*rows))
    table = pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(cols, ARROW_SCHEMA)],
                                 schema=ARROW_SCHEMA)
    con.begin()
    try:
        con.register("batch_rows", table)
        con.execute("""
            CREATE OR REPLACE TEMP TABLE batch_filings AS
            SELECT cik, accn, count(*) AS n FROM (SELECT DISTINCT * FROM batch_rows) GROUP BY cik, accn
        """)
        con.execute("""
            CREATE OR REPLACE TEMP TABLE new_filings AS
            SELECT b.* FROM batch_filings b
            ANTI JOIN loaded_filings l ON l.cik = b.cik AND l.accn = b.accn
        """)
        con.execute("""
            CREATE OR REPLACE TEMP TABLE grown_filings AS
            SELECT b.* FROM batch_filings b
            JOIN loaded_filings l ON l.cik = b.cik AND l.accn = b.accn
            WHERE b.n > l.n_facts
        """)
        n = con.execute("""
            INSERT INTO xbrl_facts
            SELECT DISTINCT b.* FROM batch_rows b SEMI JOIN new_filings n ON n.cik = b.cik AND n.accn = b.accn
        """).fetchall()[0][0]
        grown = con.execute("SELECT count(*) FROM grown_filings").fetchall()[0][0]
        if grown:
            # Rare, so the scan of xbrl_facts for just these filings is affordable.
            n += con.execute("""
                INSERT INTO xbrl_facts
                SELECT b.* FROM batch_rows b SEMI JOIN grown_filings g ON g.cik = b.cik AND g.accn = b.accn
                EXCEPT
                SELECT f.* FROM xbrl_facts f SEMI JOIN grown_filings g ON g.cik = f.cik AND g.accn = f.accn
            """).fetchall()[0][0]
            con.execute("""
                UPDATE loaded_filings SET n_facts = g.n FROM grown_filings g
                WHERE loaded_filings.cik = g.cik AND loaded_filings.accn = g.accn
            """)
        con.execute("INSERT INTO loaded_filings SELECT cik, accn, n FROM new_filings")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.unregister("batch_rows")
    return n, grown


def load_docs(con: duckdb.DuckDBPyConnection, docs: Iterable[dict], batch_rows: int = 500_000,
              progress: Callable[[LoadStats], None] | None = None) -> LoadStats:
    """Insert facts from companyfacts documents. Batches always hold whole companies."""
    stats, buf, t0 = LoadStats(), [], time.monotonic()
    for doc in docs:
        stats.files += 1
        buf.extend(iter_fact_rows(doc))
        if len(buf) >= batch_rows:
            _flush(con, buf, stats)
            buf = []
            if progress:
                progress(stats)
    if buf:
        _flush(con, buf, stats)
    stats.seconds = time.monotonic() - t0
    return stats


def _flush(con: duckdb.DuckDBPyConnection, buf: list[tuple], stats: LoadStats) -> None:
    inserted, grown = _insert_batch(con, buf)
    stats.facts_seen += len(buf)
    stats.facts_inserted += inserted
    stats.filings_grown += grown


def iter_bulk_zip(path: Path, ciks: set[int] | None = None) -> Iterator[dict]:
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            m = _CIK_FILE.search(name)
            if not m or (ciks is not None and int(m.group(1)) not in ciks):
                continue
            doc = json.loads(z.read(name))
            if not doc.get("facts"):
                continue          # some files are just {}
            if not doc.get("cik"):
                doc["cik"] = int(m.group(1))  # a few files omit cik; the filename always carries it
            yield doc


def _key(sha256: str) -> str:
    return f"companyfacts@{sha256[:16]}"


def is_loaded(con: duckdb.DuckDBPyConnection, sha256: str | None) -> bool:
    """True only when a full load of this zip version finished."""
    return bool(sha256) and ledger.is_done(con, _key(sha256), "facts", SCHEMA_VERSION)


def load_zip(con: duckdb.DuckDBPyConnection, path: Path, sha256: str, ciks: set[int] | None = None,
             progress: Callable[[LoadStats], None] | None = None) -> LoadStats:
    """Load one companyfacts.zip version. A full load of a version already done is skipped."""
    key = _key(sha256)
    full = ciks is None
    if full and ledger.is_done(con, key, "facts", SCHEMA_VERSION):
        return LoadStats(skipped=True)
    if full:
        ledger.mark(con, key, "facts", SCHEMA_VERSION, "pending", sha256)
    try:
        stats = load_docs(con, iter_bulk_zip(path, ciks), progress=progress)
    except BaseException as e:
        if full:
            ledger.mark(con, key, "facts", SCHEMA_VERSION, "failed", sha256, error=repr(e)[:500])
        raise
    if full:
        ledger.mark(con, key, "facts", SCHEMA_VERSION, "done", sha256,
                    output_ref=f"files={stats.files} inserted={stats.facts_inserted}")
    return stats
