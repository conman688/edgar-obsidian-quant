# edgar-obsidian-quant

Local-first equity research pipeline. Screens SEC filings for cheap, out-of-favour
companies with insider buying, and writes linked notes into an Obsidian vault.
No paid data or LLM API required.

## Setup

```bash
uv sync
uv run pytest
```

## Load SEC data

```bash
cp .env.example .env        # then put your name and email in SEC_USER_AGENT
uv run --env-file .env python -m src.cli ingest all
uv run python -m src.cli stats
uv run python -m src.cli facts AAPL revenue --as-of 2024-06-30
```

`ingest all` downloads the ticker map, `submissions.zip` (company profiles, SIC codes),
`companyfacts.zip` (all XBRL financials) and daily prices. Re-running skips anything SEC
has not changed. Raw zips are deleted after loading unless `--keep-raw` is passed.

`ingest prices` loads daily closes, volume, splits and dividends from Yahoo Finance
(via yfinance, unofficial) for non-financial NYSE and Nasdaq companies from 2010
(`--include-otc` adds OTC names, `--ticker` limits to given tickers). Yahoo adjusts
history for later splits; the loader stores raw values as printed on the day, so they
line up with as-filed share counts. Re-running fetches only the last week onward.

`ingest form4` loads insider trades (Form 4 and 4/A, stock rows only). Quarters SEC has
published as Insider Transactions Data Sets load from one zip each, for every issuer.
SEC publishes them a quarter or more late, so the rest come from the quarterly form index:
each filing's XML is fetched, for priced companies only. A per-filing ledger keeps the two
paths from loading a filing twice, and both produce identical rows for the same filing.
Default range is two years back (`--since 2024q4` to change). The backtest needs history from
2011q4: `ingest form4 --since 2011q4` loads 52 more quarters (5.9M rows, +1.2 GB of database,
about 10 minutes). Data sets before 2023 have no 10b5-1 checkbox; only plan footnotes mark those.
Amendments (4/A) replace the filing they amend only from the day they were filed.

## Screen

```bash
uv run python -m src.cli screen --as-of 2026-11-02            # default filters
uv run python -m src.cli screen --min-drawdown 0.3 --max-ev-ebit 10 --trace
```

Ranks priced, non-financial companies on five filters: drawdown from the 52-week high
> 50%, trailing FCF > 0, EV/EBIT < 6, net debt <= 25% of market cap, and a cluster buy by
insiders known within 90 days. Every input is point-in-time. Each company gets a status:

- `shortlist`: passes every filter on clean data. Ranked by EV/EBIT, cheapest first.
- `quarantine`: no filter fails, but an input is missing or suspect (`issues`). Never ranked.
- `fail`: some filter fails (`failed_filters` says which).

Issues: `missing_*` / `stale_*` (shares, cash, debt, balance sheet, financials older than
200 days or older than the newest balance-sheet item), `fcf_period_mismatch` (cash flow and
capex from different periods), `mixed_ebit_tags` / `mixed_fcf_tags` (a TTM bridge mixing
ladder tags), `debt_sources_disagree` (total and current + noncurrent differ by over 10%;
the larger is used), `stale_debt` also when a debt line over 5% of debt, reported in the
last 400 days, is missing from the newest balance sheet, `share_jump` (>40% share change in a
year: missed split or M&A), `foreign_filer` (20-F/40-F: ADS price vs ordinary shares),
`stale_price`. Flags, shown but not blocking: `multi_class_risk` (no cover-page share
count, so a class count or nothing), `ebit_is_pretax` (pre-tax income stands in for EBIT).

The terminal shows the shortlist and quarantine; `--trace` prints the tag, period and
accession numbers behind each number. Every company, with all metrics, issues, sources and
Form 4 accessions, is written to `data/screens/screen-<as_of>.parquet`; the filters used are
in the file metadata. A full screen takes about 9 s.

BACKTEST_SECTION

## First full load

First full load (2026-11-02, 8 GB MacBook):

| Step | Download | Load | Result |
|---|---|---|---|
| tickers | 1 request | 4 s | 10,410 tickers |
| companies | 1.6 GB | 2.8 min | 75,242 company profiles |
| facts | 1.4 GB | 10 min | 17,087 companies, 123.6M facts, 424k filings |
| prices (first) | Yahoo, 4,184 tickers | 6.1 min | 11.2M daily rows from 2010, 2,823 splits |
| prices (refresh) | last week only | 3.4 min | network-bound: one request per ticker |

Database size: 3.1 GB with prices. Peak free disk needed during load: about 5 GB (zip + database).

DuckDB is capped at 2 GB RAM and 4 GB of spill files (`EOQ_DB_MEMORY`, `EOQ_DB_TEMP_MAX`).
Uncapped, a heavy query on an 8 GB machine with little free disk can crash the computer.

## Layout

| Path | Role |
|---|---|
| `src/db/schema.py` | DuckDB tables: immutable XBRL facts, splits, prices, Form 4 rows, run ledger |
| `src/db/pit.py` | Point-in-time resolution (`filed <= as_of`), tag fallback ladder, TTM |
| `src/db/queries.py` | Screens: share-count trend, valuation, cluster buys, insider holdings |
| `src/screen.py` | `screen`: joins the screens into one ranked, traceable table; Parquet snapshot |
| `src/backtest.py` | Monthly point-in-time backtest, snapshot cache, forward returns, leak check, report |
| `src/config.py` | Data paths, SEC URLs, `SEC_USER_AGENT` |
| `src/cli.py` | `ingest`, `facts`, `stats` commands |
| `src/db/ledger.py` | Run ledger: skip finished work, resume after crashes |
| `src/ingestion/sec.py` | Rate-limited SEC client, streaming conditional download |
| `src/ingestion/companyfacts.py` | Loads SEC `companyfacts.zip` into `xbrl_facts`, per-filing idempotent |
| `src/ingestion/reference.py` | Ticker map and company profiles (SIC, fiscal year end) |
| `src/ingestion/prices.py` | Yahoo daily prices, splits, dividends; raw-price rebuild, coverage tracking |
| `src/ingestion/form4_insider.py` | Form 4 XML parser |
| `src/ingestion/form4_sources.py` | Form 4 bulk data sets, form index, per-filing XML fetch, shared ledger |
| `src/llm/schemas.py` | Optional quote-anchored extraction guard (only if an LLM is added) |
| `src/obsidian/writer.py` | Atomic note writer, AUTO-block merge, CIK-keyed person registry |
| `tests/test_failure_modes.py` | One test per audited failure mode |
| `tests/test_ingest.py` | Bulk load idempotency, crash resume, reference data, conditional download |
| `tests/test_ingest_recovery.py` | 304 after failed or filtered loads, M0 upgrade, facts added to known filings |
| `tests/test_schema.py` | Migrations: fresh, legacy adoption, new, failing, newer-than-code |
| `tests/test_prices.py` | Split undo, refresh idempotency, primary-ticker choice, failed batches |
| `tests/test_review_fixes.py` | Price split direction, filing order before tag priority |
| `tests/test_m2_review.py` | Price refresh windows, corporate actions, failure status, filing replacement |
| `tests/test_form4_sources.py` | Data set vs XML parity on a real filing, ledger, amendments, identical lots |
| `tests/test_m4_screen.py` | Debt source by date, stale items, EBIT/capex ladders, typo dates, screen status and trace |
| `tests/test_m5_backtest.py` | Split/dividend returns, delisted exits, statistics, costs, halves, leaky policy, cache, report |

## Invariants

- Every screen sees only data with `filed <= as_of`. No look-ahead.
- A fact whose period ends more than 7 days after its filing date, or after `as_of`, is ignored.
- Balance-sheet items are compared to the newest one; an older item is stale, never current.
- Schema changes are append-only migrations in `src/db/schema.py`. `connect()` applies pending ones; it refuses a database newer than the code.
- Restatements are new rows under a new filing; earlier filings are never overwritten by them.
  A filing's rows are replaced only when SEC's snapshot of that same filing changes (fingerprint).
- A price refresh replaces prices, splits and dividends only inside windows that returned data.
- Market cap = raw close on `as_of` x cover-page shares. Historical share trends are split-adjusted.
- Conviction buys = non-derivative, code `P`, acquired `A`, price > 0, not a 10b5-1 plan.
- Notes are written atomically. Only the `AUTO-GENERATED` block and auto frontmatter keys change.

## SEC access

SEC requires a `User-Agent` with a real contact email and caps traffic at 10 req/s.
`SecClient` enforces 8 req/s.

## Status

Done: schema with migrations, PIT layer, screens, Obsidian writer, M1 ingestion (live SEC data),
M2 prices/splits/dividends, M3 insider trades, M4 screener, M5 backtest, CLI.
Not done: vault sync, YoY footnote redline, Jinja templates.

See [ROADMAP.md](ROADMAP.md) for goals, milestones and acceptance checks.
