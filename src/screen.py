"""The weekly screen: cheap, out-of-favour companies with insiders buying, as of a date.

Joins valuation, share-count trend and cluster buys over the priced, non-financial
universe. Every input is point-in-time (filed <= as_of). Each filter is TRUE, FALSE
or NULL (an input is missing), and each company gets one status:

- fail:       some filter is FALSE (`failed_filters` names them).
- quarantine: no filter fails, but an input is missing or suspect (`issues`). Never ranked.
- shortlist:  every filter passes on clean data. Ranked by EV/EBIT, cheapest first.

`flags` are warnings that do not block ranking (multi-class share counts, pre-tax
income standing in for EBIT); check those names by hand. `sources` and `form4_accns`
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
from src.db.queries import cluster_buys_sql, share_trend_sql, valuation_sql


@dataclass(frozen=True)
class Filters:
    min_drawdown: float = 0.50           # at least 50% below the 52-week high
    max_ev_ebit: float = 6.0
    max_net_debt_to_mcap: float = 0.25   # "net cash or low debt": net debt <= 25% of market cap
    insider_window_days: int = 90        # cluster buys known within this many days


def screen_sql(filters: Filters = Filters(), policy: Policy = "as_last_known") -> str:
    f = filters
    return f"""
    WITH v AS ({valuation_sql(policy)}),
    st AS ({share_trend_sql(policy)}),
    b AS ({cluster_buys_sql(f.insider_window_days)}),
    j AS (
        SELECT ps.ticker, c.name,
               v.* REPLACE (list_concat(v.issues, CASE WHEN st.quarantine THEN ['share_jump'] END) AS issues),
               v.fcf_ttm / nullif(v.mcap, 0) AS fcf_yield,
               st.cagr_1y AS share_cagr_1y, st.cagr_3y AS share_cagr_3y,
               b.n_insiders, b.n_c_suite, b.usd_bought AS insider_buy_90d_usd,
               b.usd_bought / nullif(v.mcap, 0) AS insider_buy_to_mcap,
               b.buyers, b.accns AS form4_accns,
               v.drawdown_52w > {float(f.min_drawdown)} AS pass_drawdown,
               v.fcf_ttm > 0 AS pass_fcf,
               CASE WHEN v.ebit_ttm <= 0 THEN FALSE ELSE v.ev_ebit < {float(f.max_ev_ebit)} END AS pass_ev_ebit,
               -v.net_cash_to_mcap <= {float(f.max_net_debt_to_mcap)} AS pass_debt,
               b.issuer_cik IS NOT NULL AS pass_insider
        FROM v
        JOIN companies c USING (cik)
        JOIN price_status ps USING (cik)
        LEFT JOIN st USING (cik)
        LEFT JOIN b ON b.issuer_cik = v.cik
        WHERE NOT c.is_financial AND NOT c.is_shell
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
    SELECT $as_of::DATE AS as_of,
           CASE WHEN status = 'shortlist'
                THEN row_number() OVER (PARTITION BY status ORDER BY ev_ebit, cik) END AS rank,
           status, ticker, name,
           * EXCLUDE (status, ticker, name)
    FROM r
    ORDER BY CASE status WHEN 'shortlist' THEN 0 WHEN 'quarantine' THEN 1 ELSE 2 END,
             ev_ebit NULLS LAST, cik
    """


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
