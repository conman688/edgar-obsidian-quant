"""M12-A2: Forms 8-K and 8-K/A from submissions.zip, the snapshot `ingest companies` downloads.

Each company file lists its newest filings under `filings.recent` and older ones in paged
files (`filings.files`, CIK##########-submissions-001.json ...) inside the same zip; both are
read. Only the companies `companies` profiles are kept (an SIC code or a ticker), so the
~900k individual filers are skipped. Rows are keyed by (accession, CIK): a replay of the
same snapshot is skipped by the ledger, and a newer snapshot inserts only filings not yet
stored. Filings are never revised in place (an amendment is a new 8-K/A), so stored rows
are not updated.
"""
from __future__ import annotations

import json
import zipfile
from datetime import date, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from src.db import ledger

SCHEMA_VERSION = 1
STAGE = "filings_8k"
FORMS = ("8-K", "8-K/A")
BATCH = 200_000

SCHEMA = pa.schema([
    ("accn", pa.string()), ("cik", pa.int32()), ("form", pa.string()), ("filed", pa.date32()),
    ("accepted", pa.timestamp("us")), ("report_date", pa.date32()), ("items", pa.string()),
    ("primary_doc", pa.string()),
])


def key(sha256: str) -> str:
    return f"submissions@{sha256[:16]}"


def is_loaded(con: duckdb.DuckDBPyConnection, sha256: str | None) -> bool:
    return bool(sha256) and ledger.is_done(con, key(sha256), STAGE, SCHEMA_VERSION)


def _date(v) -> date | None:
    return date.fromisoformat(v[:10]) if v else None


def _ts(v) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None) if v else None


def rows_from(cik: int, cols: dict) -> list[tuple]:
    """8-K rows from SEC's column arrays (all arrays are the same length)."""
    out = []
    forms = cols.get("form") or []
    for i, form in enumerate(forms):
        if form not in FORMS:
            continue
        get = lambda k: (cols.get(k) or [None] * len(forms))[i]   # noqa: E731
        out.append((get("accessionNumber"), cik, form, _date(get("filingDate")), _ts(get("acceptanceDateTime")),
                    _date(get("reportDate")), (get("items") or "").strip() or None, get("primaryDocument") or None))
    return out


class IncompleteSnapshot(RuntimeError):
    """A company file declares an archive page the zip does not hold. SEC's snapshots hold
    every declared page (2026-11-25: 5,435 of 5,435), so this is a broken download."""


def _profiled(doc: dict) -> bool:
    """The rule `reference.load_companies_zip` keeps a company profile by."""
    return str(doc.get("sic") or "").strip().isdigit() or any(doc.get("tickers") or [])


def load_8k_zip(con: duckdb.DuckDBPyConnection, path: Path, sha256: str) -> int:
    """Insert the snapshot's 8-Ks not stored yet. Returns rows inserted (0 when done before)."""
    if is_loaded(con, sha256):
        return 0
    inserted = 0
    batch: list[tuple] = []

    def flush() -> None:
        nonlocal inserted
        if not batch:
            return
        table = pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(zip(*batch), SCHEMA)], schema=SCHEMA)
        con.register("rows_8k", table)
        try:
            before = con.execute("SELECT count(*) FROM filings_8k").fetchall()[0][0]
            con.execute("INSERT OR IGNORE INTO filings_8k SELECT * FROM rows_8k")
            inserted += con.execute("SELECT count(*) FROM filings_8k").fetchall()[0][0] - before
        finally:
            con.unregister("rows_8k")
        batch.clear()

    con.begin()
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for name in sorted(names):
                if not (name.startswith("CIK") and name.endswith(".json") and "-submissions-" not in name):
                    continue
                doc = json.loads(z.read(name))
                if not _profiled(doc):
                    continue
                cik = int(doc["cik"])
                filings = doc.get("filings") or {}
                batch.extend(rows_from(cik, filings.get("recent") or {}))
                for f in filings.get("files") or []:
                    if f.get("name") not in names:
                        raise IncompleteSnapshot(f"{name} declares {f.get('name')}, which is not in {path.name}")
                    batch.extend(rows_from(cik, json.loads(z.read(f["name"]))))
                if len(batch) >= BATCH:
                    flush()
        flush()
        ledger.mark(con, key(sha256), STAGE, SCHEMA_VERSION, "done", sha256, output_ref=f"filings_8k+={inserted}")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return inserted
