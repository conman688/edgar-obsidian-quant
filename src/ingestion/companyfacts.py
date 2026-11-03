"""Load SEC companyfacts JSON (bulk companyfacts.zip or /api/xbrl/companyfacts) into xbrl_facts.

Bulk zip is one ~1.3 GB download instead of thousands of API calls. Each fact's
`filed` and `accn` are kept so restatements become new rows, not overwrites.
"""
from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

import duckdb

KEEP_TAXONOMIES = {"us-gaap", "dei", "ifrs-full"}
KEEP_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "10-KT"}


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


def load_docs(con: duckdb.DuckDBPyConnection, docs: Iterable[dict], batch: int = 200_000) -> int:
    """Insert facts idempotently; re-running the same file inserts zero new rows."""
    sql = "INSERT INTO xbrl_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING"
    buf: list[tuple] = []
    n = 0
    for doc in docs:
        for row in iter_fact_rows(doc):
            buf.append(row)
            if len(buf) >= batch:
                con.executemany(sql, buf)
                n += len(buf)
                buf.clear()
    if buf:
        con.executemany(sql, buf)
        n += len(buf)
    return n


def iter_bulk_zip(path: Path, ciks: set[int] | None = None) -> Iterator[dict]:
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".json"):
                continue
            if ciks is not None and int(name.removeprefix("CIK").removesuffix(".json")) not in ciks:
                continue
            doc = json.loads(z.read(name))
            if "facts" in doc:
                yield doc
