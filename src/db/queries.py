"""As-of screeners. Every query takes $as_of and sees only filings with filed <= as_of.

Market cap = raw close on as_of x cover-page shares outstanding from the latest
filing on or before as_of. Both are on the same (as-of) share basis, so no split
adjustment is needed. Historical share-count trends ARE split-adjusted to today.
"""
from __future__ import annotations

from datetime import date

import duckdb

from src.db.pit import Policy, concept_sql, latest_instant_sql, ttm_flow_sql


def share_count_trend(con: duckdb.DuckDBPyConnection, as_of: date, policy: Policy = "as_last_known"):
    """1y and 3y diluted share CAGR on annual weighted-average diluted shares.

    Split-adjusts every period to as_of basis via split_factor(cik, period_end).
    |CAGR| > 40%/yr is quarantined: almost always a missed split, a spin-off or an
    M&A share issue, not buybacks.
    """
    sql = f"""
    WITH s AS (
        SELECT cik, period_end, value * split_factor(cik, period_end) / split_factor(cik, $as_of) AS shares
        FROM ({concept_sql(policy)}) WHERE period_kind = 'annual'
    ),
    r AS (
        SELECT cik, period_end, shares,
               row_number() OVER (PARTITION BY cik ORDER BY period_end DESC) AS rn
        FROM s
    ),
    j AS (
        SELECT cur.cik, cur.period_end, cur.shares AS sh_now,
               y1.shares AS sh_1y, y1.period_end AS end_1y,
               y3.shares AS sh_3y, y3.period_end AS end_3y
        FROM r cur
        LEFT JOIN r y1 ON y1.cik = cur.cik AND y1.rn = 2
        LEFT JOIN r y3 ON y3.cik = cur.cik AND y3.rn = 4
        WHERE cur.rn = 1
    )
    SELECT *,
        CASE WHEN sh_1y > 0 THEN pow(sh_now / sh_1y, 365.25 / datediff('day', end_1y, period_end)) - 1 END AS cagr_1y,
        CASE WHEN sh_3y > 0 AND datediff('day', end_3y, period_end) BETWEEN 1000 AND 1200
             THEN pow(sh_now / sh_3y, 365.25 / datediff('day', end_3y, period_end)) - 1 END AS cagr_3y,
        coalesce(abs(sh_now / nullif(sh_1y, 0) - 1) > 0.40, FALSE) AS quarantine
    FROM j
    """
    return con.sql(sql, params={"as_of": as_of, "concept": "diluted_shares"})


def valuation(con: duckdb.DuckDBPyConnection, as_of: date, policy: Policy = "as_last_known"):
    """Market cap, EV, EV/EBIT, net cash / mcap, FCF, goodwill/equity, as of a date.

    Debt: total if tagged, else current + noncurrent. Missing debt stays NULL so a
    filer that simply didn't tag debt cannot pass as debt-free.
    """
    def bound(sql: str, concept: str) -> str:
        # DuckDB params are global per statement, so each CTE inlines its own concept.
        if not concept.replace("_", "").isalnum():
            raise ValueError(concept)
        return f"({sql.replace('$concept', repr(concept))})"

    inst = {k: f"(SELECT cik, value, period_end FROM {bound(latest_instant_sql(policy), k)})"
            for k in ("shares_outstanding", "cash", "st_investments", "debt_total",
                      "debt_current", "debt_noncurrent", "goodwill", "equity")}
    flow = {k: bound(ttm_flow_sql(policy), k) for k in ("operating_income", "cfo", "capex")}
    parts = {
        "sh": inst["shares_outstanding"], "cash": inst["cash"], "sti": inst["st_investments"],
        "dt": inst["debt_total"], "dc": inst["debt_current"], "dn": inst["debt_noncurrent"],
        "gw": inst["goodwill"], "eq": inst["equity"],
        "ebit": flow["operating_income"], "cfo": flow["cfo"], "capex": flow["capex"],
    }
    ctes = ",\n".join(f"{k} AS {v}" for k, v in parts.items())
    sql = f"""
    WITH {ctes},
    px AS (
        SELECT cik, close_raw, price_date FROM prices WHERE price_date <= $as_of
        QUALIFY row_number() OVER (PARTITION BY cik ORDER BY price_date DESC) = 1
    ),
    hi AS (
        SELECT cik, max(close_raw * split_factor(cik, price_date) / split_factor(cik, $as_of)) AS hi_52w
        FROM prices WHERE price_date BETWEEN $as_of - INTERVAL 365 DAY AND $as_of GROUP BY cik
    ),
    base AS (
        SELECT px.cik, px.price_date, px.close_raw,
               px.close_raw * sh.value AS mcap,
               coalesce(dt.value, dc.value + dn.value) AS debt,
               cash.value + coalesce(sti.value, 0) AS cash_like,
               ebit.value AS ebit_ttm, ebit.basis AS ebit_basis,
               cfo.value - capex.value AS fcf_ttm,
               gw.value / nullif(eq.value, 0) AS goodwill_to_equity,
               1 - px.close_raw / nullif(hi.hi_52w, 0) AS drawdown_52w,
               datediff('day', sh.period_end, $as_of) AS shares_age_days
        FROM px
        JOIN sh USING (cik)
        LEFT JOIN cash USING (cik) LEFT JOIN sti USING (cik)
        LEFT JOIN dt USING (cik) LEFT JOIN dc USING (cik) LEFT JOIN dn USING (cik)
        LEFT JOIN gw USING (cik) LEFT JOIN eq USING (cik)
        LEFT JOIN ebit USING (cik) LEFT JOIN cfo USING (cik) LEFT JOIN capex USING (cik)
        LEFT JOIN hi USING (cik)
    )
    SELECT *,
        mcap + debt - cash_like AS ev,
        CASE WHEN ebit_ttm > 0 THEN (mcap + debt - cash_like) / ebit_ttm END AS ev_ebit,
        (cash_like - debt) / nullif(mcap, 0) AS net_cash_to_mcap,
        shares_age_days > 200 AS stale_shares
    FROM base
    """
    return con.sql(sql, params={"as_of": as_of})


def cluster_buys(con: duckdb.DuckDBPyConnection, as_of: date, window_days: int = 30,
                 include_unknown_10b5_1: bool = False):
    """Discretionary open-market buys known by as_of (filed <= as_of, not txn_date).

    Excludes derivative rows, 10b5-1 plan trades, zero-price rows (gifts, transfers)
    and amendment duplicates (same owner/date/shares/price across accessions).
    """
    # Before 2023-04-01 no checkbox existed: NULL means "no plan footnote", accept it.
    # After that date the box is mandatory, so NULL means a parse gap: reject by default.
    plan_filter = ("is_10b5_1 IS DISTINCT FROM TRUE" if include_unknown_10b5_1 else
                   "(is_10b5_1 = FALSE OR (is_10b5_1 IS NULL AND filed < DATE '2023-04-01'))")
    sql = f"""
    WITH buys AS (
        SELECT DISTINCT ON (issuer_cik, owner_cik, txn_date, shares, price)
               issuer_cik, owner_cik, owner_name, is_officer, officer_title, txn_date, filed,
               shares, price, shares * price AS usd, is_10b5_1
        FROM form4_transactions
        WHERE NOT is_derivative AND NOT is_holding
          AND txn_code = 'P' AND acq_disp = 'A'
          AND price > 0 AND shares > 0
          AND filed <= $as_of
          AND txn_date > $as_of - INTERVAL {int(window_days)} DAY
          AND {plan_filter}
        ORDER BY issuer_cik, owner_cik, txn_date, shares, price, filed ASC
    )
    SELECT issuer_cik,
           count(DISTINCT owner_cik) AS n_insiders,
           count(DISTINCT owner_cik) FILTER (WHERE regexp_matches(coalesce(officer_title, ''), '(?i)chief|ceo|cfo|president')) AS n_c_suite,
           sum(usd) AS usd_bought,
           max(usd) AS max_single_usd,
           list(DISTINCT owner_name) AS buyers,
           bool_or(is_10b5_1 IS NULL) AS has_unknown_plan_status
    FROM buys GROUP BY issuer_cik
    HAVING count(DISTINCT owner_cik) >= 2 OR max(usd) >= 100000
    """
    return con.sql(sql, params={"as_of": as_of})


def insider_holdings(con: duckdb.DuckDBPyConnection, as_of: date):
    """Current holdings per (issuer, owner): last row per account, summed across accounts.

    Account = direct, or each distinct indirect nature (trust, LLC, spouse). Summing
    every row's shares_owned_following would count the same account once per row.
    """
    sql = """
    WITH last_per_account AS (
        SELECT issuer_cik, owner_cik, owner_name, ownership_form, nature_of_ownership,
               shares_owned_following
        FROM form4_transactions
        WHERE NOT is_derivative AND filed <= $as_of AND shares_owned_following IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY issuer_cik, owner_cik, ownership_form, nature_of_ownership
            ORDER BY coalesce(txn_date, filed) DESC, filed DESC, line_no DESC
        ) = 1
    )
    SELECT issuer_cik, owner_cik, any_value(owner_name) AS owner_name,
           sum(shares_owned_following) FILTER (WHERE ownership_form = 'D') AS direct_shares,
           sum(shares_owned_following) FILTER (WHERE ownership_form = 'I') AS indirect_shares,
           list(nature_of_ownership) FILTER (WHERE ownership_form = 'I') AS indirect_vehicles
    FROM last_per_account GROUP BY issuer_cik, owner_cik
    """
    return con.sql(sql, params={"as_of": as_of})
