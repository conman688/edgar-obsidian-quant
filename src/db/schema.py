"""DuckDB schema for point-in-time SEC fundamentals, insider flow and the run ledger.

Invariants:
- Schema changes are append-only MIGRATIONS; connect() applies pending ones.
- A restatement (10-K/A, recast comparatives in a later 10-K) arrives as new rows
  under a new accession number; earlier filings are never overwritten by it. A
  filing's own rows are replaced only when SEC's snapshot of that same filing
  changes (facts added, removed or corrected), detected by its fingerprint.
- `filed` is the date the market could know a number. Every screen filters on it.
- Instant facts store period_start = period_end so key columns have no NULLs.
- xbrl_facts has no PRIMARY KEY: an index over ~100M rows would dominate memory and
  load time. Uniqueness is enforced per filing by the loader via loaded_filings.
"""
from __future__ import annotations

from typing import Callable

import duckdb

from src import config

# Migration 1 is the schema as of M1. Its CREATE ... IF NOT EXISTS statements also
# adopt databases built before migrations existed: the tables are already there,
# so applying it only records the version.
BASELINE_DDL = """
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
    value         DOUBLE  NOT NULL
);

-- Every (cik, accn) whose facts are fully in xbrl_facts. A filing's facts never change,
-- so a filing listed here is skipped on reload.
CREATE TABLE IF NOT EXISTS loaded_filings (
    cik   INTEGER NOT NULL,
    accn  VARCHAR NOT NULL,
    PRIMARY KEY (cik, accn)
);

-- Current snapshot, replaced each run. Tickers get reused, so CIK is the only
-- durable key; tickers are for display and lookup only.
CREATE TABLE IF NOT EXISTS tickers (
    cik         INTEGER NOT NULL,
    ticker      VARCHAR NOT NULL,
    name        VARCHAR,
    exchange    VARCHAR,
    fetched_on  DATE    NOT NULL,
    PRIMARY KEY (ticker)
);

CREATE TABLE IF NOT EXISTS companies (
    cik               INTEGER PRIMARY KEY,
    name              VARCHAR,
    sic               INTEGER,
    sic_description   VARCHAR,
    fiscal_year_end   VARCHAR,          -- 'MMDD'
    entity_type       VARCHAR,
    filer_category    VARCHAR,
    exchanges         VARCHAR[],
    tickers           VARCHAR[],
    is_financial      BOOLEAN NOT NULL, -- SIC 6000-6799: EV/EBIT meaningless
    is_shell          BOOLEAN NOT NULL  -- SIC 6770 blank checks / SPACs
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

CREATE TABLE IF NOT EXISTS person (
    cik        INTEGER PRIMARY KEY,
    canonical  VARCHAR NOT NULL UNIQUE,
    aliases    VARCHAR[] NOT NULL
);

CREATE TABLE IF NOT EXISTS person_issuer (
    cik         INTEGER NOT NULL,
    issuer_cik  INTEGER NOT NULL,
    PRIMARY KEY (cik, issuer_cik)
);
"""

# Append-only. Never edit a migration that has run on a real database; add a new
# (version, sql) pair instead. Each one runs once, inside its own transaction. A
# migration is SQL, or a function when it must inspect the database first.
# Migration 2: count facts per filing, so a later snapshot that adds facts to a known
# filing is noticed, and register filings loaded before loaded_filings existed (M0
# databases), which would otherwise be replayed into xbrl_facts' old primary key.
FILING_COUNTS_DDL = """
ALTER TABLE loaded_filings ADD COLUMN n_facts BIGINT;
INSERT INTO loaded_filings (cik, accn)
    SELECT DISTINCT f.cik, f.accn FROM xbrl_facts f
    ANTI JOIN loaded_filings l ON l.cik = f.cik AND l.accn = f.accn;
UPDATE loaded_filings SET n_facts = c.n
    FROM (SELECT cik, accn, count(*) AS n FROM xbrl_facts GROUP BY cik, accn) c
    WHERE loaded_filings.cik = c.cik AND loaded_filings.accn = c.accn;
"""

# Migration 3 (M2): prices lose their PRIMARY KEY (~17M rows; refreshes delete and
# re-insert a date range instead) and gain raw volume. Dividends and per-company
# price coverage get their own tables.
PRICES_DDL = """
CREATE TABLE prices_v3 (
    cik         INTEGER NOT NULL,
    ticker      VARCHAR NOT NULL,
    price_date  DATE    NOT NULL,
    close_raw   DOUBLE  NOT NULL,      -- as printed that day, on that day's share basis
    volume_raw  DOUBLE,                -- shares traded that day, unadjusted
    source      VARCHAR NOT NULL
);
INSERT INTO prices_v3 SELECT cik, ticker, price_date, close_raw, NULL, source FROM prices;
DROP TABLE prices;
ALTER TABLE prices_v3 RENAME TO prices;

CREATE TABLE IF NOT EXISTS dividends (
    cik         INTEGER NOT NULL,
    ex_date     DATE    NOT NULL,
    amount_raw  DOUBLE  NOT NULL,      -- per share, on that day's share basis
    source      VARCHAR NOT NULL,
    PRIMARY KEY (cik, ex_date)
);

-- One row per company we tried to price. status: 'ok' | 'no_data' (source has
-- nothing for the ticker, often delisted or renamed) | 'error'.
CREATE TABLE IF NOT EXISTS price_status (
    cik         INTEGER PRIMARY KEY,
    ticker      VARCHAR NOT NULL,
    status      VARCHAR NOT NULL,
    first_date  DATE,
    last_date   DATE,
    message     VARCHAR,
    fetched_at  TIMESTAMP NOT NULL
);
"""

# A filing's fingerprint: order-independent hash of its distinct fact rows. `frame`
# is left out on purpose: SEC attaches it to the most recently filed fact for a
# period, so it can move between filings without the facts changing, and nothing
# here reads it. Changing this expression marks every filing as changed once.
FILING_FINGERPRINT = """bit_xor(hash(cik, taxonomy, tag, unit, period_start, period_end, period_days,
                             period_kind, fy, fp, form, accn, filed, value))"""

XBRL_FACTS_V4_DDL = """
CREATE TABLE xbrl_facts_v4 (
    cik INTEGER NOT NULL, taxonomy VARCHAR NOT NULL, tag VARCHAR NOT NULL, unit VARCHAR NOT NULL,
    period_start DATE NOT NULL, period_end DATE NOT NULL, period_days INTEGER NOT NULL,
    period_kind VARCHAR NOT NULL, fy INTEGER, fp VARCHAR, form VARCHAR NOT NULL, accn VARCHAR NOT NULL,
    filed DATE NOT NULL, frame VARCHAR, value DOUBLE NOT NULL
);
INSERT INTO xbrl_facts_v4 SELECT * FROM xbrl_facts;
DROP TABLE xbrl_facts;
ALTER TABLE xbrl_facts_v4 RENAME TO xbrl_facts;
"""


def _filing_fingerprints(con: duckdb.DuckDBPyConnection) -> None:
    """Migration 4: fingerprint every loaded filing, so a changed SEC snapshot of a known
    filing replaces its rows. M0 databases still carry a PRIMARY KEY on xbrl_facts that
    such a replacement can violate; it is dropped by rebuilding the table."""
    has_pk = con.execute("""SELECT count(*) FROM duckdb_constraints()
                            WHERE table_name = 'xbrl_facts' AND constraint_type = 'PRIMARY KEY'""").fetchall()[0][0]
    if has_pk:
        con.execute(XBRL_FACTS_V4_DDL)
    con.execute("ALTER TABLE loaded_filings ADD COLUMN fingerprint UBIGINT")
    con.execute(f"""
        UPDATE loaded_filings SET fingerprint = f.fingerprint
        FROM (SELECT cik, accn, {FILING_FINGERPRINT} AS fingerprint FROM xbrl_facts GROUP BY cik, accn) f
        WHERE loaded_filings.cik = f.cik AND loaded_filings.accn = f.accn
    """)


Migration = str | Callable[[duckdb.DuckDBPyConnection], None]

MIGRATIONS: list[tuple[int, Migration]] = [
    (1, BASELINE_DDL),
    (2, FILING_COUNTS_DDL),
    (3, PRICES_DDL),
    (4, _filing_fingerprints),
]

# Macros are code, not data: recreated on every connect so edits always take effect.
MACROS = """
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


def migrate(con: duckdb.DuckDBPyConnection,
            migrations: list[tuple[int, Migration]] | None = None) -> list[int]:
    """Apply pending migrations in version order. Returns the versions applied."""
    migrations = MIGRATIONS if migrations is None else migrations
    con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT current_timestamp)""")
    applied = {r[0] for r in con.execute("SELECT version FROM schema_migrations").fetchall()}
    known = {v for v, _ in migrations}
    if applied - known:
        raise RuntimeError(f"database schema is at version {max(applied)} but this code only knows "
                           f"up to {max(known)}; update the code before opening this database")
    done = []
    for version, sql in sorted(migrations):
        if version in applied:
            continue
        con.begin()
        try:
            sql(con) if callable(sql) else con.execute(sql)
            con.execute("INSERT INTO schema_migrations (version) VALUES (?)", [version])
            con.commit()
        except BaseException:
            con.rollback()
            raise
        done.append(version)
    return done


def sync_concept_map(con: duckdb.DuckDBPyConnection) -> None:
    """Make concept_map equal CONCEPT_SEED. The seed in code is the source of truth,
    so changed priorities and removed tags reach existing databases too."""
    current = {tuple(r) for r in con.execute("SELECT * FROM concept_map").fetchall()}
    if current == set(CONCEPT_SEED):
        return
    con.begin()
    try:
        con.execute("DELETE FROM concept_map")
        con.executemany("INSERT INTO concept_map VALUES (?, ?, ?, ?, ?)", CONCEPT_SEED)
        con.commit()
    except BaseException:
        con.rollback()
        raise


def connect(path: str = str(config.DB_PATH)) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path, config={
        "memory_limit": config.DB_MEMORY_LIMIT,
        "max_temp_directory_size": config.DB_TEMP_MAX,
        "threads": config.DB_THREADS,
    })
    migrate(con)
    con.execute(MACROS)
    sync_concept_map(con)
    return con
