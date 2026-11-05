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

def _fingerprint(con: duckdb.DuckDBPyConnection, as_of: date, sql: str) -> str:
    """Changes when anything the snapshot can read changes: data known by as_of (or with a
    period ending by then, for the leaky policy), splits, the universe, or the SQL."""
    counts = con.execute("""
        SELECT (SELECT count(*) FROM xbrl_facts WHERE filed <= $d OR period_end <= $d),
               (SELECT count(*) FROM form4_transactions WHERE filed <= $d),
               (SELECT count(*) FROM prices WHERE price_date <= $d),
               (SELECT count(*) FROM splits),
               (SELECT count(*) FROM companies WHERE NOT is_financial AND NOT is_shell)
    """, {"d": as_of}).fetchall()[0]
    return hashlib.sha256(json.dumps([sql, list(counts)]).encode()).hexdigest()[:24]


def snapshots(con: duckdb.DuckDBPyConnection, dates: list[date], policy: Policy, cache_dir: Path,
              insider_window_days: int = 90, progress: Callable[[date, bool], None] | None = None) -> int:
    """Write metrics-<date>.parquet per date under cache_dir/policy. Returns how many were
    (re)computed; a snapshot whose fingerprint still matches is kept."""
    out = cache_dir / policy
    out.mkdir(parents=True, exist_ok=True)
    sql = metrics_sql(insider_window_days, policy)
    computed = 0
    for d in dates:
        path = out / f"metrics-{d.isoformat()}.parquet"
        fp = _fingerprint(con, d, sql)
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
               (s.as_of + to_months(h.m))::DATE AS target
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
                  FROM {ranked} r JOIN u USING (as_of) WHERE r.status = 'shortlist' AND r.{ret} IS NOT NULL)
            SELECT as_of, avg(ret) AS pr, any_value(u) AS u, count(*) AS n, count(*) FILTER (WHERE ret > u) AS hits
            FROM p GROUP BY as_of ORDER BY as_of
        """).fetchall()
        n_gaps, n_breaks = con.execute(f"SELECT {gap}, {brk} FROM {ranked} WHERE status = 'shortlist'").fetchall()[0]
        picks = sum(r[3] for r in rows)
        horizons[h] = {
            "dates": len(rows), "picks": picks,
            "pick_return": sum(r[1] for r in rows) / len(rows) if rows else None,
            "universe_return": sum(r[2] for r in rows) / len(rows) if rows else None,
            "excess": sum(r[1] - r[2] for r in rows) / len(rows) if rows else None,
            "hit_rate": sum(r[4] for r in rows) / picks if picks else None,
            "exit_gaps": n_gaps, "price_breaks": n_breaks,
        }
        ex = [r[1] - r[2] for r in rows]
        mid = len(ex) // 2
        first, second = (sum(ex[:mid]) / mid if mid else None), (sum(ex[mid:]) / (len(ex) - mid) if ex else None)
        halves[h] = {"split_date": rows[mid][0] if rows else None, "first": first, "second": second,
                     "one_half_only": None if first is None or second is None else (first > 0) != (second > 0)}

    # Monthly strategy: hold the shortlist one month, equal weight, cash when it is empty.
    months = con.execute(f"""
        SELECT as_of, avg(ret_1m) AS u,
               list(cik ORDER BY cik) FILTER (WHERE status = 'shortlist' AND ret_1m IS NOT NULL) AS picks,
               avg(ret_1m) FILTER (WHERE status = 'shortlist') AS pr
        FROM {ranked} WHERE ret_1m IS NOT NULL GROUP BY as_of ORDER BY as_of
    """).fetchall()
    curve, ucurve, turns = [], [], []
    prev: dict = {"cash": 1.0}          # the strategy starts in cash
    v = uv = 1.0
    for _, u, picks, pr in months:
        w = {c: 1 / len(picks) for c in picks} if picks else {"cash": 1.0}
        traded = sum(abs(w.get(k, 0) - prev.get(k, 0)) for k in set(w) | set(prev))
        turns.append(traded / 2)
        v *= 1 + (pr or 0.0) - traded * one_way
        uv *= 1 + u
        curve.append(v)
        ucurve.append(uv)
        prev = w
    monthly = {"months": len(months), "months_with_picks": sum(1 for m in months if m[2]),
               "cagr": _cagr(curve), "universe_cagr": _cagr(ucurve),
               "max_drawdown": _max_drawdown(curve), "universe_max_drawdown": _max_drawdown(ucurve),
               "turnover": sum(turns) / len(turns) if turns else None}
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
