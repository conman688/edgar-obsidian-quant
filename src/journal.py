"""Decision journal: what the screen showed when a decision was made, and what happened since.

`snapshot` freezes one company's screen row on the decision date. The journal note keeps it in
its frontmatter; nothing rewrites it later (the `journal` command refuses an existing note and
`sync-vault` never writes in Journal/). `read_journals` and `since` read the snapshots back so
the dashboard can set them against the screen on the sync date.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import duckdb

from src.db.queries import PRICE_BREAK
from src.obsidian.writer import split_note
from src.screen import Filters

REVIEW_DAYS = 90
RATIOS = ("ev_ebit", "net_cash_to_mcap", "fcf_yield", "drawdown_52w", "share_cagr_3y", "insider_buy_to_mcap")
DOLLARS = ("mcap", "ev", "ebit_ttm", "fcf_ttm", "cash_like", "debt", "insider_buy_90d_usd")


def _round(v, digits: int):
    if v is None:
        return None
    return int(round(float(v))) if digits == 0 else round(float(v), digits)


def snapshot(row: dict, filters: Filters, policy: str) -> dict:
    """The screen row as it stood: ratios to 4 decimals, dollars whole, the raw close as printed.
    The close stays on that day's share basis (prices are stored raw; 4 decimals drop float32 noise);
    `since` adjusts it."""
    return {"as_of": row["as_of"], "status": row["status"], "rank": row["rank"],
            **{k: _round(row[k], 4) for k in RATIOS}, **{k: _round(row[k], 0) for k in DOLLARS},
            "n_insiders": row["n_insiders"], "close_raw": _round(row["close_raw"], 4), "price_date": row["price_date"],
            "issues": list(row["issues"] or []), "flags": list(row["flags"] or []),
            "failed_filters": list(row["failed_filters"] or []),
            "filters": asdict(filters), "policy": policy}


def to_date(v) -> date | None:
    """A frontmatter date. YAML reads `2024-09-05T12:00:00Z` as a datetime (a date subclass that
    cannot be compared with a date): keep its calendar day as written."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def read_journals(folder: Path, as_of: date) -> list[dict]:
    """Journal notes decided on or before as_of, oldest first. A note whose frontmatter does not
    parse, or lacks a CIK or decision date, is left out (and left alone)."""
    out = []
    for p in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            meta = split_note(p.read_text(encoding="utf-8"))[0]
        except Exception:
            continue
        decided = to_date(meta.get("decided"))
        if meta.get("type") != "journal" or not isinstance(meta.get("cik"), int) or decided is None:
            continue
        if decided <= as_of:
            snap = meta.get("snapshot")
            out.append({"path": p, "meta": meta, "decided": decided, "cik": meta["cik"],
                        "snapshot": dict(snap) if isinstance(snap, dict) else {}})
    return sorted(out, key=lambda j: (j["decided"], j["path"].name))


def since(con: duckdb.DuckDBPyConnection, journals: list[dict], as_of: date) -> list[dict]:
    """Per journal (same order): the latest close on or before as_of, and the price change and
    total return from the snapshot's close, both on today's share basis (close / split_factor).
    The total adds dividends with ex-date after the decision date, up to the latest close: a
    decision made on an ex-date or later does not get that dividend. When the snapshot's close is
    older than the decision and a dividend went ex in between, that close still included the
    dividend, so the total return is unknown (None). Both are None when either close is missing or
    the split-adjusted close jumps by PRICE_BREAK (10x) or more in a day inside the window. The
    window's first close is compared with the frozen snapshot close, not the database's close for
    that day: the return divides by the frozen close, and the database may have revised it since.
    With no close after the snapshot day, the latest close itself is checked against the frozen one.
    If the database's close for the snapshot day now differs from the frozen close by PRICE_BREAK
    or more in either direction, one of them is wrong and both returns are None."""
    if not journals:
        return []
    snaps = [j["snapshot"] for j in journals]
    rows = con.execute(f"""
        WITH j AS (SELECT unnest($i) AS i, unnest($cik) AS cik, unnest($dec)::DATE AS dec,
                          unnest($d0)::DATE AS d0, unnest($c0) AS c0),
        n AS (SELECT cik, price_date AS d1, close_raw AS c1 FROM prices
              WHERE price_date <= $as_of AND list_contains($cik, cik)
              QUALIFY row_number() OVER (PARTITION BY cik ORDER BY price_date DESC) = 1),
        adj AS (SELECT cik, price_date AS d, close_raw / split_factor(cik, price_date) AS a,
                       lag(close_raw / split_factor(cik, price_date)) OVER (PARTITION BY cik ORDER BY price_date) AS prev,
                       lag(price_date) OVER (PARTITION BY cik ORDER BY price_date) AS prev_d
                FROM prices WHERE price_date <= $as_of AND list_contains($cik, cik)),
        r AS (SELECT j.i, n.d1, n.c1, j.c0 / split_factor(j.cik, j.d0) AS a0, n.c1 / split_factor(j.cik, n.d1) AS a1,
                     (SELECT sum(d.amount_raw / split_factor(d.cik, d.ex_date)) FROM dividends d
                      WHERE d.cik = j.cik AND d.ex_date > j.dec AND d.ex_date <= n.d1) AS divs,
                     EXISTS (SELECT 1 FROM dividends d
                             WHERE d.cik = j.cik AND d.ex_date > j.d0 AND d.ex_date <= j.dec) AS gap,
                     EXISTS (SELECT 1 FROM adj
                             WHERE adj.cik = j.cik AND adj.d > j.d0 AND adj.d <= n.d1
                               AND adj.a >= {PRICE_BREAK} * CASE WHEN adj.prev_d > j.d0 THEN adj.prev
                                                                 ELSE j.c0 / split_factor(j.cik, j.d0) END)
                     OR (n.d1 <= j.d0 AND n.c1 / split_factor(j.cik, n.d1) >= {PRICE_BREAK} * (j.c0 / split_factor(j.cik, j.d0)))
                     OR EXISTS (SELECT 1 FROM prices p WHERE p.cik = j.cik AND p.price_date = j.d0
                                  AND (p.close_raw >= {PRICE_BREAK} * j.c0 OR p.close_raw * {PRICE_BREAK} <= j.c0))
                     AS broke
              FROM j LEFT JOIN n USING (cik))
        SELECT d1, c1,
               CASE WHEN NOT broke THEN a1 / nullif(a0, 0) - 1 END,
               CASE WHEN NOT broke AND NOT gap THEN (a1 + coalesce(divs, 0)) / nullif(a0, 0) - 1 END
        FROM r ORDER BY i
    """, {"i": list(range(len(journals))), "cik": [j["cik"] for j in journals],
          "dec": [j["decided"] for j in journals],
          "d0": [to_date(s.get("price_date")) for s in snaps],
          "c0": [s.get("close_raw") if isinstance(s.get("close_raw"), (int, float)) else None for s in snaps],
          "as_of": as_of}).fetchall()
    return [{"price_date": d1, "close_raw": c1, "price_change": pc, "total_return": tr}
            for d1, c1, pc, tr in rows]
