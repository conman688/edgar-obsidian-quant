"""Reference data: ticker map and company profile (SIC, fiscal year end, exchanges)."""
from __future__ import annotations

import json
import re
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa

from src.db import ledger

SCHEMA_VERSION = 1
_MAIN_FILE = re.compile(r"CIK(\d{10})\.json$")   # skips paged *-submissions-001.json files


def load_tickers(con: duckdb.DuckDBPyConnection, payload: bytes, fetched_on: date) -> int:
    """Replace the ticker snapshot from company_tickers_exchange.json."""
    doc = json.loads(payload)
    idx = {f: i for i, f in enumerate(doc["fields"])}
    rows, seen = [], set()
    for r in doc["data"]:
        ticker = (r[idx["ticker"]] or "").upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        rows.append((int(r[idx["cik"]]), ticker, r[idx["name"]], r[idx["exchange"]], fetched_on))
    if not rows:
        raise ValueError("ticker file has no rows; refusing to wipe the snapshot")
    con.begin()
    try:
        con.execute("DELETE FROM tickers")
        con.executemany("INSERT INTO tickers VALUES (?, ?, ?, ?, ?)", rows)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return len(rows)


def _company_row(doc: dict) -> tuple:
    sic_raw = str(doc.get("sic") or "").strip()
    sic = int(sic_raw) if sic_raw.isdigit() else None
    return (
        int(doc["cik"]), doc.get("name"), sic, doc.get("sicDescription") or None,
        doc.get("fiscalYearEnd") or None, doc.get("entityType") or None, doc.get("category") or None,
        [e for e in doc.get("exchanges") or [] if e], [t for t in doc.get("tickers") or [] if t],
        sic is not None and 6000 <= sic <= 6799, sic == 6770,
    )


COMPANY_SCHEMA = pa.schema([
    ("cik", pa.int32()), ("name", pa.string()), ("sic", pa.int32()), ("sic_description", pa.string()),
    ("fiscal_year_end", pa.string()), ("entity_type", pa.string()), ("filer_category", pa.string()),
    ("exchanges", pa.list_(pa.string())), ("tickers", pa.list_(pa.string())),
    ("is_financial", pa.bool_()), ("is_shell", pa.bool_()),
])


def _key(sha256: str) -> str:
    return f"submissions@{sha256[:16]}"


def is_loaded(con: duckdb.DuckDBPyConnection, sha256: str | None) -> bool:
    return bool(sha256) and ledger.is_done(con, _key(sha256), "companies", SCHEMA_VERSION)


def load_companies_zip(con: duckdb.DuckDBPyConnection, path: Path, sha256: str) -> int:
    """Upsert company profiles from submissions.zip in one bulk statement.

    Most of the ~900k files are individual insiders with no SIC code and no ticker;
    they are skipped because the screen only needs operating companies.
    """
    key = _key(sha256)
    if ledger.is_done(con, key, "companies", SCHEMA_VERSION):
        return 0
    rows = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not _MAIN_FILE.search(name):
                continue
            row = _company_row(json.loads(z.read(name)))
            if row[2] is not None or row[8]:
                rows.append(row)
    table = pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(zip(*rows), COMPANY_SCHEMA)],
                                 schema=COMPANY_SCHEMA)
    con.begin()
    try:
        con.register("company_rows", table)
        con.execute("INSERT OR REPLACE INTO companies SELECT * FROM company_rows")
        ledger.mark(con, key, "companies", SCHEMA_VERSION, "done", sha256, output_ref=f"companies={len(rows)}")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.unregister("company_rows")
    return len(rows)
