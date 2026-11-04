"""Point-in-time resolution: which number did the market know on `as_of`?

Two policies:
- as_first_reported: the original print. Use for backtests of what traded on news.
- as_last_known: latest restatement filed on or before as_of. Use for live screens.
Both refuse to see anything filed after as_of, which removes look-ahead bias.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

import duckdb

Policy = Literal["as_first_reported", "as_last_known"]


def _resolve_sql(policy: Policy, concept_filter: str) -> str:
    order = "f.filed ASC" if policy == "as_first_reported" else "f.filed DESC"
    return f"""
    SELECT f.cik, f.period_start, f.period_end, f.period_days, f.period_kind, f.fy, f.fp,
           f.value, f.taxonomy, f.tag, f.unit, f.accn, f.filed, m.priority, f.form, m.concept
    FROM xbrl_facts f
    JOIN concept_map m
      ON {concept_filter} AND m.taxonomy = f.taxonomy AND m.tag = f.tag AND m.unit = f.unit
    WHERE f.filed <= $as_of
      -- No period ends after the filing that reports it. Typos (2034 cover dates) would
      -- otherwise win every "latest period" pick for years; a week covers cover pages
      -- dated a day or two after filing.
      AND f.period_end <= f.filed + INTERVAL 7 DAY
      AND f.period_end <= $as_of               -- nor after the screen date (negative ages)
    QUALIFY row_number() OVER (
        PARTITION BY m.concept, f.cik, f.period_kind, f.period_start, f.period_end
        -- The policy picks the filing; the tag ladder only breaks ties within it. Priority
        -- first would let a later filing's preferred tag override the first-reported value.
        ORDER BY {order}, m.priority ASC, f.accn {'ASC' if policy == 'as_first_reported' else 'DESC'}
    ) = 1
    """


def concept_sql(policy: Policy) -> str:
    """SQL resolving one concept per (cik, period_kind, period_end).

    Binds $as_of (DATE) and $concept (VARCHAR). The tag ladder is applied after the
    as-of filter, so a later filing that switches tags cannot leak backwards.
    """
    return _resolve_sql(policy, "m.concept = $concept")


def concepts_sql(policy: Policy, concepts: tuple[str, ...]) -> str:
    """concept_sql for several concepts in one scan of xbrl_facts; binds only $as_of.

    Rows carry a `concept` column. Each pass over the 100M-row facts table costs seconds,
    so callers that need many concepts resolve them all at once and filter after.
    """
    for c in concepts:
        if not c.replace("_", "").isalnum():
            raise ValueError(c)
    return _resolve_sql(policy, f"m.concept IN ({', '.join(repr(c) for c in concepts)})")


def concept(
    con: duckdb.DuckDBPyConnection, name: str, as_of: date, policy: Policy = "as_last_known"
) -> duckdb.DuckDBPyRelation:
    return con.sql(concept_sql(policy), params={"as_of": as_of, "concept": name})


def ttm_flow_sql(policy: Policy, source: str | None = None) -> str:
    """Trailing-twelve-month value for a duration concept, as of $as_of.

    TTM = latest FY + current YTD - prior-year YTD. Filers rarely tag a discrete Q4,
    so summing four quarters silently fails; the YTD bridge does not. YTD rows are
    matched on fiscal-year start (+/-10 days), not calendar dates, so non-December
    and 52/53-week fiscal years align. Falls back to the plain FY value.

    `source` is SQL yielding one concept's resolved rows (default: concept_sql, which
    binds $concept). `accns` lists the filings the value came from; `same_tag` is FALSE
    when the bridge mixes ladder tags (say operating income for the year, pre-tax YTD).
    """
    return f"""
    WITH c AS ({source or concept_sql(policy)}),
    fy AS (
        SELECT cik, value, period_end, tag, accn
        FROM c WHERE period_kind = 'annual'
        QUALIFY row_number() OVER (PARTITION BY cik ORDER BY period_end DESC) = 1
    ),
    ytd AS (
        SELECT c.cik, c.value, c.period_start, c.period_end, c.period_kind, c.tag, c.accn
        FROM c JOIN fy USING (cik)
        WHERE c.period_kind IN ('quarterly', 'semiannual', 'ytd9m')
          AND c.period_end > fy.period_end
          AND abs(datediff('day', fy.period_end, c.period_start)) <= 10
        QUALIFY row_number() OVER (PARTITION BY c.cik ORDER BY c.period_end DESC) = 1
    ),
    ytd_prior AS (
        SELECT p.cik, p.value, p.tag, p.accn
        FROM c p JOIN ytd y ON p.cik = y.cik AND p.period_kind = y.period_kind
        WHERE datediff('day', p.period_start, y.period_start) BETWEEN 355 AND 375
          AND datediff('day', p.period_end, y.period_end) BETWEEN 355 AND 375
        QUALIFY row_number() OVER (PARTITION BY p.cik ORDER BY p.period_end DESC) = 1
    )
    SELECT fy.cik,
           CASE WHEN ytd_prior.value IS NOT NULL THEN fy.value + ytd.value - ytd_prior.value
                ELSE fy.value END AS value,
           CASE WHEN ytd_prior.value IS NOT NULL THEN ytd.period_end ELSE fy.period_end END AS period_end,
           CASE WHEN ytd_prior.value IS NOT NULL THEN 'ttm_ytd_bridge' ELSE 'fy' END AS basis,
           fy.tag,
           CASE WHEN ytd_prior.value IS NOT NULL THEN [fy.accn, ytd.accn, ytd_prior.accn]
                ELSE [fy.accn] END AS accns,
           ytd_prior.value IS NULL OR (ytd.tag = fy.tag AND ytd_prior.tag = fy.tag) AS same_tag
    FROM fy LEFT JOIN ytd USING (cik) LEFT JOIN ytd_prior USING (cik)
    """


def latest_instant_sql(policy: Policy, source: str | None = None) -> str:
    """Most recent instant (balance-sheet) value per cik as of $as_of. `source` as in ttm_flow_sql."""
    return f"""
    SELECT * FROM ({source or concept_sql(policy)}) c
    WHERE period_kind = 'instant'
    QUALIFY row_number() OVER (PARTITION BY cik ORDER BY period_end DESC, filed DESC) = 1
    """
