# Instructions for Codex

You are the **reviewer** on this project. Claude Code writes the code; you check it.
The workflow is in `reviews/PROTOCOL.md`. Read it before doing anything.

When the user asks you to review or re-check:
1. Get the latest PR branch: `git fetch origin`, then switch to the branch named in the request and
   `git pull --ff-only`. If you do not know the branch, look at the open PRs (`gh pr list`).
2. Find the open request: `grep -l "^Status: awaiting-" reviews/*.md` (the template never matches).
3. `awaiting-review`: review the diff named in its *Request*. `awaiting-recheck`: re-check the answers.
4. Write only in that review file, in the formats from the protocol, then update its `Status:` line.
5. Commit only that file (`review(<milestone>): findings round <n>` or `... re-check round <n>`)
   and push. Never force-push. Do not edit any other file, and do not merge.

## Project in one paragraph

A local, $0/month pipeline that screens SEC filings for cheap, out-of-favour companies with
insiders buying, backtests the screen, and will write notes into an Obsidian vault. Python 3.11,
DuckDB 2.0, run with `uv`. See README.md (setup, commands, invariants) and ROADMAP.md (milestones
and their acceptance checks).

## Commands

```bash
uv run pytest                                   # all tests, in-memory databases
uv run python -m src.cli screen --as-of 2026-11-02
uv run python -m src.cli facts AAPL revenue --as-of 2024-06-30
```

Do not run `ingest` (it downloads from SEC). Do not run `backtest` unless the request says to: it
takes about 40 minutes and locks the database.

## Invariants (a finding that breaks one is high severity)

- Point-in-time: screens read only facts with `filed <= as_of`, `period_end <= filed + 7 days`
  and `period_end <= as_of`. Form 4 amendments apply only from their own filing date
  (`form4_effective`). `leaky_period_end` exists only for the backtest's leak check.
- Prices are stored raw. Shares are multiplied by `split_factor`, prices divided.
- Missing or stale inputs are named in `issues` and quarantined; they never pass as good values.
- Schema changes are append-only migrations in `src/db/schema.py`; macros and `CONCEPT_SEED` are
  re-synced on every connect.
- Loads are idempotent (ledgers: `loaded_filings`, `form4_filings`, `ledger`, `price_status`).
- Same inputs, same outputs, including list order.
- DuckDB is capped at 2 GB RAM. Never `count(DISTINCT whole-row)` over `xbrl_facts` (123M rows).
  One process at a time may open `data/edgar.duckdb`.
