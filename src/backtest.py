"""Backtest harness: does the screen pick stocks that beat the market they were picked from?

Monthly rebalance. On each month end the screen's inputs are snapshotted point-in-time
(as_first_reported: the numbers as first printed, filed <= that date) and cached as
Parquet, so filter variants are compared without re-running the screen. Forward 1, 3, 6
and 12-month total returns (split-adjusted, dividends added, not reinvested) are measured
against the equal-weight average of every company the screen looked at that month.

Known biases, reported rather than hidden:
- Survivorship: prices exist only for companies listed today, so the universe and picks
  miss names that later delisted. `survivorship_gap` says how many filers are missing.
- A stock whose prices stop inside a window exits at its last close (`exit_gaps`).
- SIC codes (financial exclusion) and tickers are today's snapshot.

The leak check reruns everything with numbers visible from their period end instead of
their filing date. That version must score better; if it does not, the harness cannot
tell look-ahead from skill.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import duckdb
import pyarrow.parquet as pq

from src.db.pit import Policy
from src.db.queries import MIN_PRICE, PRICE_BREAK
from src.obsidian.writer import atomic_write, upsert_note
from src.screen import Filters, metrics_sql, rank_sql

HORIZONS = (1, 3, 6, 12)
EXIT_GAP_DAYS = 10          # last close this far before the exit date: stopped trading
SNAPSHOT_DROP = ("sources", "buyers", "form4_accns")   # traceability lives in the live screen

VARIANTS: dict[str, Filters] = {
    "default": Filters(),                                   # value + insider buying
    "drawdown_50": Filters(min_drawdown=0.50),              # the M4 default
    "loose": Filters(min_drawdown=0.30, max_ev_ebit=10.0),
    "value": Filters(require_insider=False),
    "insider_fcf": Filters(max_ev_ebit=None, max_net_debt_to_mcap=None),
}
LEAK_VARIANT = "value"      # enough picks per month to measure; the default has 0-1


def month_ends(start: date, end: date) -> list[date]:
    out, y, m = [], start.year, start.month
    while True:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        if d > end:
            return out
        if d >= start:
            out.append(d)
        y, m = nxt.year, nxt.month


# ---- snapshots --------------------------------------------------------------------

# Content hashes, not row counts: a corrected price or a replaced filing with the same number
# of rows must still invalidate (review C1). Each dated table is hashed once per run, grouped
# by the day its rows become visible; a date's hash is the XOR of every day up to it. XOR of
# per-row hashes is order-free and streams, so nothing counts distinct rows of the facts table.
_DATED_INPUTS = {
    "facts": ("xbrl_facts", "{key}", "cik, taxonomy, tag, unit, period_start, period_end, period_days, "
              "period_kind, fy, fp, form, accn, filed, value"),
    "form4": ("form4_transactions", "filed", "accn, line_no, is_derivative, is_holding, issuer_cik, owner_cik, "
              "owner_name, is_director, is_officer, officer_title, filed, txn_date, security_title, txn_code, "
              "acq_disp, shares, price, shares_owned_following, ownership_form, nature_of_ownership, "
              "is_10b5_1, footnotes, form_type, period_of_report"),
    "prices": ("prices", "price_date", "cik, price_date, close_raw"),
}
_STATIC_INPUTS = {
    "splits": ("splits", "cik, ex_date, ratio"),
    "concept_map": ("concept_map", "concept, taxonomy, tag, unit, priority"),
    "companies": ("companies", "cik, name, is_financial, is_shell"),
    "price_status": ("price_status", "cik, ticker, status"),
}


def _input_hashes(con: duckdb.DuckDBPyConnection, policy: Policy) -> Callable[[date], list[int]]:
    """Returns a function giving, for a date, the hash of every input visible by then. Facts
    become visible on their filing date, or their period end for the leaky policy."""
    key = "period_end" if policy == "leaky_period_end" else "filed"
    prefix: dict[str, tuple[list[date], list[int]]] = {}
    for name, (table, day, cols) in _DATED_INPUTS.items():
        day = day.format(key=key)
        rows = con.execute(f"SELECT {day} AS d, bit_xor(hash({cols})) FROM {table} GROUP BY 1 ORDER BY 1").fetchall()
        days, acc, x = [], [], 0
        for d, h in rows:
            x ^= int(h)
            days.append(d)
            acc.append(x)
        prefix[name] = (days, acc)
    static = [int(con.execute(f"SELECT coalesce(bit_xor(hash({cols})), 0) FROM {table}").fetchall()[0][0])
              for table, cols in _STATIC_INPUTS.values()]
    # The SQL calls macros by name (split_factor, form4_effective); their bodies are inputs too
    # (review C5). Read from the connection, so a redefinition by any path is seen.
    macros = con.execute("""SELECT coalesce(bit_xor(hash(function_name, parameters::VARCHAR, macro_definition)), 0)
                            FROM duckdb_functions()
                            WHERE function_type IN ('macro', 'table_macro') AND NOT internal""").fetchall()
    static.append(int(macros[0][0]))

    def at(d: date) -> list[int]:
        out = []
        for days, acc in prefix.values():
            i = bisect.bisect_right(days, d)
            out.append(acc[i - 1] if i else 0)
        return out + static
    return at


def _fingerprint(sql: str, hashes: list[int]) -> str:
    return hashlib.sha256(json.dumps([sql, hashes]).encode()).hexdigest()[:24]


def snapshots(con: duckdb.DuckDBPyConnection, dates: list[date], policy: Policy, cache_dir: Path,
              insider_window_days: int = 90, progress: Callable[[date, bool], None] | None = None) -> int:
    """Write metrics-<date>.parquet per date under cache_dir/policy. Returns how many were
    (re)computed; a snapshot whose fingerprint still matches is kept."""
    out = cache_dir / policy
    out.mkdir(parents=True, exist_ok=True)
    sql = metrics_sql(insider_window_days, policy)
    hashes_at = _input_hashes(con, policy)
    computed = 0
    for d in dates:
        path = out / f"metrics-{d.isoformat()}.parquet"
        fp = _fingerprint(sql, hashes_at(d))
        fresh = path.exists() and (pq.read_schema(path).metadata or {}).get(b"fingerprint") == fp.encode()
        if not fresh:
            table = con.sql(sql, params={"as_of": d}).to_arrow_table()
            table = table.drop_columns([c for c in SNAPSHOT_DROP if c in table.column_names])
            table = table.replace_schema_metadata({b"fingerprint": fp.encode(), b"policy": policy.encode()})
            tmp = path.with_name(f".tmp-{path.name}")
            pq.write_table(table, tmp)
            tmp.replace(path)
            computed += 1
        if progress:
            progress(d, not fresh)
    return computed


# ---- forward returns --------------------------------------------------------------

def returns_sql(source: str) -> str:
    """source rows (cik, as_of, price_date, close_raw, ...) plus ret_<h>m and exit_gap_<h>m.

    Total return on today's share basis: price / split_factor, plus split-adjusted dividends
    with ex-date after entry and on or before exit. NULL when the exit date is after the last
    price in the database (the future). A stock that stops trading exits at its last close.
    A window that contains a price break (see PRICE_BREAK) has no return: price_break_<h>m.
    """
    known = "m = {h} AND target <= last.d"
    cols = ",\n".join(
        f"max(CASE WHEN {known.format(h=h)} AND NOT broke THEN ret END) AS ret_{h}m, "
        f"bool_or(CASE WHEN {known.format(h=h)} THEN gap END) AS exit_gap_{h}m, "
        f"bool_or(CASE WHEN {known.format(h=h)} THEN broke END) AS price_break_{h}m" for h in HORIZONS)
    return f"""
    WITH s AS (SELECT * FROM {source}),
    last AS (SELECT max(price_date) AS d FROM prices),
    legs AS (
        SELECT s.cik, s.as_of, s.price_date AS entry_date, s.close_raw AS entry_raw, h.m,
               -- Month end h months on: for month-end rebalances the 1m exit is the next
               -- rebalance date (April 30 + 1 month is May 31, not May 30; review C2).
               last_day((s.as_of + to_months(h.m))::DATE) AS target
        FROM s CROSS JOIN (SELECT unnest({list(HORIZONS)}) AS m) h
        WHERE s.close_raw IS NOT NULL
    ),
    ex AS (
        SELECT l.*, p.price_date AS exit_date, p.close_raw AS exit_raw
        FROM legs l ASOF LEFT JOIN prices p ON p.cik = l.cik AND p.price_date <= l.target
    ),
    adj AS (SELECT cik, price_date, close_raw / split_factor(cik, price_date) AS a FROM prices),
    pb AS (
        SELECT cik, price_date AS d FROM (
            SELECT cik, price_date, a, lag(a) OVER (PARTITION BY cik ORDER BY price_date) AS prev FROM adj
        ) WHERE a >= {PRICE_BREAK} * prev
    ),
    cd AS (
        SELECT cik, ex_date,
               sum(amount_raw / split_factor(cik, ex_date)) OVER (PARTITION BY cik ORDER BY ex_date) AS cum
        FROM dividends
    ),
    r AS (
        SELECT ex.cik, ex.as_of, ex.m, ex.target,
               (ex.exit_raw / split_factor(ex.cik, ex.exit_date) + coalesce(c2.cum, 0) - coalesce(c1.cum, 0))
                   / (ex.entry_raw / split_factor(ex.cik, ex.entry_date)) - 1 AS ret,
               ex.exit_date < ex.target - INTERVAL {EXIT_GAP_DAYS} DAY AS gap,
               EXISTS (SELECT 1 FROM pb WHERE pb.cik = ex.cik AND pb.d > ex.entry_date
                                            AND pb.d <= ex.exit_date) AS broke
        FROM ex
        ASOF LEFT JOIN cd c1 ON c1.cik = ex.cik AND c1.ex_date <= ex.entry_date
        ASOF LEFT JOIN cd c2 ON c2.cik = ex.cik AND c2.ex_date <= ex.exit_date
    ),
    w AS (SELECT cik, as_of, {cols} FROM r CROSS JOIN last GROUP BY cik, as_of)
    SELECT s.*, w.* EXCLUDE (cik, as_of) FROM s LEFT JOIN w USING (cik, as_of)
    """


def panel_sql(source: str) -> str:
    """Investable rows of source (close of at least MIN_PRICE on the date) with forward returns.
    Picks and the benchmark universe both come from it."""
    return returns_sql(f"(SELECT * FROM {source} WHERE close_raw >= {MIN_PRICE})")


# ---- statistics -------------------------------------------------------------------

def _max_drawdown(curve: list[float]) -> float:
    peak, dd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak)
    return dd


def _cagr(curve: list[float]) -> float | None:
    return curve[-1] ** (12 / len(curve)) - 1 if curve and curve[-1] > 0 else None


def evaluate(con: duckdb.DuckDBPyConnection, ranked: str, cost_bps: float = 50) -> dict:
    """Statistics for ranked rows (as_of, cik, status, ret_<h>m, ...). cost_bps is one-way:
    each pick pays it on entry and exit; the monthly strategy pays it on what it trades."""
    one_way = cost_bps / 1e4
    cols = set(con.sql(f"SELECT * FROM {ranked} LIMIT 0").columns)
    horizons, halves = {}, {}
    for h in HORIZONS:
        ret = f"ret_{h}m"
        gap = f"count(*) FILTER (WHERE exit_gap_{h}m)" if f"exit_gap_{h}m" in cols else "0"
        brk = f"count(*) FILTER (WHERE price_break_{h}m)" if f"price_break_{h}m" in cols else "0"
        rows = con.execute(f"""
            WITH u AS (SELECT as_of, avg({ret}) AS u FROM {ranked} WHERE {ret} IS NOT NULL GROUP BY as_of),
            p AS (SELECT r.as_of, r.{ret} - {2 * one_way} AS ret, u.u
                  FROM {ranked} r JOIN u USING (as_of) WHERE r.status = 'shortlist'),
            -- A date where any pick's return is unknown (price break) is not measured at all:
            -- averaging the known picks would reweight the shortlist in hindsight (review C6).
            d AS (SELECT as_of, avg(ret) AS pr, any_value(u) AS u, count(*) AS n,
                         count(*) FILTER (WHERE ret > u) AS hits, bool_or(ret IS NULL) AS unknown
                  FROM p GROUP BY as_of)
            SELECT as_of, pr, u, n, hits, unknown FROM d ORDER BY as_of
        """).fetchall()
        unmeasured = sum(1 for r in rows if r[5])
        rows = [r for r in rows if not r[5]]
        n_gaps, n_breaks = con.execute(f"SELECT {gap}, {brk} FROM {ranked} WHERE status = 'shortlist'").fetchall()[0]
        picks = sum(r[3] for r in rows)
        horizons[h] = {
            "dates": len(rows), "picks": picks,
            "pick_return": sum(r[1] for r in rows) / len(rows) if rows else None,
            "universe_return": sum(r[2] for r in rows) / len(rows) if rows else None,
            "excess": sum(r[1] - r[2] for r in rows) / len(rows) if rows else None,
            "hit_rate": sum(r[4] for r in rows) / picks if picks else None,
            "exit_gaps": n_gaps, "price_breaks": n_breaks, "unmeasured_dates": unmeasured,
        }
        ex = [r[1] - r[2] for r in rows]
        mid = len(ex) // 2
        first, second = (sum(ex[:mid]) / mid if mid else None), (sum(ex[mid:]) / (len(ex) - mid) if ex else None)
        halves[h] = {"split_date": rows[mid][0] if rows else None, "first": first, "second": second,
                     "one_half_only": None if first is None or second is None else (first > 0) != (second > 0)}

    # Monthly strategy: hold the shortlist one month, equal weight, cash when it is empty.
    months = con.execute(f"""
        SELECT as_of, avg(ret_1m) AS u,
               list({{'cik': cik, 'r': ret_1m}} ORDER BY cik) FILTER (WHERE status = 'shortlist') AS picks
        FROM {ranked} GROUP BY as_of HAVING count(ret_1m) > 0 ORDER BY as_of
    """).fetchall()
    skipped = 0
    curve, ucurve, turns = [], [], []
    held: dict = {"cash": 1.0}      # weights at the end of the last month, after each holding's return
    v = uv = 1.0
    for _, u, picks in months:
        picks = picks or []         # no shortlist this month: the FILTER list is NULL
        target = {p["cik"]: 1 / len(picks) for p in picks} if picks else {"cash": 1.0}
        if any(p["r"] is None for p in picks):
            # A holding's month is unknown (price break): the month is left out of both curves
            # rather than credited to the other holdings (review C6). Without drift
            # information the next rebalance trades from the target weights.
            skipped += 1
            held = target
            continue
        delta = {k: abs(target.get(k, 0) - held.get(k, 0)) for k in set(target) | set(held)}
        turns.append(sum(delta.values()) / 2)
        # Only stock legs pay: moving from cash into one stock is one purchase (review C3).
        cost = sum(dw for k, dw in delta.items() if k != "cash") * one_way
        pr = sum(p["r"] for p in picks) / len(picks) if picks else 0.0
        v *= 1 + pr - cost
        uv *= 1 + u
        curve.append(v)
        ucurve.append(uv)
        # Weights drift with returns; the next rebalance trades back from there (review C4).
        grown = {p["cik"]: target[p["cik"]] * (1 + p["r"]) for p in picks}
        total = sum(grown.values())
        held = {k: g / total for k, g in grown.items()} if picks and total > 0 else {"cash": 1.0}
    monthly = {"months": len(curve), "months_with_picks": sum(1 for m in months if m[2]) - skipped,
               "months_unmeasured": skipped,
               "cagr": _cagr(curve), "universe_cagr": _cagr(ucurve),
               "turnover": sum(turns) / len(turns) if turns else None,
               "max_drawdown": _max_drawdown(curve) if curve else None,
               "universe_max_drawdown": _max_drawdown(ucurve) if ucurve else None}
    return {"horizons": horizons, "monthly": monthly, "halves": halves}


def survivorship_gap(con: duckdb.DuckDBPyConnection, dates: list[date]) -> float | None:
    """Average share of active non-financial filers (a 10-K or 10-Q in the last 15 months)
    with no price on the date. Free price feeds drop delisted names, so this is the part of
    the past the backtest cannot see."""
    row = con.execute("""
        WITH d AS (SELECT unnest($dates::DATE[]) AS as_of),
        filings AS (SELECT DISTINCT cik, filed FROM xbrl_facts WHERE form IN ('10-K', '10-Q')),
        active AS (
            SELECT DISTINCT d.as_of, f.cik FROM d JOIN filings f
              ON f.filed <= d.as_of AND f.filed > d.as_of - INTERVAL 456 DAY
            JOIN companies c ON c.cik = f.cik AND NOT c.is_financial AND NOT c.is_shell
        ),
        g AS (
            SELECT a.as_of, avg(CASE WHEN ps.first_date <= a.as_of AND ps.last_date >= a.as_of THEN 0 ELSE 1 END) AS gap
            FROM active a LEFT JOIN price_status ps ON ps.cik = a.cik AND ps.status = 'ok'
            GROUP BY a.as_of
        )
        SELECT avg(gap) FROM g
    """, {"dates": dates}).fetchall()
    return row[0][0]


# ---- report -----------------------------------------------------------------------

def _pct(v, digits: int = 1) -> str:
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:.{digits}f}%"


def render_report(results: dict[str, dict[str, dict]], meta: dict) -> str:
    """results[variant][policy] = evaluate(...)."""
    lines = [f"# Backtest {meta['run_date']}", "",
             f"Monthly rebalance {meta['start']} to {meta['end']}, numbers as first reported and known by each "
             f"date. Returns are total returns (dividends added), equal weight, against the equal-weight "
             f"universe the screen saw. Costs: {meta['cost_bps']:g} bps each way.", ""]
    lines += ["## Excess return over the universe", "",
              "| variant | policy | dates with picks | avg picks | " + " | ".join(f"{h}m excess" for h in HORIZONS)
              + " | 3m hit rate | 12m hit rate |",
              "|---|---|---|---|" + "---|" * len(HORIZONS) + "---|---|"]
    for name, by_policy in results.items():
        for policy, s in by_policy.items():
            h = s["horizons"]
            avg = h[1]["picks"] / h[1]["dates"] if h[1]["dates"] else 0
            lines.append(f"| {name} | {policy} | {h[1]['dates']} | {avg:.1f} | "
                         + " | ".join(_pct(h[k]["excess"]) for k in HORIZONS)
                         + f" | {_pct(h[3]['hit_rate'], 0)} | {_pct(h[12]['hit_rate'], 0)} |")
    lines += ["", "## Monthly strategy (hold the shortlist one month, cash when empty)", "",
              "| variant | policy | months | with picks | CAGR | universe CAGR | max drawdown | universe max DD | turnover |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, by_policy in results.items():
        for policy, s in by_policy.items():
            m = s["monthly"]
            lines.append(f"| {name} | {policy} | {m['months']} | {m['months_with_picks']} | {_pct(m['cagr'])} | "
                         f"{_pct(m['universe_cagr'])} | {_pct(m['max_drawdown'])} | {_pct(m['universe_max_drawdown'])} | "
                         f"{_pct(m['turnover'], 0)} |")
    lines += ["", "## Halves (3m and 12m excess; a screen that works in one half only is flagged)", "",
              "| variant | policy | split | 3m first | 3m second | 12m first | 12m second | flag |",
              "|---|---|---|---|---|---|---|---|"]
    for name, by_policy in results.items():
        for policy, s in by_policy.items():
            a, b = s["halves"][3], s["halves"][12]
            flag = "one half only" if a["one_half_only"] or b["one_half_only"] else ""
            lines.append(f"| {name} | {policy} | {a['split_date'] or '-'} | {_pct(a['first'])} | {_pct(a['second'])} | "
                         f"{_pct(b['first'])} | {_pct(b['second'])} | {flag} |")
    leak = meta.get("leak_check")
    lines += ["", "## Checks", ""]
    if leak:
        lines.append(f"- Leak check ({leak['variant']}, 3m excess): real {_pct(leak['real'])}, leaky "
                     f"{_pct(leak['leaky'])}. {'PASS: the harness rewards look-ahead, so it can detect it.' if leak['passed'] else 'FAIL: the leaky version did not score better; results cannot be told apart from look-ahead.'}")
    lines.append(f"- Survivorship gap: {_pct(meta.get('survivorship_gap'))} of active non-financial filers "
                 "had no price on an average date (mostly later delisted). Results are an upper bound.")
    exit_gaps = {n: s["horizons"][3]["exit_gaps"] for n, by in results.items() for p, s in by.items()
                 if p == "as_first_reported"}
    lines.append(f"- Picks that stopped trading inside the 3m window (exit at last close): {exit_gaps}.")
    breaks = {n: s["horizons"][3]["price_breaks"] for n, by in results.items() for p, s in by.items()
              if p == "as_first_reported"}
    lines.append(f"- Picks left out of the 3m figures because the price series broke (10x in a day): {breaks}.")
    lines.append(f"- Universe and picks: names with a close of at least ${MIN_PRICE:g} on the rebalance date.")
    unmeasured = {n: (s["horizons"][3]["unmeasured_dates"], s["monthly"]["months_unmeasured"])
                  for n, by in results.items() for p, s in by.items() if p == "as_first_reported"}
    lines.append(f"- Dates left out because a pick's return was unknown (3m dates, 1m strategy months): {unmeasured}.")
    lines += ["", "## Variants", ""]
    for name, f in meta.get("variants", {}).items():
        lines.append(f"- `{name}`: {json.dumps(asdict(f), sort_keys=True)}")
    return "\n".join(lines) + "\n"


def write_report(md: str, run_date: date, reports_dir: Path, vault_dir: Path | None) -> tuple[Path, Path | None]:
    name = f"backtest-{run_date.isoformat()}"
    path = reports_dir / f"{name}.md"
    atomic_write(path, md)
    note = None
    if vault_dir:
        note = vault_dir / "Reports" / f"{name}.md"
        upsert_note(note, {"type": "backtest", "run_date": run_date.isoformat(),
                           "report": f"reports/{name}.md", "tags": ["backtest"]}, md)
    return path, note


def run(con: duckdb.DuckDBPyConnection, start: date, end: date, cache_dir: Path, cost_bps: float = 50,
        leak_check: bool = True, progress: Callable[[str, date, bool], None] | None = None) -> tuple[str, dict]:
    dates = month_ends(start, end)
    policies: list[Policy] = ["as_first_reported"] + (["leaky_period_end"] if leak_check else [])
    results: dict[str, dict[str, dict]] = {}
    for policy in policies:
        snapshots(con, dates, policy, cache_dir,
                  progress=(lambda d, c, p=policy: progress(p, d, c)) if progress else None)
        files = [str(cache_dir / policy / f"metrics-{d.isoformat()}.parquet") for d in dates]
        con.execute(f"CREATE OR REPLACE TEMP TABLE bt_snap AS SELECT * FROM read_parquet({files})")
        con.execute(f"CREATE OR REPLACE TEMP TABLE bt_panel AS {panel_sql('bt_snap')}")
        for name, f in VARIANTS.items():
            con.execute(f"CREATE OR REPLACE TEMP TABLE bt_ranked AS {rank_sql(f, 'bt_panel')}")
            results.setdefault(name, {})[policy] = evaluate(con, "bt_ranked", cost_bps)
    leak = None
    if leak_check:
        real = results[LEAK_VARIANT]["as_first_reported"]["horizons"][3]["excess"]
        leaky = results[LEAK_VARIANT]["leaky_period_end"]["horizons"][3]["excess"]
        leak = {"variant": LEAK_VARIANT, "real": real, "leaky": leaky,
                "passed": real is not None and leaky is not None and leaky > real}
    meta = {"run_date": date.today(), "start": dates[0], "end": dates[-1], "cost_bps": cost_bps,
            "survivorship_gap": survivorship_gap(con, dates), "leak_check": leak, "variants": VARIANTS}
    return render_report(results, meta), {"results": results, "meta": meta}
