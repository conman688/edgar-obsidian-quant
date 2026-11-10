"""M11 Phase 1: why each quarantined name is quarantined, read from stored facts only.

For each issue of a quarantined row, one check with a reason:
- foreign_filer: the latest annual-form accession filed by as_of is a 20-F or 40-F.
- no_annual_in_stored_facts: the stored facts filed by as_of hold a 10-Q and no annual-form
  accession, so no TTM can be built from a stored annual base. It never says no annual report
  was filed, or that the company listed recently.
- tag_candidates: us-gaap facts outside concept_map, in the newest balance sheet's accession and
  date, whose tag fits the debt pattern. Shown for manual review, never used as debt.
- no_cover_count: the latest 10-K or 10-Q filed by as_of has no undimensioned DEI cover count.
  Per-class tagging is one possible cause, not a confirmed one.
- unknown: the facts hold no form or date evidence for the question. An accession in the
  ledger with no stored facts makes the latest-filing and missing-filing reasons unknown.
- none_found: the facts were enough, and nothing explains the issue; read the filing.

Nothing here fills a value or changes a status, rank or shortlist. Stored facts describe a
companyfacts snapshot, not a complete filing inventory, and the messages say only that.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import duckdb
import pyarrow as pa

from src.db.queries import MIN_PRICE

REASONS = ("foreign_filer", "no_annual_in_stored_facts", "tag_candidates", "no_cover_count", "unknown",
           "none_found")
ANNUAL_FORMS = ("10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A")
FOREIGN_FORMS = ("20-F", "20-F/A", "40-F", "40-F/A")
PERIODIC_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A")
DEBT_PATTERN = "(Debt|Notes|Borrowing|LineOfCredit|Loan)"
DEBT_EXCLUDE = "(Lease|Receivable|Asset)"
# Matched by the debt pattern but not owed (C9): debt securities the company holds as investments
# (available-for-sale, held-to-maturity), and credit-facility amounts still available to borrow.
# Maturity schedules are not excluded: they can describe real outstanding debt.
NOT_OWED = "(AvailableForSale|HeldToMaturity|DebtSecurities|BorrowingCapacity)"
DEBT_ISSUES = ("missing_debt", "stale_debt")
TTM_ISSUES = ("missing_ebit", "missing_fcf")
SHARE_ISSUES = ("missing_shares", "stale_shares")

CANDIDATE = pa.struct([("tag", pa.string()), ("value", pa.float64()), ("accn", pa.string()),
                       ("period_end", pa.date32()), ("kind", pa.string())])
CHECK = pa.struct([("issue", pa.string()), ("reason", pa.string()), ("message", pa.string()),
                   ("candidates", pa.list_(CANDIDATE))])


def _check(issue: str, reason: str, message: str, candidates: list[dict] | None = None) -> dict:
    return {"issue": issue, "reason": reason, "message": message, "candidates": candidates or []}


def _accessions(con, ciks: list[int], as_of: date) -> tuple[dict[int, list[dict]], dict[int, list[str]]]:
    """(filings, gaps). filings: accessions in the stored facts filed by as_of, newest first, with
    their form and date. gaps: ledger accessions with no stored facts at all, whose form and filing
    date are unknown. One grouped scan of xbrl_facts; a correlated probe per ledger row took ~4
    minutes on the 11-07 backup."""
    filings: dict[int, list[dict]] = defaultdict(list)
    have: set[tuple[int, str]] = set()
    for cik, accn, form, filed, has_dei in con.execute("""
            SELECT cik, accn,
                   -- Metadata and evidence come only from rows filed by as_of (C10); rows filed
                   -- later still show the accession exists, so it is not a ledger gap.
                   min(form) FILTER (WHERE filed <= $as_of), min(filed) FILTER (WHERE filed <= $as_of),
                   -- Only a count the screen could use: the same guards as pit.py.
                   coalesce(bool_or(taxonomy = 'dei' AND tag = 'EntityCommonStockSharesOutstanding'
                                    AND filed <= $as_of AND period_end <= filed + INTERVAL 7 DAY
                                    AND period_end <= $as_of), FALSE)
            FROM xbrl_facts WHERE list_contains($ciks, cik)
            GROUP BY cik, accn ORDER BY cik, 4 DESC NULLS LAST, accn DESC""",
            {"ciks": ciks, "as_of": as_of}).fetchall():
        have.add((cik, accn))
        if filed is not None:
            filings[cik].append({"accn": accn, "form": form, "filed": filed, "has_dei": has_dei})
    gaps: dict[int, list[str]] = defaultdict(list)
    for cik, accn in con.execute("SELECT cik, accn FROM loaded_filings WHERE list_contains($ciks, cik) "
                                 "ORDER BY cik, accn", {"ciks": ciks}).fetchall():
        if (cik, accn) not in have:
            gaps[cik].append(accn)
    return filings, gaps


def _debt_candidates(con, want: list[tuple[int, date, list[str]]], as_of: date) -> dict[int, list[dict]]:
    """us-gaap USD instants outside concept_map, at the balance-sheet date and in its accession."""
    out: dict[int, list[dict]] = defaultdict(list)
    if not want:
        return out
    for cik, bs_date, accns in want:
        for tag, value, accn, period_end in con.execute(f"""
                SELECT f.tag, f.value, f.accn, f.period_end FROM xbrl_facts f
                WHERE f.cik = $cik AND f.taxonomy = 'us-gaap' AND f.unit = 'USD' AND f.period_kind = 'instant'
                  AND f.filed <= $as_of AND f.period_end = $bs_date AND list_contains($accns, f.accn)
                  AND f.period_end <= f.filed + INTERVAL 7 DAY AND f.period_end <= $as_of   -- pit.py guards
                  AND regexp_matches(f.tag, '{DEBT_PATTERN}') AND NOT regexp_matches(f.tag, '{DEBT_EXCLUDE}')
                  AND NOT regexp_matches(f.tag, '{NOT_OWED}')
                  AND NOT EXISTS (SELECT 1 FROM concept_map m WHERE m.taxonomy = f.taxonomy AND m.tag = f.tag)
                ORDER BY f.tag, f.accn""",
                {"cik": cik, "as_of": as_of, "bs_date": bs_date, "accns": accns}).fetchall():
            # A tag outside the map is a component until a reviewed batch (M11 2a) proves it a
            # complete total and adds it to CONCEPT_SEED, after which it is no longer a candidate.
            out[cik].append({"tag": tag, "value": value, "accn": accn, "period_end": period_end,
                             "kind": "component"})
    return out


def _bs_accns(row: dict) -> list[str]:
    """Accessions of the inputs the screen read at the balance-sheet date."""
    return sorted({a for s in row["sources"] or [] if s["period_end"] == row["bs_date"] for a in s["accns"]})


def _describe(f: dict) -> str:
    return f"{f['form']} {f['accn']} (filed {f['filed']})"


def checks_for(row: dict, filings: list[dict], gaps: list[str], candidates: list[dict], as_of: date) -> list[dict]:
    """One check per issue of a quarantined row, in issue order."""
    annual = next((f for f in filings if f["form"] in ANNUAL_FORMS), None)
    periodic = next((f for f in filings if f["form"] in PERIODIC_FORMS), None)
    gap_msg = (f"ledger accession(s) {', '.join(gaps)} have no stored facts, so their form and filing date "
               f"are unknown")
    out = []
    for issue in row["issues"]:
        if issue == "foreign_filer":
            if gaps:
                out.append(_check(issue, "unknown", gap_msg))
            elif annual and annual["form"] in FOREIGN_FORMS:
                out.append(_check(issue, "foreign_filer",
                                  f"latest annual-form accession filed by {as_of}: {_describe(annual)}"))
            else:
                out.append(_check(issue, "none_found",
                                  "a cover or cash fact is from a 20-F or 40-F, but the latest annual-form "
                                  "accession in the stored facts is not; read the filings"))
        elif issue in TTM_ISSUES:
            ten_q = next((f for f in filings if f["form"] in ("10-Q", "10-Q/A")), None)
            if gaps:
                out.append(_check(issue, "unknown", gap_msg))
            elif ten_q and not annual:
                out.append(_check(issue, "no_annual_in_stored_facts",
                                  f"the stored facts filed by {as_of} include {_describe(ten_q)} and no "
                                  f"annual-form accession, so no TTM can be built from a stored annual base"))
            elif not filings:
                out.append(_check(issue, "unknown", f"the stored facts hold no filing dated by {as_of}"))
            else:
                out.append(_check(issue, "none_found",
                                  "an annual-form accession is in the stored facts; the TTM inputs are "
                                  "missing from it or untagged in the concept map; read the filing"))
        elif issue in DEBT_ISSUES:
            if candidates:
                accns = sorted({c["accn"] for c in candidates})
                out.append(_check(issue, "tag_candidates",
                                  f"{len(candidates)} us-gaap fact(s) outside the concept map in {', '.join(accns)} "
                                  f"at {row['bs_date']} fit the debt pattern; shown for review, not used as debt",
                                  candidates))
            else:
                out.append(_check(issue, "none_found",
                                  f"no stored us-gaap fact outside the concept map fits the debt pattern at "
                                  f"{row['bs_date']}; read the balance sheet"))
        elif issue in SHARE_ISSUES:
            if gaps:
                out.append(_check(issue, "unknown", gap_msg))
            elif periodic and not periodic["has_dei"]:
                out.append(_check(issue, "no_cover_count",
                                  f"the latest 10-K or 10-Q filed by {as_of}, {_describe(periodic)}, has no "
                                  f"undimensioned DEI cover count; per-class cover tagging is one possible cause"))
            elif periodic:
                out.append(_check(issue, "none_found",
                                  f"{_describe(periodic)} has a DEI cover count; read the cover page"))
            else:
                out.append(_check(issue, "unknown", f"the stored facts hold no 10-K or 10-Q filed by {as_of}"))
        elif issue == "penny_price":
            out.append(_check(issue, "none_found",
                              f"close {row['close_raw']:.2f} on {row['price_date']} is below {MIN_PRICE:.2f}"))
        elif issue == "stale_price":
            out.append(_check(issue, "none_found", f"the last stored close is from {row['price_date']}"))
        elif issue == "share_jump" and row.get("share_cagr_1y") is not None:
            out.append(_check(issue, "none_found",
                              f"diluted shares changed {row['share_cagr_1y']:+.0%} in a year; a listing, "
                              f"reorganisation or missed split; read the filings"))
        else:
            out.append(_check(issue, "none_found", f"the stored facts do not explain {issue}; read the filing"))
    return out


def with_issue_checks(con: duckdb.DuckDBPyConnection, table: pa.Table, as_of: date) -> pa.Table:
    """Add `issue_checks` to a screen table: a reason for every issue of every quarantined row.

    A quarantined row whose latest annual-form accession is a 20-F or 40-F also gains the
    `foreign_filer` issue (FMX, GRAB: no shares or cash facts, so the screen cannot see the form).
    Statuses are never changed: only rows already quarantined are touched.
    """
    rows = table.to_pylist()
    q = [r for r in rows if r["status"] == "quarantine"]
    ciks = sorted({r["cik"] for r in q})
    filings, gaps = _accessions(con, ciks, as_of) if ciks else ({}, {})
    want = [(r["cik"], r["bs_date"], _bs_accns(r)) for r in q
            if set(r["issues"]) & set(DEBT_ISSUES) and r["bs_date"] is not None]
    cands = _debt_candidates(con, want, as_of)
    issues, checks = [], []
    for r in rows:
        if r["status"] != "quarantine":
            issues.append(r["issues"])
            checks.append([])
            continue
        mine = filings.get(r["cik"], [])
        annual = next((f for f in mine if f["form"] in ANNUAL_FORMS), None)
        row_issues = list(r["issues"])
        if ("foreign_filer" not in row_issues and not gaps.get(r["cik"])
                and annual and annual["form"] in FOREIGN_FORMS):
            row_issues.append("foreign_filer")
        issues.append(row_issues)
        checks.append(checks_for({**r, "issues": row_issues}, mine, gaps.get(r["cik"], []),
                                 cands.get(r["cik"], []), as_of))
    i = table.schema.get_field_index("issues")
    table = table.set_column(i, "issues", pa.array(issues, table.schema.field("issues").type))
    return table.append_column("issue_checks", pa.array(checks, pa.list_(CHECK)))
