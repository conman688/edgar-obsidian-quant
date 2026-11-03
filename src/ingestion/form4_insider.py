"""Parse SEC Form 4 ownershipDocument XML into form4_transactions rows.

Parses the raw XML (stable SEC schema) instead of a wrapper's object model, so
field semantics are explicit:
- transactionCode 'A' (grant) and transactionAcquiredDisposedCode 'A' (acquired) are
  different fields. A conviction buy is code 'P' AND acquired/disposed 'A'.
- Each row keeps direct/indirect ownership and the nature text (trust, LLC, spouse)
  so holdings are summed once per account, never across duplicate rows.
- The 10b5-1 flag is True/False only when the filing states it. Pre-2023 filings
  without the checkbox and without a footnote stay NULL (unknown).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

import duckdb

_10B5_FOOTNOTE = re.compile(r"10b5-?1", re.IGNORECASE)


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    raw = (v.text if v is not None else el.text) or ""
    raw = raw.strip()
    return raw or None


def _float(node: ET.Element | None, path: str) -> float | None:
    s = _text(node, path)
    try:
        return float(s.replace(",", "")) if s else None
    except ValueError:
        return None


def _bool(s: str | None) -> bool | None:
    if s is None:
        return None
    return s.strip().lower() in ("1", "true", "y", "yes")


def _footnote_ids(node: ET.Element) -> list[str]:
    return [f.get("id", "") for f in node.iter("footnoteId")]


def parse_form4(xml: bytes, accn: str, filed: date) -> list[dict]:
    root = ET.fromstring(xml)
    issuer_cik = int(_text(root, "issuer/issuerCik") or 0)
    footnotes = {f.get("id"): (f.text or "").strip() for f in root.iter("footnote")}

    # 10b5-1 checkbox: EDGAR schema updated 2023-03-20, required for filings from
    # 2023-04-01. Matched on any element whose tag mentions 10b5 rather than a single
    # hard-coded name. Grandfathered (pre-Dec-2022) plans leave the box unchecked, so
    # False only means "no amended-rule plan"; footnotes below can still flip it True.
    doc_flag = None
    for el in root.iter():
        if "10b5" in el.tag.lower():
            doc_flag = _bool((el.text or "").strip() or _text(el, "value"))
            break

    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        owners.append({
            "owner_cik": int(_text(ro, "reportingOwnerId/rptOwnerCik") or 0),
            "owner_name": _text(ro, "reportingOwnerId/rptOwnerName") or "",
            "is_director": _bool(_text(rel, "isDirector")),
            "is_officer": _bool(_text(rel, "isOfficer")),
            "is_ten_pct": _bool(_text(rel, "isTenPercentOwner")),
            "officer_title": _text(rel, "officerTitle"),
        })
    if not owners or issuer_cik == 0:
        raise ValueError(f"{accn}: missing issuer or reporting owner")
    # Joint filings list several owners for the same shares. Attribute rows to the
    # first owner only; attributing to all would multiply the dollar flow.
    owner = owners[0]

    rows: list[dict] = []
    tables = [
        ("nonDerivativeTable/nonDerivativeTransaction", False, False),
        ("nonDerivativeTable/nonDerivativeHolding", False, True),
        ("derivativeTable/derivativeTransaction", True, False),
        ("derivativeTable/derivativeHolding", True, True),
    ]
    for path, is_deriv, is_holding in tables:
        for i, t in enumerate(root.findall(path)):
            notes = [footnotes.get(fid, "") for fid in _footnote_ids(t)]
            note_text = " | ".join(n for n in notes if n)
            flag = doc_flag
            if _10B5_FOOTNOTE.search(note_text):
                flag = True
            txn_date = _text(t, "transactionDate")
            rows.append({
                "accn": accn, "line_no": i, "is_derivative": is_deriv, "is_holding": is_holding,
                "issuer_cik": issuer_cik, **owner, "filed": filed,
                "txn_date": date.fromisoformat(txn_date[:10]) if txn_date else None,
                "security_title": _text(t, "securityTitle"),
                "txn_code": _text(t, "transactionCoding/transactionCode"),
                "acq_disp": _text(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                "shares": _float(t, "transactionAmounts/transactionShares"),
                "price": _float(t, "transactionAmounts/transactionPricePerShare"),
                "shares_owned_following": _float(t, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                "ownership_form": _text(t, "ownershipNature/directOrIndirectOwnership"),
                "nature_of_ownership": (_text(t, "ownershipNature/natureOfOwnership") or "").strip().lower(),
                "is_10b5_1": flag,
                "footnotes": note_text or None,
            })
    return rows


COLUMNS = [
    "accn", "line_no", "is_derivative", "is_holding", "issuer_cik", "owner_cik", "owner_name",
    "is_director", "is_officer", "is_ten_pct", "officer_title", "filed", "txn_date",
    "security_title", "txn_code", "acq_disp", "shares", "price", "shares_owned_following",
    "ownership_form", "nature_of_ownership", "is_10b5_1", "footnotes",
]


def store(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    """Idempotent on (accn, table, line). Amendments (4/A) carry a new accn and are kept."""
    if not rows:
        return
    ph = ",".join("?" * len(COLUMNS))
    con.executemany(
        f"INSERT INTO form4_transactions ({','.join(COLUMNS)}) VALUES ({ph}) ON CONFLICT DO NOTHING",
        [[r[c] for c in COLUMNS] for r in rows],
    )
