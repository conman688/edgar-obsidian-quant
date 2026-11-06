# Research R1: repositories suggested by GitHub's AI
Status: planned (start 2026-11-06)

The user asked GitHub's AI to look at this repository and suggest projects to study. Claude and
Codex investigate each one and decide which, if any, the project should adopt. This is research:
nothing is merged into `main` before the M9 two-week check ends (about 2026-11-19), and any
adoption becomes its own milestone with the usual review.

## The suggestions

| # | Repository | GitHub AI's reason (paraphrased) | Already in the project |
|---|---|---|---|
| 1 | dgunning/edgartools | SEC/XBRL parsing: footnote text blocks, DEF 14A pay, filing retrieval | own `src/ingestion/` (companyfacts, submissions, Form 4, 10-K text) |
| 2 | jadchaar/sec-edgar-downloader | Filing download | `SecClient` (rate limit, conditional GET, retries) |
| 3 | jadchaar/sec-cik-mapper | Ticker/CIK mapping | `tickers` from SEC's company_tickers_exchange.json |
| 4 | bagoldbe/form4lab | Form 4 insider-signal definitions, plan-trade exclusions, strategy evaluation | M3 conviction buys, 10b5-1 flag, M5 backtest |
| 5 | DMulajkar/Quantgress | Resumable DuckDB ingestion; alternative data (13F, congressional trades, lobbying) | DuckDB ledgers, migrations |
| 6 | ranaroussi/yfinance | Market data ("keep it") | yes, the price source |
| 7 | kernc/backtesting.py | Backtesting | own point-in-time backtest (M5) |
| 8 | pmorissette/bt | Backtesting | own point-in-time backtest (M5) |
| 9 | blacksmithgu/obsidian-dataview | Dashboard queries | yes, Dashboard.md |
| 10 | obsidianmd/obsidian-api | Plugin/API integration | none (notes are plain files) |
| 11 | obsidian-tasks-group/obsidian-tasks | Review workflows | none (journal `review_date` + Dataview) |

GitHub's AI suggested this order: SEC/XBRL parsing, then footnote extraction, Form 4 validation,
alternative datasets, and Obsidian workflow. It warned that one backtesting project is AGPL
(check which) and that MIT/Apache-2.0 code is safer to reuse. These are its claims; verify
each.

## Questions for every repository

1. **Fit:** which gap does it close? The gaps: M7's deferred footnotes (going-concern,
   related-party and debt default first) and executive pay; Form 4 checks; alternative data;
   journal reviews. Or does it duplicate code we already have and test?
2. **Invariants** (AGENTS.md): can it work point-in-time (`filed <= as_of`, no restated values
   leaking back)? Does it fit idempotent loads with ledgers, raw prices with split factors, 2 GB
   DuckDB RAM, the one-process database, the tight disk, and SEC's 10 requests per second with a
   User-Agent?
3. **Cost:** licence (MIT/Apache/BSD reuse; GPL/AGPL only as a separate tool or for ideas),
   dependency weight, maintenance (recent commits, open issues, releases), $0/month.
4. **Proof:** on a sample (the vault's companies, or DINO, GIII, LRN), does it give the same
   numbers as our pipeline, or better ones? Report the numbers.
5. **Verdict:** adopt as a dependency / borrow an idea or test case / reject, with the reason.

## How we work

- Claude and Codex each write an independent verdict per repository below, without reading the
  other's first. Then they compare, and disagreements go to the user.
- Read the code, licence and tests, not only the README. Install only in a throwaway virtual
  environment outside the repository. Never point a candidate at `data/edgar.duckdb` or the
  vault. Run any SEC requests through the project's User-Agent at a low rate.
- Suggested split for a first pass (each still reviews all): Claude 1-5 (SEC data, Form 4,
  DuckDB); Codex 6-11 (backtesting, Obsidian).

## Verdicts (Claude)

_Per repository: fit, invariants, cost, proof, verdict._

## Verdicts (Codex)

_Per repository: fit, invariants, cost, proof, verdict._

## Decision (user)

_Which to adopt, and in which milestone._
