"""As-of screeners. Every query takes $as_of and sees only filings with filed <= as_of.

Market cap = raw close on as_of x cover-page shares outstanding from the latest
filing on or before as_of, with the shares scaled for any split between the cover
date and the price date. Historical share-count trends are split-adjusted to as_of.
"""
from __future__ import annotations

from datetime import date

import duckdb

from src.db.pit import Policy, concepts_sql, latest_instant_sql, ttm_flow_sql


def share_count_trend(con: duckdb.DuckDBPyConnection, as_of: date, policy: Policy = "as_last_known"):
    """1y and 3y diluted share CAGR on annual weighted-average diluted shares."""
    return con.sql(share_trend_sql(policy), params={"as_of": as_of})


def share_trend_sql(policy: Policy = "as_last_known") -> str:
    """SQL behind share_count_trend(); binds only $as_of.

    Split-adjusts every period to as_of basis via split_factor(cik, period_end).
    |CAGR| > 40%/yr is quarantined: almost always a missed split, a spin-off or an
    M&A share issue, not buybacks.
    """
    sql = f"""
    WITH s AS (
        SELECT cik, period_end, value * split_factor(cik, period_end) / split_factor(cik, $as_of) AS shares
        FROM ({concepts_sql(policy, ('diluted_shares',))}) WHERE period_kind = 'annual'
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
    return sql


# Older than this, a cover-page share count, balance sheet or TTM figure no longer
# describes the company: timely filers report every quarter, within ~90 days of it.
STALE_DAYS = 200
STALE_PRICE_DAYS = 7          # last close older than this: halted, delisted or a feed gap
MIN_PRICE = 1.0               # below $1 a quote moves a tick at a time and cannot be traded in size
# A one-day rise of 10x or more in the split-adjusted close is a broken series (bankruptcy
# shares replaced under the same ticker, an unadjusted reverse split), not a market move.
PRICE_BREAK = 10.0
DEI_SHARES = "EntityCommonStockSharesOutstanding"

_INSTANTS = {"sh": "shares_outstanding", "cash": "cash", "sti": "st_investments", "dt": "debt_total",
             "dc": "debt_current", "dn": "debt_noncurrent", "stb": "short_term_debt",
             "gw": "goodwill", "eq": "equity"}
# Debt tags that already include short-term borrowings, so those are not added again.
_DEBT_INCL_SHORT_TERM = ("DebtLongtermAndShorttermCombinedAmount", "DebtCurrent")
_FLOWS = ("ebit", "cfo", "capex")


def valuation_sql(policy: Policy = "as_last_known") -> str:
    """SQL behind valuation(); binds only $as_of, so other screens can embed it."""
    def one(concept: str) -> str:
        return f"SELECT * FROM pit WHERE concept = '{concept}'"

    parts = {k: latest_instant_sql(policy, one(c)) for k, c in _INSTANTS.items()}
    parts |= {k: ttm_flow_sql(policy, one(k)) for k in _FLOWS}
    ctes = ",\n".join(f"{k} AS ({v})" for k, v in parts.items())
    return f"""
    WITH pit AS MATERIALIZED ({concepts_sql(policy, tuple(_INSTANTS.values()) + _FLOWS)}),
    {ctes},
    px AS (
        SELECT cik, close_raw, price_date FROM prices WHERE price_date <= $as_of
        QUALIFY row_number() OVER (PARTITION BY cik ORDER BY price_date DESC) = 1
    ),
    hi AS (
        -- Prices move opposite to share counts: a 10-for-1 split divides old prices by 10.
        SELECT cik, max(close_raw / split_factor(cik, price_date) * split_factor(cik, $as_of)) AS hi_52w
        FROM prices WHERE price_date BETWEEN $as_of - INTERVAL 365 DAY AND $as_of GROUP BY cik
    ),
    brk AS (
        SELECT DISTINCT cik FROM (
            SELECT cik, close_raw / split_factor(cik, price_date) AS adj,
                   lag(close_raw / split_factor(cik, price_date)) OVER (PARTITION BY cik ORDER BY price_date) AS prev
            FROM prices WHERE price_date BETWEEN $as_of - INTERVAL 365 DAY AND $as_of
        ) WHERE adj >= {PRICE_BREAK} * prev
    ),
    bs AS (
        -- Balance-sheet date: the newest period among the items EV is built from. An item
        -- last reported before it is stale (the filer stopped tagging it or moved tags).
        SELECT cik, max(period_end) AS bs_date FROM (
            SELECT cik, period_end FROM cash UNION ALL SELECT cik, period_end FROM dt
            UNION ALL SELECT cik, period_end FROM dc UNION ALL SELECT cik, period_end FROM dn
            UNION ALL SELECT cik, period_end FROM stb UNION ALL SELECT cik, period_end FROM eq
        ) GROUP BY cik
    ),
    debt_src AS (
        SELECT ids.cik, dt.value AS t_val, dt.period_end AS t_end, dt.tag AS t_tag, dt.accn AS t_accn,
               dc.value AS c_val, dc.period_end AS c_end, dc.tag AS c_tag, dc.accn AS c_accn,
               dn.value AS n_val, dn.period_end AS n_end, dn.tag AS n_tag, dn.accn AS n_accn,
               stb.value AS s_val, stb.period_end AS s_end, stb.tag AS s_tag, stb.accn AS s_accn,
               greatest(dt.period_end, dc.period_end, dn.period_end, stb.period_end) AS d_end
        FROM (SELECT cik FROM dt UNION SELECT cik FROM dc UNION SELECT cik FROM dn UNION SELECT cik FROM stb) ids
        LEFT JOIN dt USING (cik) LEFT JOIN dc USING (cik) LEFT JOIN dn USING (cik) LEFT JOIN stb USING (cik)
    ),
    debt_lines AS (
        -- Debt is read at its newest date, d_end. Which lines are reported then:
        SELECT *,
               coalesce(t_end = d_end, FALSE) AS has_t,
               coalesce(c_end = d_end, FALSE) AS has_c,
               coalesce(n_end = d_end, FALSE) AS has_n,
               coalesce(s_end = d_end, FALSE) AS has_s,
               -- A "short and long term" total that equals current + noncurrent LTD leaves
               -- short-term borrowings out (WMT), whatever its name says.
               coalesce(t_tag IN {_DEBT_INCL_SHORT_TERM}
                        AND NOT (t_end = d_end AND c_end = d_end AND n_end = d_end
                                 AND abs(t_val - c_val - n_val) <= 0.01 * abs(t_val)), FALSE) AS t_incl_s,
               coalesce(c_tag IN {_DEBT_INCL_SHORT_TERM}, FALSE) AS c_incl_s
        FROM debt_src
    ),
    debt_sums AS (
        -- Two alternative sources at d_end: the total, and current + noncurrent. Each gets
        -- short-term borrowings added unless its tag already includes them.
        SELECT *,
               CASE WHEN has_t THEN t_val + CASE WHEN has_s AND NOT t_incl_s THEN s_val ELSE 0 END END AS by_total,
               CASE WHEN has_c OR has_n OR (has_s AND NOT has_t)
                    THEN CASE WHEN has_c THEN c_val ELSE 0 END + CASE WHEN has_n THEN n_val ELSE 0 END
                         + CASE WHEN has_s AND NOT (has_c AND c_incl_s) THEN s_val ELSE 0 END END AS by_split
        FROM debt_lines
    ),
    debt AS (
        SELECT cik, d_end AS debt_period_end,
               -- The larger source is a lower bound on what is owed.
               greatest(by_total, by_split) AS debt,
               -- The sources conflict when a complete split misses the total by over 10%,
               -- or a current line exceeds a total that should contain it.
               coalesce(has_t AND has_n AND abs(by_total - by_split) > 0.10 * greatest(abs(by_total), abs(by_split)), FALSE)
                 OR coalesce(has_t AND has_c AND c_val > 1.10 * t_val, FALSE) AS debt_sources_disagree,
               -- A positive line reported in the last 400 days but not at d_end, and not
               -- covered by a line that is, may still be owed: a missing tag is not a
               -- repayment. Older lines have been left out of a full 10-K since; lines under
               -- 5% of debt cannot move the screen. Current LTD is covered by short-term
               -- borrowings (JNJ shows it inside that line in 10-Qs).
               list_sum([
                   CASE WHEN t_end < d_end AND NOT has_n AND datediff('day', t_end, d_end) <= 400 THEN t_val END,
                   CASE WHEN c_end < d_end AND NOT has_t AND NOT has_s AND datediff('day', c_end, d_end) <= 400 THEN c_val END,
                   CASE WHEN n_end < d_end AND NOT has_t AND datediff('day', n_end, d_end) <= 400 THEN n_val END,
                   CASE WHEN s_end < d_end AND NOT ((has_t AND t_incl_s) OR (has_c AND c_incl_s))
                             AND datediff('day', s_end, d_end) <= 400 THEN s_val END
               ]) AS dropped_recent,
               array_to_string([CASE WHEN has_t THEN t_tag END, CASE WHEN has_c THEN c_tag END,
                                CASE WHEN has_n THEN n_tag END, CASE WHEN has_s THEN s_tag END], '+') AS debt_tag,
               list_distinct(list_filter([CASE WHEN has_t THEN t_accn END, CASE WHEN has_c THEN c_accn END,
                                          CASE WHEN has_n THEN n_accn END, CASE WHEN has_s THEN s_accn END],
                                         lambda a: a IS NOT NULL)) AS debt_accns
        FROM debt_sums
    ),
    base AS (
        SELECT px.cik, px.price_date, px.close_raw,
               datediff('day', px.price_date, $as_of) AS price_age_days,
               brk.cik IS NOT NULL AS price_break,
               -- Cover shares are counted before the price date; a split in between
               -- (NVDA: May 24 count, June 10 split) must scale them to the price's basis.
               px.close_raw * sh.value * split_factor(px.cik, sh.period_end)
                   / split_factor(px.cik, px.price_date) AS mcap,
               sh.tag AS shares_tag, sh.period_end AS shares_period_end,
               datediff('day', sh.period_end, $as_of) AS shares_age_days,
               bs.bs_date,
               -- Investments only count at the cash date: an old figure may be long sold.
               cash.value + CASE WHEN sti.period_end = cash.period_end THEN sti.value ELSE 0 END AS cash_like,
               -- 20-F / 40-F filers: annual cover shares, often non-USD statements, and an
               -- ADS price against an ordinary-share count. Market cap cannot be trusted.
               regexp_matches(coalesce(sh.form, ''), '^(20|40)-F')
                   OR regexp_matches(coalesce(cash.form, ''), '^(20|40)-F') AS foreign_filer,
               cash.period_end AS cash_period_end,
               debt.debt, debt.debt_period_end, debt.debt_sources_disagree,
               coalesce(debt.dropped_recent > 0.05 * greatest(debt.debt, 0)
                        OR (debt.dropped_recent > 0 AND debt.debt <= 0), FALSE) AS debt_line_dropped,
               ebit.value AS ebit_ttm, ebit.basis AS ebit_basis, ebit.tag AS ebit_tag,
               ebit.period_end AS ebit_period_end, ebit.same_tag AS ebit_same_tag,
               cfo.value AS cfo_ttm, capex.value AS capex_ttm, cfo.value - capex.value AS fcf_ttm,
               cfo.period_end AS cfo_period_end, capex.period_end AS capex_period_end,
               cfo.same_tag AND capex.same_tag AS fcf_same_tag,
               CASE WHEN gw.period_end = eq.period_end THEN gw.value / nullif(eq.value, 0) END AS goodwill_to_equity,
               hi.hi_52w,
               1 - px.close_raw / nullif(hi.hi_52w, 0) AS drawdown_52w,
               list_filter([
                   {{'item': 'shares', 'tag': sh.tag, 'period_end': sh.period_end, 'accns': [sh.accn]}},
                   {{'item': 'cash', 'tag': cash.tag, 'period_end': cash.period_end, 'accns': [cash.accn]}},
                   {{'item': 'st_investments', 'tag': sti.tag, 'period_end': sti.period_end,
                     'accns': CASE WHEN sti.period_end = cash.period_end THEN [sti.accn] END}},
                   {{'item': 'debt', 'tag': debt.debt_tag, 'period_end': debt.debt_period_end, 'accns': debt.debt_accns}},
                   {{'item': 'ebit', 'tag': ebit.tag, 'period_end': ebit.period_end, 'accns': ebit.accns}},
                   {{'item': 'cfo', 'tag': cfo.tag, 'period_end': cfo.period_end, 'accns': cfo.accns}},
                   {{'item': 'capex', 'tag': capex.tag, 'period_end': capex.period_end, 'accns': capex.accns}}
               ], lambda s: s.accns[1] IS NOT NULL) AS sources
        FROM px
        LEFT JOIN sh USING (cik) LEFT JOIN bs USING (cik)
        LEFT JOIN cash USING (cik) LEFT JOIN sti USING (cik) LEFT JOIN debt USING (cik)
        LEFT JOIN gw USING (cik) LEFT JOIN eq USING (cik)
        LEFT JOIN ebit USING (cik) LEFT JOIN cfo USING (cik) LEFT JOIN capex USING (cik)
        LEFT JOIN hi USING (cik) LEFT JOIN brk USING (cik)
    )
    SELECT *,
        mcap + debt - cash_like AS ev,
        CASE WHEN ebit_ttm > 0 THEN (mcap + debt - cash_like) / ebit_ttm END AS ev_ebit,
        (cash_like - debt) / nullif(mcap, 0) AS net_cash_to_mcap,
        shares_age_days > {STALE_DAYS} AS stale_shares,
        -- Multi-class filers tag cover shares per class, which companyfacts drops; what is
        -- left is a balance-sheet count of one class, or nothing (META, BRK, V).
        shares_tag IS DISTINCT FROM '{DEI_SHARES}' AS multi_class_risk,
        list_filter([
            CASE WHEN mcap IS NULL THEN 'missing_shares'
                 WHEN shares_age_days > {STALE_DAYS} THEN 'stale_shares' END,
            CASE WHEN price_age_days > {STALE_PRICE_DAYS} THEN 'stale_price' END,
            CASE WHEN close_raw < {MIN_PRICE} THEN 'penny_price' END,
            CASE WHEN price_break THEN 'price_break' END,
            CASE WHEN foreign_filer THEN 'foreign_filer' END,
            CASE WHEN cash_like IS NULL THEN 'missing_cash' WHEN cash_period_end < bs_date THEN 'stale_cash' END,
            CASE WHEN debt IS NULL THEN 'missing_debt'
                 WHEN debt_period_end < bs_date OR debt_line_dropped THEN 'stale_debt' END,
            CASE WHEN debt_sources_disagree THEN 'debt_sources_disagree' END,
            CASE WHEN datediff('day', bs_date, $as_of) > {STALE_DAYS} THEN 'stale_balance_sheet' END,
            CASE WHEN ebit_ttm IS NULL THEN 'missing_ebit' WHEN NOT ebit_same_tag THEN 'mixed_ebit_tags' END,
            CASE WHEN fcf_ttm IS NULL THEN 'missing_fcf'
                 WHEN cfo_period_end <> capex_period_end THEN 'fcf_period_mismatch'
                 WHEN NOT fcf_same_tag THEN 'mixed_fcf_tags' END,
            CASE WHEN datediff('day', least(ebit_period_end, cfo_period_end), $as_of) > {STALE_DAYS}
                 THEN 'stale_financials' END
        ], lambda x: x IS NOT NULL) AS issues
    FROM base
    """


def valuation(con: duckdb.DuckDBPyConnection, as_of: date, policy: Policy = "as_last_known"):
    """Market cap, EV, EV/EBIT, net cash / mcap, FCF, goodwill/equity, as of a date.

    One row per priced company. Missing inputs stay NULL and are named in `issues`
    (missing_debt, stale_cash, ...), so a filer that simply didn't tag debt cannot pass
    as debt-free and a stale number cannot pass as current. `sources` lists the tag,
    period and accession numbers behind each input. All concepts come from one scan.
    """
    return con.sql(valuation_sql(policy), params={"as_of": as_of})


def cluster_buys(con: duckdb.DuckDBPyConnection, as_of: date, window_days: int = 30,
                 include_unknown_10b5_1: bool = False):
    """Discretionary open-market buys known by as_of; see cluster_buys_sql."""
    return con.sql(cluster_buys_sql(window_days, include_unknown_10b5_1), params={"as_of": as_of})


def cluster_buys_sql(window_days: int = 30, include_unknown_10b5_1: bool = False) -> str:
    """Discretionary open-market buys known by as_of (filed <= as_of, not txn_date).

    Reads form4_effective(as_of), where amendments filed by as_of replace the filings
    they amend. Identical lots in one filing are separate purchases and all count.

    Code 'P' means open-market *or private* purchase, so it also covers IPO
    allocations, private placements and funds buying into deals. Kept: officers and
    directors buying common stock, outside 10b5-1 plans, with no offering named in
    the footnotes, at a price within 0.5-2x that day's close when one is stored
    (catches totals typed into the price field).
    """
    # Before 2023-04-01 no checkbox existed: NULL means "no plan footnote", accept it.
    # After that date the box is mandatory, so NULL means a parse gap: reject by default.
    plan_filter = ("t.is_10b5_1 IS DISTINCT FROM TRUE" if include_unknown_10b5_1 else
                   "(t.is_10b5_1 = FALSE OR (t.is_10b5_1 IS NULL AND t.filed < DATE '2023-04-01'))")
    sql = f"""
    WITH buys AS (
        SELECT t.accn, t.issuer_cik, t.owner_cik, t.owner_name, t.is_officer, t.officer_title, t.txn_date, t.filed,
               t.shares, t.price, t.shares * t.price AS usd, t.is_10b5_1
        FROM form4_effective($as_of, p_since := $as_of - INTERVAL {int(window_days)} DAY) t
        LEFT JOIN prices p ON p.cik = t.issuer_cik AND p.price_date = t.txn_date
        WHERE NOT t.is_derivative AND NOT t.is_holding
          AND t.txn_code = 'P' AND t.acq_disp = 'A'
          AND t.price > 0 AND t.shares > 0
          AND t.txn_date > $as_of - INTERVAL {int(window_days)} DAY
          AND t.txn_date <= t.filed          -- a later date is a typo (2033 in a 2022 filing)
          AND {plan_filter}
          AND (t.is_officer OR t.is_director)
          AND NOT regexp_matches(coalesce(t.security_title, ''),
                                 '(?i)preferred|warrant|note|debenture|unit|right|option')
          AND NOT regexp_matches(coalesce(t.footnotes, ''),
                                 '(?i)initial public offering|\\bIPO\\b|private placement|underwritten|registered direct|public offering|subscription agreement|securities purchase agreement')
          AND (p.close_raw IS NULL OR t.price BETWEEN 0.5 * p.close_raw AND 2 * p.close_raw)
    )
    SELECT issuer_cik,
           count(DISTINCT owner_cik) AS n_insiders,
           count(DISTINCT owner_cik) FILTER (WHERE regexp_matches(coalesce(officer_title, ''), '(?i)chief|ceo|cfo|president')) AS n_c_suite,
           round(sum(usd), 2) AS usd_bought,     -- cents: float sums vary with thread order
           max(usd) AS max_single_usd,
           list_sort(list(DISTINCT owner_name)) AS buyers,
           bool_or(is_10b5_1 IS NULL) AS has_unknown_plan_status,
           list_sort(list(DISTINCT accn)) AS accns
    FROM buys GROUP BY issuer_cik
    HAVING count(DISTINCT owner_cik) >= 2 OR max(usd) >= 100000
    """
    return sql


def insider_holdings(con: duckdb.DuckDBPyConnection, as_of: date):
    """Current holdings per (issuer, owner): last row per account, summed across accounts.

    Account = direct, or each distinct indirect nature (trust, LLC, spouse). Summing
    every row's shares_owned_following would count the same account once per row.
    """
    sql = """
    WITH last_per_account AS (
        SELECT issuer_cik, owner_cik, owner_name, ownership_form, nature_of_ownership,
               shares_owned_following
        FROM form4_effective($as_of)
        WHERE NOT is_derivative AND shares_owned_following IS NOT NULL
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
