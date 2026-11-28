"""The weekly screen: cheap, out-of-favour companies with insiders buying, as of a date.

Joins valuation, share-count trend and cluster buys over the priced, non-financial
universe. Every input is point-in-time (filed <= as_of). Each filter is TRUE, FALSE
or NULL (an input is missing), and each company gets one status:

- fail:       some filter is FALSE (`failed_filters` names them).
- quarantine: no filter fails, but an input is missing or suspect (`issues`). Never ranked.
- shortlist:  every filter passes on clean data. Ranked by EV/EBIT, cheapest first.

`flags` are warnings that do not block ranking (multi-class share counts, pre-tax
income standing in for EBIT); check those names by hand. `signals` (M12) are
informational, such as insider selling: they are joined after ranking (`screen_sql`),
so no filter, status or rank can read them, and the backtest never computes them. `sources` and `form4_accns`
trace every number to the filings it came from.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from src.db.pit import Policy
from src.db.queries import (EVENT_ITEMS, HIGH_SHORT_INTEREST, UNTRUSTED_SHARE_ISSUES, cluster_buys_sql,
                             events_8k_sql, insider_flow_sql, institutional_sql, last_results_8k_sql, share_trend_sql,
                             short_interest_sql, stakes_sql, valuation_sql)


@dataclass(frozen=True)
class Filters:
    """None (or False) switches a filter off, for comparing variants in the backtest."""
    # Off by default: in the M5 backtest (2012-2026) the 50%-drawdown rule left picks in 27 of
    # 177 months and only worked after 2020; without it the screen beat the universe in both
    # halves (reports/backtest-2026-11-04.md). --min-drawdown 0.5 restores it.
    min_drawdown: float | None = None           # fraction below the 52-week high, e.g. 0.5
    max_ev_ebit: float | None = 6.0
    max_net_debt_to_mcap: float | None = 0.25   # "net cash or low debt": net debt <= 25% of mcap
    require_fcf: bool = True                    # trailing FCF > 0
    require_insider: bool = True                # a cluster buy known within the window
    insider_window_days: int = 90


def metrics_sql(insider_window_days: int = 90, policy: Policy = "as_last_known") -> str:
    """One row of screen inputs per priced, non-financial company as of $as_of. No filters."""
    return f"""
    WITH v AS ({valuation_sql(policy)}),
    st AS ({share_trend_sql(policy)}),
    b AS ({cluster_buys_sql(insider_window_days)})
    SELECT $as_of::DATE AS as_of, ps.ticker, c.name,
           v.* REPLACE (list_concat(v.issues, CASE WHEN st.quarantine THEN ['share_jump'] END) AS issues),
           v.fcf_ttm / nullif(v.mcap, 0) AS fcf_yield,
           st.cagr_1y AS share_cagr_1y, st.cagr_3y AS share_cagr_3y,
           b.n_insiders, b.n_c_suite, b.usd_bought AS insider_buy_90d_usd,
           b.usd_bought / nullif(v.mcap, 0) AS insider_buy_to_mcap,
           b.buyers, b.accns AS form4_accns
    FROM v
    JOIN companies c USING (cik)
    JOIN price_status ps USING (cik)
    LEFT JOIN st USING (cik)
    LEFT JOIN b ON b.issuer_cik = v.cik
    WHERE NOT c.is_financial AND NOT c.is_shell
    """


def rank_sql(filters: Filters, source: str) -> str:
    """Filters, status and rank over metrics rows in `source` (a table or subquery).
    Works on many as_of dates at once: ranks are per date."""
    f = filters

    def on(cond: str, enabled: bool) -> str:
        return cond if enabled else "TRUE"

    return f"""
    WITH j AS (
        SELECT *,
               {on(f"drawdown_52w > {float(f.min_drawdown or 0)}", f.min_drawdown is not None)} AS pass_drawdown,
               {on("fcf_ttm > 0", f.require_fcf)} AS pass_fcf,
               {on(f"CASE WHEN ebit_ttm <= 0 THEN FALSE ELSE ev_ebit < {float(f.max_ev_ebit or 0)} END",
                   f.max_ev_ebit is not None)} AS pass_ev_ebit,
               {on(f"-net_cash_to_mcap <= {float(f.max_net_debt_to_mcap or 0)}",
                   f.max_net_debt_to_mcap is not None)} AS pass_debt,
               {on("n_insiders IS NOT NULL", f.require_insider)} AS pass_insider
        FROM {source}
    ),
    s AS (
        SELECT *,
               list_filter([CASE WHEN NOT pass_drawdown THEN 'drawdown' END, CASE WHEN NOT pass_fcf THEN 'fcf' END,
                            CASE WHEN NOT pass_ev_ebit THEN 'ev_ebit' END, CASE WHEN NOT pass_debt THEN 'debt' END,
                            CASE WHEN NOT pass_insider THEN 'insider' END], lambda x: x IS NOT NULL) AS failed_filters,
               list_filter([CASE WHEN multi_class_risk THEN 'multi_class_risk' END,
                            CASE WHEN ebit_tag <> 'OperatingIncomeLoss' THEN 'ebit_is_pretax' END],
                           lambda x: x IS NOT NULL) AS flags
        FROM j
    ),
    r AS (
        SELECT *,
               CASE WHEN len(failed_filters) > 0 THEN 'fail'
                    WHEN len(issues) > 0 OR (pass_drawdown AND pass_fcf AND pass_ev_ebit AND pass_debt) IS NULL
                         THEN 'quarantine'
                    ELSE 'shortlist' END AS status
        FROM s
    )
    SELECT as_of,
           CASE WHEN status = 'shortlist'
                THEN row_number() OVER (PARTITION BY as_of, status ORDER BY ev_ebit, cik) END AS rank,
           status, ticker, name,
           * EXCLUDE (as_of, status, ticker, name)
    FROM r
    ORDER BY as_of, CASE status WHEN 'shortlist' THEN 0 WHEN 'quarantine' THEN 1 ELSE 2 END,
             ev_ebit NULLS LAST, cik
    """


# M12-B6: 13F titles of class that are not the ordinary shares the cover page counts.
INST_ADR_RE = r"\bADRS?\b|\bADS\b|SPON|DEPOSITAR"
INST_CLASS_RE = r"\bCL(ASS)?\.?\s*[A-Z]\b"


def informational_sql(ranked: str, insider_window_days: int = 90) -> str:
    """Ranked rows (one as_of) plus M12 information: insider selling and `signals`. Joined
    after ranking, so status, rank and order are exactly `ranked`'s. Binds $as_of.
    8-K signals (M12-A2) follow EVENT_ITEMS order."""
    eight_k = ",\n                        ".join(
        f"CASE WHEN list_contains(ev.items_8k, '{k}') THEN '{name}' END" for k, (name, _, _) in EVENT_ITEMS.items())
    return f"""
    WITH r AS ({ranked}),
    fl AS ({insider_flow_sql(insider_window_days)}),
    ev AS (SELECT cik, list(DISTINCT item) AS items_8k, list_sort(list(DISTINCT accn)) AS accns
           FROM ({events_8k_sql()}) GROUP BY cik),
    res AS ({last_results_8k_sql()}),
    si AS ({short_interest_sql()}),
    inst AS ({institutional_sql()}),
    st AS ({stakes_sql()}),
    x0 AS (
        SELECT r.*,
               -- FINRA counts shares at settlement: put them on the price date's split basis.
               si.short_shares * split_factor(r.cik, si.settlement_date) / split_factor(r.cik, r.price_date)
                   / nullif(r.mcap / nullif(r.close_raw, 0), 0) AS si_ratio,
               si.days_to_cover, si.settlement_date AS short_settlement_date, si.known_at AS short_known_at,
               -- 13F shares at the quarter end, on the price date's split basis (M12-B6).
               inst.inst_shares * split_factor(r.cik, inst.inst_period) / split_factor(r.cik, r.price_date)
                   / nullif(r.mcap / nullif(r.close_raw, 0), 0) AS inst_ratio,
               inst.inst_period, inst.inst_known_at, inst.inst_title, inst.inst_holders, inst.inst_holders_new,
               inst.inst_holders_exited
        FROM r LEFT JOIN si ON si.cik = r.cik LEFT JOIN inst ON inst.cik = r.cik
    ),
    -- A share of shares outstanding only where the count can be trusted: not stale, jumped or
    -- a 20-F/40-F count, and not below the short position itself (cover counts of 1 or 1,000
    -- shares). Days to cover is FINRA's own figure and is kept.
    x1 AS (
        SELECT * EXCLUDE (si_ratio, inst_title),
               CASE WHEN NOT list_has_any(issues, {list(UNTRUSTED_SHARE_ISSUES)}) AND si_ratio <= 1
                    THEN si_ratio END AS short_interest_pct,
               -- Why 13F shares are not a share of shares outstanding: 13F counts depositary
               -- shares or one class, or the count is untrusted. Above 100% is shown and flagged
               -- (managers sharing discretion can report the same shares).
               CASE WHEN inst_holders IS NULL THEN NULL
                    WHEN regexp_matches(coalesce(inst_title, ''), '{INST_ADR_RE}') THEN 'adr'
                    WHEN regexp_matches(coalesce(inst_title, ''), '{INST_CLASS_RE}') THEN 'share_class'
                    WHEN list_has_any(issues, {list(UNTRUSTED_SHARE_ISSUES)}) THEN 'untrusted_shares'
                    WHEN inst_ratio > 1 THEN 'above_100pct' END AS inst_pct_note
        FROM x0
    ),
    x AS (
        SELECT * EXCLUDE (inst_ratio),
               CASE WHEN inst_pct_note IS NULL OR inst_pct_note = 'above_100pct' THEN inst_ratio END AS inst_pct
        FROM x1
    )
    SELECT r.*,
           fl.usd_sold AS insider_sell_90d_usd, fl.n_sellers, fl.sellers,
           fl.usd_planned AS insider_sell_planned_90d_usd, fl.usd_unknown AS insider_sell_unknown_plan_90d_usd,
           fl.usd_routine AS insider_sell_routine_90d_usd, fl.net_flow AS net_insider_flow_90d_usd,
           fl.n_peer_sellers, fl.n_exercisers, fl.n_gifters, fl.accns AS form4_sell_accns,
           list_filter([CASE WHEN fl.n_sellers >= 2 THEN 'cluster_sell' END,
                        CASE WHEN r.n_insiders IS NOT NULL AND fl.n_peer_sellers > 0 THEN 'buy_while_peers_sell' END,
                        {eight_k},
                        CASE WHEN r.short_interest_pct >= {HIGH_SHORT_INTEREST} THEN 'high_short_interest' END,
                        CASE WHEN st.new_13d THEN 'new_13d' END],
                       lambda x: x IS NOT NULL) AS signals,
           ev.accns AS form8k_accns, res.last_results_8k,
           -- Schedule 13D/13G stakes of 5% or more (M12-B7)
           coalesce(st.holders_13d, 0) AS holders_13d, st.max_pct_13d, st.last_filed_13d, st.top_filer_13d,
           coalesce(st.holders_13g, 0) AS holders_13g, st.max_pct_13g
    FROM x r LEFT JOIN fl ON fl.issuer_cik = r.cik
    LEFT JOIN ev ON ev.cik = r.cik
    LEFT JOIN res ON res.cik = r.cik
    LEFT JOIN st ON st.cik = r.cik
    ORDER BY r.as_of, CASE r.status WHEN 'shortlist' THEN 0 WHEN 'quarantine' THEN 1 ELSE 2 END,
             r.ev_ebit NULLS LAST, r.cik
    """


def screen_sql(filters: Filters = Filters(), policy: Policy = "as_last_known") -> str:
    """The live screen: ranked metrics plus M12 information. The backtest ranks
    `metrics_sql` snapshots with `rank_sql` alone."""
    ranked = rank_sql(filters, f"({metrics_sql(filters.insider_window_days, policy)})")
    return informational_sql(ranked, filters.insider_window_days)


def screen(con: duckdb.DuckDBPyConnection, as_of: date, filters: Filters = Filters(),
           policy: Policy = "as_last_known") -> duckdb.DuckDBPyRelation:
    return con.sql(screen_sql(filters, policy), params={"as_of": as_of})


def write_parquet(table: pa.Table, as_of: date, filters: Filters, out_dir: Path,
                  policy: Policy = "as_last_known") -> Path:
    """Write one dated snapshot atomically; the filters used travel in the file metadata."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"screen-{as_of.isoformat()}.parquet"
    meta = {**(table.schema.metadata or {}), b"as_of": as_of.isoformat().encode(),
            b"policy": policy.encode(), b"filters": json.dumps(asdict(filters), sort_keys=True).encode()}
    tmp = path.with_name(f".tmp-{path.name}")
    pq.write_table(table.replace_schema_metadata(meta), tmp)
    os.replace(tmp, path)
    return path
