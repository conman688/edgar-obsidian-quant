"""Capital-allocation and balance-sheet forensics from facts already loaded (M1).

Buybacks, acquisitions and impairments are read for the latest fiscal year only: these
tags appear only in years when something happened, so a TTM bridge or "latest value"
would show a five-year-old impairment as current. The latest fiscal year is the one of the
company's latest annual operating cash flow. Debt maturities come from that year's
balance sheet (the 10-K schedule), bucket N = N years after fiscal year end.

Flags are informative ("check by hand"); they do not change the screen.
"""
from __future__ import annotations

from datetime import date

import duckdb

from src.db.pit import Policy, concepts_sql

FLOWS = {"buybacks": "buybacks_paid", "acquisitions": "acquisitions_paid",
         "goodwill_impairment": "goodwill_impairment", "asset_impairment": "asset_impairment"}
MATURITIES = ("debt_due_y1", "debt_due_y2", "debt_due_y3", "debt_due_y4", "debt_due_y5", "debt_due_after_y5")
STALE_FY_DAYS = 470          # a fiscal year ends, its 10-K follows within ~3 months, the next a year later
ACQUISITIVE = 0.10           # acquisitions paid in the year >= 10% of market cap
IMPAIRMENT = 0.05            # one impairment line >= 5% of market cap
WALL_YEARS = 2               # debt due within this many years above cash on hand


def forensics_sql(policy: Policy = "as_last_known") -> str:
    """Binds $as_of and $ciks. One row per company and forensic value of its latest fiscal year."""
    return f"""
    WITH pit AS MATERIALIZED (
        SELECT * FROM ({concepts_sql(policy, ('cfo', *FLOWS.values(), *MATURITIES))})
        WHERE list_contains($ciks, cik)
    ),
    ref AS (
        SELECT cik, max(period_end) AS fy_end FROM pit
        WHERE concept = 'cfo' AND period_kind = 'annual' GROUP BY cik
    )
    SELECT ref.cik, ref.fy_end, p.concept, p.value, p.accn
    FROM ref LEFT JOIN pit p
      ON p.cik = ref.cik AND p.period_end = ref.fy_end AND p.concept <> 'cfo'
     AND p.period_kind = CASE WHEN p.concept LIKE 'debt_due%' THEN 'instant' ELSE 'annual' END
    QUALIFY row_number() OVER (PARTITION BY ref.cik, p.concept ORDER BY p.filed DESC, p.accn DESC) = 1
    ORDER BY ref.cik, p.concept
    """


def forensics(con: duckdb.DuckDBPyConnection, ciks: list[int], as_of: date,
              policy: Policy = "as_last_known") -> dict[int, dict]:
    concept_key = {v: k for k, v in FLOWS.items()}
    out: dict[int, dict] = {}
    for cik, fy_end, concept, value, accn in con.execute(
            forensics_sql(policy), {"as_of": as_of, "ciks": sorted(ciks)}).fetchall():
        f = out.setdefault(cik, {"fy_end": fy_end, "stale": (as_of - fy_end).days > STALE_FY_DAYS,
                                 **{k: None for k in FLOWS}, "debt_due": [None] * len(MATURITIES), "accns": []})
        if concept is None:
            continue
        if concept in concept_key:
            f[concept_key[concept]] = value
        else:
            f["debt_due"][MATURITIES.index(concept)] = value
        if accn not in f["accns"]:
            f["accns"].append(accn)
    return out


def assess(f: dict, row: dict) -> dict:
    """Ratios to market cap and flags, given the company's screen row (mcap, cash_like).

    `unknown` names the checks that cannot be made: `debt_wall` when cash is unknown, or a
    maturity bucket within WALL_YEARS is missing and the known debt alone does not exceed cash.
    A missing input never counts as zero."""
    mcap, cash = row.get("mcap"), row.get("cash_like")
    if f["stale"] or not mcap:
        return {"buyback_yield": None, "acquisitions_to_mcap": None, "due_soon": None, "flags": [], "unknown": []}
    buckets = f["debt_due"][:WALL_YEARS]
    known = sum(v for v in buckets if v is not None)
    complete = None not in buckets
    flags, unknown = [], []
    if cash is not None and known > cash:
        flags.append("debt_wall")                    # true whatever the missing buckets hold
    elif cash is None or not complete:
        unknown.append("debt_wall")
    impairment = max(f["goodwill_impairment"] or 0.0, f["asset_impairment"] or 0.0)
    if (f["acquisitions"] or 0.0) >= ACQUISITIVE * mcap:
        flags.append("acquisitive")
    if impairment >= IMPAIRMENT * mcap:
        flags.append("impairment")
    return {"buyback_yield": f["buybacks"] / mcap if f["buybacks"] is not None else None,
            "acquisitions_to_mcap": f["acquisitions"] / mcap if f["acquisitions"] is not None else None,
            "due_soon": known if complete else None, "flags": flags, "unknown": unknown}
