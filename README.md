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

`ingest all` downloads the ticker map, `submissions.zip` (company profiles, SIC codes)
and `companyfacts.zip` (all XBRL financials). Re-running skips anything SEC has not
changed. Raw zips are deleted after loading unless `--keep-raw` is passed.

First full load (2026-11-02, 8 GB MacBook):

| Step | Download | Load | Result |
|---|---|---|---|
| tickers | 1 request | 4 s | 10,410 tickers |
| companies | 1.6 GB | 2.8 min | 75,242 company profiles |
| facts | 1.4 GB | 10 min | 17,087 companies, 123.6M facts, 424k filings |

Database size: 3.1 GB. Peak free disk needed during load: about 5 GB (zip + database).

DuckDB is capped at 2 GB RAM and 4 GB of spill files (`EOQ_DB_MEMORY`, `EOQ_DB_TEMP_MAX`).
Uncapped, a heavy query on an 8 GB machine with little free disk can crash the computer.

## Layout

| Path | Role |
|---|---|
| `src/db/schema.py` | DuckDB tables: immutable XBRL facts, splits, prices, Form 4 rows, run ledger |
| `src/db/pit.py` | Point-in-time resolution (`filed <= as_of`), tag fallback ladder, TTM |
| `src/db/queries.py` | Screens: share-count trend, valuation, cluster buys, insider holdings |
| `src/config.py` | Data paths, SEC URLs, `SEC_USER_AGENT` |
| `src/cli.py` | `ingest`, `facts`, `stats` commands |
| `src/db/ledger.py` | Run ledger: skip finished work, resume after crashes |
| `src/ingestion/sec.py` | Rate-limited SEC client, streaming conditional download |
| `src/ingestion/companyfacts.py` | Loads SEC `companyfacts.zip` into `xbrl_facts`, per-filing idempotent |
| `src/ingestion/reference.py` | Ticker map and company profiles (SIC, fiscal year end) |
| `src/ingestion/form4_insider.py` | Form 4 XML parser |
| `src/llm/schemas.py` | Optional quote-anchored extraction guard (only if an LLM is added) |
| `src/obsidian/writer.py` | Atomic note writer, AUTO-block merge, CIK-keyed person registry |
| `tests/test_failure_modes.py` | One test per audited failure mode |
| `tests/test_ingest.py` | Bulk load idempotency, crash resume, reference data, conditional download |

## Invariants

- Every screen sees only data with `filed <= as_of`. No look-ahead.
- Schema changes are append-only migrations in `src/db/schema.py`. `connect()` applies pending ones; it refuses a database newer than the code.
- Fact rows are never updated. Restatements are new rows.
- Market cap = raw close on `as_of` x cover-page shares. Historical share trends are split-adjusted.
- Conviction buys = non-derivative, code `P`, acquired `A`, price > 0, not a 10b5-1 plan.
- Notes are written atomically. Only the `AUTO-GENERATED` block and auto frontmatter keys change.

## SEC access

SEC requires a `User-Agent` with a real contact email and caps traffic at 10 req/s.
`SecClient` enforces 8 req/s.

## Status

Done: schema, PIT layer, screens, Form 4 parser, Obsidian writer, M1 ingestion (live SEC data), CLI.
Not done: price loader, split loader, Form 4 ingestion, YoY footnote redline, Jinja templates.

See [ROADMAP.md](ROADMAP.md) for goals, milestones and acceptance checks.
