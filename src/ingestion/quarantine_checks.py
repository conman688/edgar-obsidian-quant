"""M11 Phase 2 requests and ledger: fetch the documents that can resolve a quarantine issue.

Eligibility (C6): only rows with screen status `quarantine`. `m11_cover` runs for a share issue
whose Phase 1 reason is `no_cover_count`; `m11_balance_sheet` for `missing_debt` or `stale_debt`.
The filing is the latest 10-K or 10-Q in the stored facts filed by as_of.

Requests (C5), all through the caller's SEC client: index.json and the one `*_htm.xml` instance
(both families), FilingSummary.xml and the balance-sheet R page (`m11_balance_sheet`). At most 4
per filing per run. `cache` holds documents already fetched in this invocation, keyed by URL, so
one family or another job (M10) never fetches a document twice. Nothing is kept on disk.

Ledger: one `quarantine_checks` row per (cik, accn, family). A family is complete for a filing
when its row is `ok` or `not_found` at the current parser version; an `error` row is retried.
Each family is written in its own transaction, so one failing never loses the other.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime

import duckdb
import requests

from src.ingestion.quarantine_docs import (DocError, balance_sheet_report, cover_classes, instance_name,
                                           parse_instance, r_page, zero_debt)
from src.quarantine import DEBT_ISSUES, PERIODIC_FORMS, _accessions

PARSERS = {"m11_cover": 1, "m11_balance_sheet": 1}
FETCH_ERRORS = (DocError, requests.RequestException, RuntimeError)   # RuntimeError: SEC throttled to the end


def _base(cik: int, accn: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/"


def families(row: dict) -> list[str]:
    if row["status"] != "quarantine":
        return []
    out = []
    if any(c["reason"] == "no_cover_count" for c in row["issue_checks"]):
        out.append("m11_cover")
    if set(row["issues"]) & set(DEBT_ISSUES):
        out.append("m11_balance_sheet")
    return out


def _complete(con, cik: int, accn: str, family: str) -> bool:
    return bool(con.execute("""SELECT 1 FROM quarantine_checks WHERE cik = ? AND accn = ? AND family = ?
                               AND status IN ('ok', 'not_found') AND parser >= ?""",
                            [cik, accn, family, PARSERS[family]]).fetchall())


def _write(con, cik: int, filing: dict, family: str, status: str, result=None, period_end=None,
           message=None, detail=None, classes=None) -> None:
    con.begin()
    try:
        con.execute("""INSERT OR REPLACE INTO quarantine_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [cik, filing["accn"], family, PARSERS[family], status, filing["filed"], result, period_end,
                     message, json.dumps(detail, default=str) if detail is not None else None, datetime.now()])
        if family == "m11_cover":
            con.execute("DELETE FROM cover_shares WHERE cik = ? AND accn = ?", [cik, filing["accn"]])
            if classes:
                con.executemany("INSERT INTO cover_shares VALUES (?, ?, ?, ?, ?, ?, ?)",
                                [(cik, filing["accn"], filing["filed"], c["member"], c["shares"], c["symbol"],
                                  c["dei_date"]) for c in classes])
        con.commit()
    except Exception:
        con.rollback()
        raise


def run_checks(con: duckdb.DuckDBPyConnection, sec, rows: list[dict], as_of: date,
               cache: dict[str, bytes] | None = None) -> Counter:
    """Run the eligible, incomplete families for the screen `rows`. Returns ledger statuses written."""
    cache = {} if cache is None else cache
    failed: dict[str, Exception] = {}          # a document that failed is not requested again this run

    def get(url: str) -> bytes:
        if url in failed:
            raise failed[url]
        if url not in cache:
            try:
                cache[url] = sec.get(url)
            except FETCH_ERRORS as e:
                failed[url] = e
                raise
        return cache[url]

    want = [(r, families(r)) for r in rows]
    want = [(r, f) for r, f in want if f]
    filings, _ = _accessions(con, sorted({r["cik"] for r, _ in want}), as_of) if want else ({}, {})
    got: Counter = Counter()
    for r, fams in sorted(want, key=lambda x: x[0]["cik"]):
        cik = r["cik"]
        filing = next((f for f in filings.get(cik, []) if f["form"] in PERIODIC_FORMS), None)
        if filing is None:
            continue
        base = _base(cik, filing["accn"])
        for fam in fams:
            if _complete(con, cik, filing["accn"], fam):
                continue
            try:
                inst = parse_instance(get(base + instance_name(get(base + "index.json"))))
                if fam == "m11_cover":
                    cov = cover_classes(inst)
                    if not cov["classes"]:
                        _write(con, cik, filing, fam, "not_found", message="no share-class cover counts in the instance",
                               detail={"symbols": cov["symbols"]})
                        got["not_found"] += 1
                        continue
                    _write(con, cik, filing, fam, "ok", "conflict" if cov["conflict"] else "classes",
                           max(c["dei_date"] for c in cov["classes"] if c["dei_date"]) if any(
                               c["dei_date"] for c in cov["classes"]) else None,
                           f"{len(cov['classes'])} classes, symbols {', '.join(cov['symbols']) or 'none'}",
                           {"symbols": cov["symbols"]}, cov["classes"])
                else:
                    page = get(base + balance_sheet_report(get(base + "FilingSummary.xml")))
                    dates, elems = r_page(page)
                    z = zero_debt(dates, elems, inst, r["bs_date"])
                    _write(con, cik, filing, fam, "ok", "debt_zero" if z["debt_zero"] else "not_zero",
                           z["column"], z["reason"], {"candidates": z["candidates"], "method": z["method"]})
                got["ok"] += 1
            except FETCH_ERRORS as e:
                _write(con, cik, filing, fam, "error", message=f"{type(e).__name__}: {e}"[:500])
                got["error"] += 1
    return got
