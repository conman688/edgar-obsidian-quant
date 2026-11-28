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
    tkc AS (
        -- Rows priced under more than one ticker in the year may be two securities (a primary
        -- switch from debt or warrants to common, or a rename). Never one series until reviewed.
        SELECT cik FROM prices WHERE price_date BETWEEN $as_of - INTERVAL 365 DAY AND $as_of
        GROUP BY cik HAVING count(DISTINCT ticker) > 1
    ),
    held AS (
        -- Source actions that could not be tied to a security (review C2): unresolved inputs.
        SELECT DISTINCT cik FROM price_actions_held WHERE ex_date BETWEEN $as_of - INTERVAL 365 DAY AND $as_of
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
               tkc.cik IS NOT NULL AS ticker_change,
               held.cik IS NOT NULL AS held_source_action,
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
        LEFT JOIN hi USING (cik) LEFT JOIN brk USING (cik) LEFT JOIN tkc USING (cik) LEFT JOIN held USING (cik)
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
            CASE WHEN ticker_change THEN 'ticker_change' END,
            CASE WHEN held_source_action THEN 'held_source_action' END,
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


def conviction_buys_sql(window_days: int = 30, include_unknown_10b5_1: bool = False) -> str:
    """One row per discretionary open-market purchase known by as_of (filed <= as_of,
    not txn_date) with a trade date in the window. Binds $as_of.

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
    return f"""
        SELECT t.accn, t.line_no, t.issuer_cik, t.owner_cik, t.owner_name, t.is_officer, t.is_director,
               t.officer_title, t.txn_date, t.filed, t.shares, t.price, t.shares * t.price AS usd, t.is_10b5_1
        FROM form4_effective($as_of, p_since := $as_of - INTERVAL {int(window_days)} DAY) t
        LEFT JOIN prices p ON p.cik = t.issuer_cik AND p.price_date = t.txn_date
        WHERE {_open_market_trade("P", "A", window_days, plan_filter)}
    """


def _open_market_trade(code: str, acq_disp: str, window_days: int | None, plan_filter: str | None = None) -> str:
    """WHERE clause shared by buys and sells over form4_effective t LEFT JOIN prices p: an
    officer's or director's common-stock trade with this code, dated in the window (None:
    any date) and not after its filing, priced within 0.5-2x that day's close when one is
    stored, with no offering named in the footnotes.

    For the buys it renders main's text byte for byte: the backtest's snapshot fingerprint
    hashes this SQL, and a changed text would discard every cached month (review M12-A1 C1)."""
    window = "" if window_days is None else f"\n          AND t.txn_date > $as_of - INTERVAL {int(window_days)} DAY"
    plan = "" if plan_filter is None else f"\n          AND {plan_filter}"
    return f"""NOT t.is_derivative AND NOT t.is_holding
          AND t.txn_code = '{code}' AND t.acq_disp = '{acq_disp}'
          AND t.price > 0 AND t.shares > 0{window}
          AND t.txn_date <= t.filed          -- a later date is a typo (2033 in a 2022 filing){plan}
          AND (t.is_officer OR t.is_director)
          AND NOT regexp_matches(coalesce(t.security_title, ''),
                                 '(?i)preferred|warrant|note|debenture|unit|right|option')
          AND NOT regexp_matches(coalesce(t.footnotes, ''),
                                 '(?i)initial public offering|\\bIPO\\b|private placement|underwritten|registered direct|public offering|subscription agreement|securities purchase agreement')
          AND (p.close_raw IS NULL OR t.price BETWEEN 0.5 * p.close_raw AND 2 * p.close_raw)"""


def insider_sells(con: duckdb.DuckDBPyConnection, as_of: date, window_days: int = 90):
    """Officer and director open-market sales known by as_of; see insider_sells_sql."""
    return con.sql(insider_sells_sql(window_days), params={"as_of": as_of})


def insider_sells_sql(window_days: int = 90) -> str:
    """One row per officer or director open-market sale (code S, disposed) known by as_of
    (filed <= as_of) with a trade date in the window, under the same row rules as the buys.
    Binds $as_of. Informational (M12-A1): never feeds status or rank.

    plan: 'planned' (10b5-1 box or footnote), 'discretionary' (box unchecked, or no box
    before 2023-04-01 when none existed), 'unknown' (no box from 2023-04-01: a parse gap).
    An unchecked box can still be a grandfathered pre-2023 plan, so it is not proof.

    routine: the same owner sold this issuer's stock in the same calendar month in each of
    the 3 prior calendar years (Cohen, Malloy and Pomorski 2012, "Decoding Inside
    Information"; their routine trades carry no signal). The only way to separate
    scheduled selling before the 10b5-1 box existed.
    """
    return f"""
    WITH s AS (
        SELECT t.accn, t.line_no, t.issuer_cik, t.owner_cik, t.owner_name, t.is_officer, t.is_director,
               t.officer_title, t.txn_date, t.filed, t.shares, t.price, t.shares * t.price AS usd, t.is_10b5_1,
               CASE WHEN t.is_10b5_1 THEN 'planned'
                    WHEN t.is_10b5_1 = FALSE OR t.filed < DATE '2023-04-01' THEN 'discretionary'
                    ELSE 'unknown' END AS plan
        FROM form4_effective($as_of, p_since := $as_of - INTERVAL {int(window_days)} DAY) t
        LEFT JOIN prices p ON p.cik = t.issuer_cik AND p.price_date = t.txn_date
        WHERE {_open_market_trade("S", "D", window_days)}
    ),
    hist AS (        -- earlier sales by the same owners of the same issuers, known by as_of,
                     -- under the same row rules (any plan status): offerings never make a routine
        SELECT DISTINCT t.issuer_cik, t.owner_cik, year(t.txn_date) AS y, month(t.txn_date) AS m
        -- Bounded to filings from Jan 1, 3 years before the window's first year: the full
        -- history is 25x slower (164 s against 7 s on the 2026-11-21 database).
        FROM form4_effective($as_of, p_since := make_date(year($as_of - INTERVAL {int(window_days)} DAY) - 3, 1, 1)
                                                 - INTERVAL 1 DAY) t
        SEMI JOIN (SELECT DISTINCT issuer_cik, owner_cik FROM s) o
             ON o.issuer_cik = t.issuer_cik AND o.owner_cik = t.owner_cik
        LEFT JOIN prices p ON p.cik = t.issuer_cik AND p.price_date = t.txn_date
        WHERE {_open_market_trade("S", "D", None)}
    ),
    r AS (
        SELECT s.accn, s.line_no, count(DISTINCT h.y) = 3 AS routine
        FROM s LEFT JOIN hist h
          ON h.issuer_cik = s.issuer_cik AND h.owner_cik = s.owner_cik AND h.m = month(s.txn_date)
         AND h.y BETWEEN year(s.txn_date) - 3 AND year(s.txn_date) - 1
        GROUP BY s.accn, s.line_no
    )
    SELECT s.*, r.routine FROM s JOIN r USING (accn, line_no)
    """


def insider_flow_sql(window_days: int = 90) -> str:
    """Per issuer, insider selling and net flow in the window as of $as_of (M12-A1,
    informational). Sold, sellers and net flow count discretionary, non-routine sales only;
    planned, unknown-plan and routine sales are summed apart. Net flow is every conviction
    buy (not only clusters) minus those sales. n_peer_sellers: discretionary sellers who did
    not also buy. Exercises (M, shares acquired) and gifts (G) are counted as context;
    tax withholding (F) is not."""
    w = int(window_days)
    disc = "plan = 'discretionary' AND NOT routine"
    return f"""
    WITH s AS ({insider_sells_sql(w)}),
    b AS ({conviction_buys_sql(w)}),
    bs AS (
        SELECT issuer_cik, round(sum(usd), 2) AS usd_bought, list(DISTINCT owner_cik) AS buyer_ciks
        FROM b GROUP BY issuer_cik
    ),
    ss AS (
        SELECT issuer_cik,
               round(sum(usd) FILTER (WHERE {disc}), 2) AS usd_sold,     -- cents: float sums vary with thread order
               nullif(count(DISTINCT owner_cik) FILTER (WHERE {disc}), 0) AS n_sellers,
               round(sum(usd) FILTER (WHERE plan = 'planned'), 2) AS usd_planned,
               round(sum(usd) FILTER (WHERE plan = 'unknown'), 2) AS usd_unknown,
               round(sum(usd) FILTER (WHERE plan = 'discretionary' AND routine), 2) AS usd_routine,
               list_sort(list(DISTINCT owner_name) FILTER (WHERE {disc})) AS sellers,
               list(DISTINCT owner_cik) FILTER (WHERE {disc}) AS seller_ciks,
               list_sort(list(DISTINCT accn) FILTER (WHERE {disc})) AS accns
        FROM s GROUP BY issuer_cik
    ),
    ctx AS (
        SELECT t.issuer_cik,
               nullif(count(DISTINCT t.owner_cik) FILTER (WHERE t.txn_code = 'M' AND t.acq_disp = 'A'), 0)
                   AS n_exercisers,
               nullif(count(DISTINCT t.owner_cik) FILTER (WHERE t.txn_code = 'G'), 0) AS n_gifters
        FROM form4_effective($as_of, p_since := $as_of - INTERVAL {w} DAY) t
        WHERE NOT t.is_derivative AND NOT t.is_holding AND t.txn_code IN ('M', 'G')
          AND t.txn_date > $as_of - INTERVAL {w} DAY AND t.txn_date <= t.filed
          AND (t.is_officer OR t.is_director)
        GROUP BY t.issuer_cik
    )
    SELECT issuer_cik, ss.usd_sold, ss.n_sellers, ss.usd_planned, ss.usd_unknown, ss.usd_routine,
           ss.sellers, ss.accns,
           CASE WHEN bs.usd_bought IS NOT NULL OR ss.usd_sold IS NOT NULL
                THEN round(coalesce(bs.usd_bought, 0) - coalesce(ss.usd_sold, 0), 2) END AS net_flow,
           nullif(len(list_filter(coalesce(ss.seller_ciks, []),
                                  lambda x: NOT list_contains(coalesce(bs.buyer_ciks, []), x))), 0) AS n_peer_sellers,
           ctx.n_exercisers, ctx.n_gifters
    FROM ss FULL JOIN bs USING (issuer_cik) FULL JOIN ctx USING (issuer_cik)
    """


def cluster_buys_sql(window_days: int = 30, include_unknown_10b5_1: bool = False) -> str:
    """Issuers with a cluster of conviction buys (see conviction_buys_sql) in the window:
    two or more insiders, or one purchase of $100k or more."""
    return f"""
    WITH buys AS ({conviction_buys_sql(window_days, include_unknown_10b5_1)})
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


# M12-A2: 8-K items read as red flags, with how far back each counts. 5.02 covers departures,
# elections and appointments alike (Codex, Form 8-K check 2026-11-25), so its label is neutral
# and its window short. Order here is the order of `signals`.
EVENT_ITEMS: dict[str, tuple[str, str, int]] = {
    "4.02": ("8k_non_reliance", "non-reliance on prior financial statements", 365),
    "4.01": ("8k_auditor_change", "change in certifying accountant", 365),
    "2.06": ("8k_impairment", "material impairment", 365),
    "3.01": ("8k_listing_notice", "delisting or listing-rule notice, or transfer", 365),
    "5.02": ("8k_officer_change", "director or officer change", 90),
}
RESULTS_ITEM = "2.02"


def events_8k_sql() -> str:
    """One row per (8-K, red-flag item) known by as_of (filed <= as_of: the SEC filing date,
    never the acceptance time) inside that item's window. Binds $as_of. Items are SEC's
    comma-separated string, matched as whole tokens; the legacy integers used before
    2004-08-23 never match. Informational (M12-A2): never feeds status or rank."""
    cases = "\n".join(f"WHEN '{k}' THEN {w}" for k, (_, _, w) in EVENT_ITEMS.items())
    return f"""
    WITH t AS (
        SELECT e.accn, e.cik, e.form, e.filed, trim(unnest(string_split(e.items, ','))) AS item
        FROM filings_8k e
        WHERE e.filed <= $as_of AND e.filed > $as_of - INTERVAL {max(w for *_, w in EVENT_ITEMS.values())} DAY
    )
    SELECT accn, cik, form, filed, item FROM t
    WHERE filed > $as_of - (CASE item {cases} END) * INTERVAL 1 DAY
    """


def last_results_8k_sql() -> str:
    """Per company, the newest 8-K carrying Item 2.02 (results) filed by as_of. Binds $as_of."""
    return f"""
    SELECT cik, max(filed) AS last_results_8k FROM filings_8k
    WHERE filed <= $as_of AND list_contains(list_transform(string_split(items, ','), lambda x: trim(x)), '{RESULTS_ITEM}')
    GROUP BY cik
    """


# M12-A3: short interest as a share of shares outstanding that counts as high, and how old the
# newest cycle may be (3 cycles) before a company is treated as having none.
HIGH_SHORT_INTEREST = 0.10
# Screen issues that make the cover-page share count unusable as a denominator.
UNTRUSTED_SHARE_ISSUES = ("stale_shares", "share_jump", "foreign_filer")
SHORT_INTEREST_MAX_AGE_DAYS = 45


def short_interest_sql() -> str:
    """Per company, the newest FINRA cycle known by as_of (known_at <= as_of, never the
    settlement date), matched on the company's current ticker. Binds $as_of."""
    return f"""
    SELECT ps.cik, s.settlement_date, s.known_at, s.short_shares, s.days_to_cover
    FROM short_interest s JOIN price_status ps ON ps.ticker = s.symbol
    WHERE s.known_at <= $as_of AND s.settlement_date > $as_of - INTERVAL {SHORT_INTEREST_MAX_AGE_DAYS} DAY
    QUALIFY row_number() OVER (PARTITION BY ps.cik ORDER BY s.settlement_date DESC) = 1
    """


# M12-B6: 13F institutional holdings. A quarter is read once its filing deadline has passed.

# A CUSIP's validity under its symbol ends this long after its last fails-to-deliver sighting
# when the next CUSIP under that symbol was first seen much later (a reused ticker).
CUSIP_SLACK_DAYS = 92


def cusip_periods_sql() -> str:
    """When each CUSIP stands for a company: its fails-to-deliver symbol is the company's current
    ticker (price_status; a ticker two companies hold maps to neither). A CUSIP seen under another
    symbol more than CUSIP_SLACK_DAYS after its last sighting under this one only borrowed the
    ticker (live: the Roundhill Metaverse ETF traded as META 2021-07..2022-01, then METV to
    2026-10) and never stands for the company. Days under a temporary symbol at a CUSIP change
    do not count (live: HON's old CUSIP under HONZZZZ for 15 days, LRCX's under LRCXXXXX).
    The others take turns in order of their first sighting under any symbol (live: Meta's
    30303M102 traded as FB until 2022-06): each is valid from that sighting (the first one from
    the start, so it covers years before its first fail) until the next one's, or until
    CUSIP_SLACK_DAYS after its own last sighting if that is earlier. A same-ticker CUSIP change
    (reverse split, reorganisation) keeps the company's history; an old issuer's CUSIP under a
    reused ticker stays with the years it traded. Callers also require the quarter to fall in
    the company's own price history."""
    return f"""
    WITH c AS (SELECT cusip, min(first_seen) AS first_any, max(last_seen) AS last_any FROM cusip_symbols GROUP BY cusip),
         own AS (SELECT s.symbol, s.cusip, c.first_any, s.last_seen
                 FROM cusip_symbols s JOIN c USING (cusip)
                 WHERE NOT EXISTS (SELECT 1 FROM cusip_symbols o WHERE o.cusip = s.cusip AND o.symbol <> s.symbol
                                   AND o.last_seen > s.last_seen + INTERVAL {CUSIP_SLACK_DAYS} DAY)),
         e AS (SELECT symbol, cusip, first_any, lag(last_seen) OVER w AS prev_last, row_number() OVER w AS k
               FROM own WINDOW w AS (PARTITION BY symbol ORDER BY first_any, cusip)),
         v AS (SELECT symbol, cusip, k,
                      CASE WHEN k = 1 THEN DATE '0001-01-01'
                           ELSE least(first_any, CAST(prev_last + INTERVAL {CUSIP_SLACK_DAYS} DAY AS DATE)) END AS valid_from
               FROM e),
         iv AS (SELECT *, coalesce(lead(valid_from) OVER (PARTITION BY symbol ORDER BY k), DATE '9999-12-31') AS valid_to
                FROM v),
         t AS (SELECT ticker, min(cik) AS cik, min(first_date) AS price_from, max(last_date) AS price_to
               FROM price_status GROUP BY ticker HAVING count(*) = 1)
    SELECT t.cik, iv.cusip, iv.valid_from, iv.valid_to, t.price_from, t.price_to
    FROM iv JOIN t ON t.ticker = iv.symbol
    """


def institutional_sql() -> str:
    """Per mapped company, the newest 13F quarter with on-time reports whose deadline is <= as_of
    (never the quarter end). Shares are read under the CUSIP valid at that quarter's end. A
    manager holds the company in a quarter if it reports any CUSIP valid for the company during
    that quarter (live HON 2026-06-30: the new CUSIP came on 2026-06-16 and some managers still
    reported the old one). A company no manager reported has 0 holders. New and exited count
    managers with a report in both this quarter and the one before, so a CUSIP change is not a
    change of holders; NULL when the quarter before has no on-time reports. Binds $as_of."""
    return f"""
    WITH p AS (SELECT * FROM form13f_periods WHERE deadline <= $as_of AND filers > 0 ORDER BY period DESC LIMIT 1),
         pp AS (SELECT f.period FROM form13f_periods f CROSS JOIN p
                WHERE f.period = last_day(p.period - INTERVAL 3 MONTH) AND f.filers > 0),
         cp AS ({cusip_periods_sql()}),
         m AS (SELECT c.cik, c.cusip FROM cp c CROSS JOIN p
               WHERE c.valid_from <= p.period AND p.period < c.valid_to AND c.price_from <= p.period
                 AND p.period <= c.price_to + INTERVAL {CUSIP_SLACK_DAYS} DAY),
         -- every CUSIP valid for the company at some time in quarter q (ending q.period)
         cq AS (SELECT c.cik, c.cusip, p.period FROM cp c CROSS JOIN p
                WHERE c.cik IN (SELECT cik FROM m) AND c.valid_from <= p.period
                  AND c.valid_to > last_day(p.period - INTERVAL 3 MONTH)
                UNION ALL
                SELECT c.cik, c.cusip, pp.period FROM cp c CROSS JOIN pp
                WHERE c.cik IN (SELECT cik FROM m) AND c.valid_from <= pp.period
                  AND c.valid_to > last_day(pp.period - INTERVAL 3 MONTH)),
         h AS (SELECT DISTINCT cq.cik, cq.period, f.filer_cik FROM cq
               JOIN form13f_positions f ON f.cusip = cq.cusip AND f.period = cq.period),
         both_ AS (SELECT f.filer_cik FROM form13f_positions f JOIN p USING (period)
                   INTERSECT SELECT f.filer_cik FROM form13f_positions f JOIN pp USING (period)),
         cur AS (SELECT h.cik, h.filer_cik FROM h JOIN p USING (period)),
         chg AS (SELECT coalesce(c.cik, o.cik) AS cik, count(*) FILTER (WHERE o.filer_cik IS NULL) AS new_,
                        count(*) FILTER (WHERE c.filer_cik IS NULL) AS gone
                 FROM (SELECT * FROM cur WHERE filer_cik IN (SELECT filer_cik FROM both_)) c
                 FULL JOIN (SELECT h.cik, h.filer_cik FROM h JOIN pp USING (period)
                            WHERE h.filer_cik IN (SELECT filer_cik FROM both_)) o
                   ON o.cik = c.cik AND o.filer_cik = c.filer_cik
                 GROUP BY 1),
         n AS (SELECT cik, count(*) AS holders FROM cur GROUP BY cik)
    SELECT m.cik, p.period AS inst_period, p.deadline AS inst_known_at, q.title AS inst_title,
           coalesce(n.holders, 0) AS inst_holders, coalesce(q.shares, 0) AS inst_shares,
           CASE WHEN EXISTS (SELECT 1 FROM pp) THEN coalesce(chg.new_, 0) END AS inst_holders_new,
           CASE WHEN EXISTS (SELECT 1 FROM pp) THEN coalesce(chg.gone, 0) END AS inst_holders_exited
    FROM m CROSS JOIN p LEFT JOIN form13f_quarters q ON q.period = p.period AND q.cusip = m.cusip
    LEFT JOIN chg ON chg.cik = m.cik LEFT JOIN n ON n.cik = m.cik
    """


STAKE_MIN_PCT = 5.0
NEW_13D_DAYS = 90


def stakes_sql() -> str:
    """Per issuer, the stakes of 5% or more at $as_of (M12-B7): each filer's latest Schedule 13D or
    13G filed on or before as_of (never by event date) stands, so an amendment below 5% ends the
    stake and a filer moving from 13G to 13D counts once, under 13D. A filing listed under its
    subject only has no filer: it stands for itself. Unreadable percents count as no stake.
    new_13d: a holder of a 13D stake whose initial 13D was filed in the last NEW_13D_DAYS."""
    return f"""
    WITH s AS (SELECT *, coalesce(CAST(filer_cik AS VARCHAR), accn) AS holder FROM sched13_stakes
               WHERE filed <= $as_of AND issuer_cik IS NOT NULL),
         last AS (SELECT * FROM s
                  QUALIFY row_number() OVER (PARTITION BY issuer_cik, holder
                                             ORDER BY filed DESC, accepted DESC NULLS LAST, accn DESC) = 1),
         on_ AS (SELECT l.*, n.holder IS NOT NULL AS new_ FROM last l
                 LEFT JOIN (SELECT DISTINCT issuer_cik, holder FROM s
                            WHERE form = '13D' AND NOT amendment AND filed > $as_of - INTERVAL {NEW_13D_DAYS} DAY) n
                   USING (issuer_cik, holder)
                 WHERE l.pct >= {STAKE_MIN_PCT})
    SELECT issuer_cik AS cik,
           count(*) FILTER (WHERE form = '13D') AS holders_13d,
           max(pct) FILTER (WHERE form = '13D') AS max_pct_13d,
           max(filed) FILTER (WHERE form = '13D') AS last_filed_13d,
           arg_max(filer_name, pct) FILTER (WHERE form = '13D') AS top_filer_13d,
           count(*) FILTER (WHERE form = '13G') AS holders_13g,
           max(pct) FILTER (WHERE form = '13G') AS max_pct_13g,
           bool_or(form = '13D' AND new_) AS new_13d
    FROM on_ GROUP BY issuer_cik
    """
