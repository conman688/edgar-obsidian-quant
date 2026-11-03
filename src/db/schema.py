"""DuckDB schema for point-in-time SEC fundamentals, insider flow and the run ledger.

Invariants:
- Fact rows are immutable. A restatement (10-K/A, recast comparatives in a later
  10-K) arrives as a new row with a new accession number. Nothing is UPDATEd.
- `filed` is the date the market could know a number. Every screen filters on it.
- Instant facts store period_start = period_end so the composite key has no NULLs.
"""
from __future__ import annotations

import duckdb

DDL = """
CREATE TABLE IF NOT EXISTS xbrl_facts (
    cik           INTEGER NOT NULL,
    taxonomy      VARCHAR NOT NULL,          -- 'us-gaap' | 'dei' | 'ifrs-full'
    tag           VARCHAR NOT NULL,
    unit          VARCHAR NOT NULL,          -- 'USD' | 'shares' | 'USD/shares' | 'pure'
    period_start  DATE    NOT NULL,
    period_end    DATE    NOT NULL,
    period_days   INTEGER NOT NULL,          -- 0 for instant facts
    period_kind   VARCHAR NOT NULL,          -- instant | quarterly | semiannual | ytd9m | annual | other
    fy            INTEGER,
    fp            VARCHAR,
    form          VARCHAR NOT NULL,
    accn          VARCHAR NOT NULL,
    filed         DATE    NOT NULL,
    frame         VARCHAR,
    value         DOUBLE  NOT NULL,
    PRIMARY KEY (cik, taxonomy, tag, unit, period_start, period_end, accn)
);

-- Ratio = new shares per old share. 2-for-1 forward split = 2.0, 1-for-10 reverse = 0.1.
CREATE TABLE IF NOT EXISTS splits (
    cik      INTEGER NOT NULL,
    ex_date  DATE    NOT NULL,
    ratio    DOUBLE  NOT NULL CHECK (ratio > 0),
    source   VARCHAR NOT NULL,
    PRIMARY KEY (cik, ex_date)
);

-- close_raw is the price printed that day, on that day's share basis. Never mix a
-- split-adjusted price with an as-filed share count.
CREATE TABLE IF NOT EXISTS prices (
    cik         INTEGER NOT NULL,
    ticker      VARCHAR NOT NULL,
    price_date  DATE    NOT NULL,
    close_raw   DOUBLE  NOT NULL,
    source      VARCHAR NOT NULL,
    PRIMARY KEY (cik, price_date)
);

-- Ordered fallback ladder: lowest priority wins when a filer reports several tags.
CREATE TABLE IF NOT EXISTS concept_map (
    concept   VARCHAR NOT NULL,
    taxonomy  VARCHAR NOT NULL,
    tag       VARCHAR NOT NULL,
    unit      VARCHAR NOT NULL,
    priority  INTEGER NOT NULL,
    PRIMARY KEY (concept, taxonomy, tag, unit)
);

CREATE TABLE IF NOT EXISTS form4_transactions (
    accn                    VARCHAR NOT NULL,
    line_no                 INTEGER NOT NULL,
    is_derivative           BOOLEAN NOT NULL,
    is_holding              BOOLEAN NOT NULL,     -- holding row (no transaction)
    issuer_cik              INTEGER NOT NULL,
    owner_cik               INTEGER NOT NULL,     -- canonical person key
    owner_name              VARCHAR NOT NULL,
    is_director             BOOLEAN,
    is_officer              BOOLEAN,
    is_ten_pct              BOOLEAN,
    officer_title           VARCHAR,
    filed                   DATE    NOT NULL,
    txn_date                DATE,
    security_title          VARCHAR,
    txn_code                VARCHAR,              -- P, S, A, M, F, G, ...
    acq_disp                VARCHAR,              -- 'A' acquired | 'D' disposed (NOT the grant code)
    shares                  DOUBLE,
    price                   DOUBLE,
    shares_owned_following  DOUBLE,
    ownership_form          VARCHAR,              -- 'D' direct | 'I' indirect
    nature_of_ownership     VARCHAR NOT NULL DEFAULT '',
    is_10b5_1               BOOLEAN,              -- NULL = unknown, never coerced to FALSE
    footnotes               VARCHAR,
    PRIMARY KEY (accn, is_derivative, is_holding, line_no)
);

-- One row per (accession, stage, schema, model). Claim before work, mark done after.
CREATE TABLE IF NOT EXISTS ledger (
    accn            VARCHAR NOT NULL,
    stage           VARCHAR NOT NULL,     -- 'facts' | 'form4' | 'llm:footnotes' | 'vault'
    schema_version  INTEGER NOT NULL,
    model           VARCHAR NOT NULL DEFAULT '',
    input_sha256    VARCHAR,
    status          VARCHAR NOT NULL,     -- 'pending' | 'done' | 'failed'
    output_ref      VARCHAR,
    error           VARCHAR,
    updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (accn, stage, schema_version, model)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key   VARCHAR PRIMARY KEY,      -- sha256(model|schema_version|prompt|chunk)
    response    JSON    NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE OR REPLACE MACRO split_factor(p_cik, p_date) AS (
    SELECT coalesce(product(ratio), 1.0) FROM splits WHERE cik = p_cik AND ex_date > p_date
);
"""

CONCEPT_SEED: list[tuple[str, str, str, str, int]] = [
    ("revenue", "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD", 1),
    ("revenue", "us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax", "USD", 2),
    ("revenue", "us-gaap", "Revenues", "USD", 3),
    ("revenue", "us-gaap", "SalesRevenueNet", "USD", 4),
    ("revenue", "us-gaap", "SalesRevenueGoodsNet", "USD", 5),
    ("operating_income", "us-gaap", "OperatingIncomeLoss", "USD", 1),
    ("diluted_shares", "us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", 1),
    ("shares_outstanding", "dei", "EntityCommonStockSharesOutstanding", "shares", 1),
    ("shares_outstanding", "us-gaap", "CommonStockSharesOutstanding", "shares", 2),
    ("cash", "us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD", 1),
    ("cash", "us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD", 2),
    ("st_investments", "us-gaap", "ShortTermInvestments", "USD", 1),
    ("st_investments", "us-gaap", "MarketableSecuritiesCurrent", "USD", 2),
    ("debt_total", "us-gaap", "DebtLongtermAndShorttermCombinedAmount", "USD", 1),
    ("debt_total", "us-gaap", "LongTermDebt", "USD", 2),
    ("debt_noncurrent", "us-gaap", "LongTermDebtNoncurrent", "USD", 1),
    ("debt_current", "us-gaap", "LongTermDebtCurrent", "USD", 1),
    ("debt_current", "us-gaap", "DebtCurrent", "USD", 2),
    ("cfo", "us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD", 1),
    ("cfo", "us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "USD", 2),
    ("capex", "us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "USD", 1),
    ("capex", "us-gaap", "PaymentsToAcquireProductiveAssets", "USD", 2),
    ("goodwill", "us-gaap", "Goodwill", "USD", 1),
    ("equity", "us-gaap", "StockholdersEquity", "USD", 1),
    ("equity", "us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD", 2),
    ("current_assets", "us-gaap", "AssetsCurrent", "USD", 1),
    ("current_liabilities", "us-gaap", "LiabilitiesCurrent", "USD", 1),
    ("total_liabilities", "us-gaap", "Liabilities", "USD", 1),
    # Capital-allocation forensics: already in companyfacts, so no per-filing fetch or LLM needed.
    ("goodwill_impairment", "us-gaap", "GoodwillImpairmentLoss", "USD", 1),
    ("asset_impairment", "us-gaap", "AssetImpairmentCharges", "USD", 1),
    ("acquisitions_paid", "us-gaap", "PaymentsToAcquireBusinessesNetOfCashAcquired", "USD", 1),
    ("buybacks_paid", "us-gaap", "PaymentsForRepurchaseOfCommonStock", "USD", 1),
    ("sbc", "us-gaap", "ShareBasedCompensation", "USD", 1),
    # Maturity buckets are relative to the balance-sheet date: year N = fiscal year end + N.
    ("debt_due_y1", "us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths", "USD", 1),
    ("debt_due_y2", "us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo", "USD", 1),
    ("debt_due_y3", "us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree", "USD", 1),
    ("debt_due_y4", "us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour", "USD", 1),
    ("debt_due_y5", "us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive", "USD", 1),
    ("debt_due_after_y5", "us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive", "USD", 1),
]


def connect(path: str = "data/edgar.duckdb") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path)
    con.execute(DDL)
    con.executemany(
        "INSERT INTO concept_map VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", CONCEPT_SEED
    )
    return con
