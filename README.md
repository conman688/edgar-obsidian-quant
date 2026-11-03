# edgar-obsidian-quant

Local-first equity research pipeline. Screens SEC filings for cheap, out-of-favour
companies with insider buying, and writes linked notes into an Obsidian vault.
No paid data or LLM API required.

## Setup

```bash
uv sync
uv run pytest
```

## Layout

| Path | Role |
|---|---|
| `src/db/schema.py` | DuckDB tables: immutable XBRL facts, splits, prices, Form 4 rows, run ledger |
| `src/db/pit.py` | Point-in-time resolution (`filed <= as_of`), tag fallback ladder, TTM |
| `src/db/queries.py` | Screens: share-count trend, valuation, cluster buys, insider holdings |
| `src/ingestion/companyfacts.py` | Loads SEC `companyfacts.zip` / API JSON |
| `src/ingestion/form4_insider.py` | Form 4 XML parser and rate-limited SEC client |
| `src/llm/schemas.py` | Optional quote-anchored extraction guard (only if an LLM is added) |
| `src/obsidian/writer.py` | Atomic note writer, AUTO-block merge, CIK-keyed person registry |
| `tests/test_failure_modes.py` | One test per audited failure mode |

## Invariants

- Every screen sees only data with `filed <= as_of`. No look-ahead.
- Fact rows are never updated. Restatements are new rows.
- Market cap = raw close on `as_of` x cover-page shares. Historical share trends are split-adjusted.
- Conviction buys = non-derivative, code `P`, acquired `A`, price > 0, not a 10b5-1 plan.
- Notes are written atomically. Only the `AUTO-GENERATED` block and auto frontmatter keys change.

## SEC access

SEC requires a `User-Agent` with a real contact email and caps traffic at 10 req/s.
`SecClient` enforces 8 req/s.

## Status

Done: schema, PIT layer, screens, Form 4 parser, Obsidian writer, tests (synthetic data).
Not done: CLI, price loader, split loader, live SEC run, YoY footnote redline, Jinja templates.
