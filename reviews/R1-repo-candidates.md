# Research R1: repositories suggested by GitHub's AI
Status: in progress (Claude first pass 1-5 done 2026-11-06; Codex 6-11 pending)

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

First pass 2026-11-06, repositories 1-5. Candidates ran in a throwaway venv outside the repo
(Python 3.14), with the project's User-Agent and their own caches in a scratch folder. Our
numbers came from read-only queries on `data/edgar.duckdb` while no run held the lock; no
candidate touched the database or the vault. Sample: DINO (CIK 1915657), GIII (821002),
LRN (1157408); Form 4 window: filed 2025-01-01 to 2026-11-05.

### Summary

| # | Repository | Verdict | One-line reason |
|---|---|---|---|
| 1 | edgartools | **borrow the idea** (adopt only if our own parser proves hard) | Same numbers as ours; its useful part for us is reading 10-K text blocks, which ~10 lines of stdlib also do |
| 2 | sec-edgar-downloader | **reject** | Duplicates `SecClient`; downloads whole 13-18 MB submissions with no ledger |
| 3 | sec-cik-mapper | **reject** | Archived (last release 2022), same SEC file as our `tickers`, fake User-Agent |
| 4 | form4lab | **borrow an idea** (routine-buyer flag), not code | Our Form 4 handling is stricter; its routine/opportunistic idea is cheap to test |
| 5 | Quantgress | **reject as code; note two ideas** (13F, `ecd:` frames for pay) | Script collection, weaker point-in-time handling, author's e-mail as User-Agent |

### 1. dgunning/edgartools

- **Fit:** closes the M7 footnote gap: it reads XBRL text blocks from a 10-K. The rest
  (company facts, filings lists, Form 4 parsing, financial statements) duplicates our tested code.
- **Invariants:** each fact comes from one filing with a known filing date, so footnotes can be
  stored point-in-time (`filed <= as_of`). It keeps its own disk cache (6.6 MB for this test;
  needs `EDGAR_LOCAL_DATA_DIR` set or it writes to `~/.edgar`) and its own rate limiter, separate
  from ours: two limiters in one run could exceed SEC's 10 requests/second together.
- **Cost:** MIT. Very active (v5.62.1 on 2026-10-23, pushed 2026-11-07, 2,838 stars, 106 open
  issues). Heavy: 22 direct dependencies (pandas, httpx, rich, lxml, rapidfuzz, ...), 24 MB
  package, 354 MB venv in all; we use 8. Fast-moving API (major version 5).
- **Proof (all three match our database):**

  | | DINO | GIII | LRN |
  |---|---|---|---|
  | Latest 10-K accession | 0001915657-26-000016 (same) | 0001104659-26-033891 (same) | 0001104659-26-090515 (same) |
  | Revenue, latest FY | $26,869M (same) | $2,957M (`...IncludingAssessedTax`, a tag we map) | $2,518.081M (same) |
  | Net income | $579M ours (`NetIncomeLoss`); it showed $586M `ProfitLoss` (includes minority interest, a different tag) | $67.353M (same) | $338.192M (same) |
  | Form 4 filings | 51 (same) | 47 (same) | 68 = our 68 in `form4_filings`; 59 have stock rows, 9 are option-only (we store only non-derivative rows, by design) |
  | Open-market buys (P/A) | 6 rows, 3 insiders, $3,722,465 vs ours $3,722,450 | 3 rows, 3 insiders, $1,719,561 (same) | 15 rows, 2 insiders, $31,483,168 (same) |

  DINO's $15 gap: SEC's bulk Form 4 data set (our main source) rounds prices to cents
  (35.66 vs 35.6628 in the XML). Effect 0.0004%; not worth a change.
  Text blocks in the latest 10-K: DINO 89, GIII 68, LRN 63. Found: debt and commitments for all
  three, related-party for GIII and LRN, going-concern for none (expected for healthy companies:
  absence must be stored as "none filed", not as missing data).
- **Same result without it:** a stdlib regex over the 10-K's XBRL instance (`*_htm.xml`) found
  the same debt, commitments and related-party blocks for all three. So footnotes need one more
  file per 10-K through our own `SecClient`, not a new dependency.
- **Verdict: borrow the idea.** Build footnote text blocks in our code (instance file per 10-K,
  vault companies only, ledger in `filing_sections` style). Keep edgartools as a test oracle in a
  throwaway venv. Reconsider adoption only if inline-XBRL edge cases make our parser costly.

### 2. jadchaar/sec-edgar-downloader

- **Fit:** none new. Downloads whole filings; no parsing. `SecClient` already fetches what we need
  with rate limiting, conditional GET and retries.
- **Invariants:** no ledger, no idempotent load; writes full submissions to disk (DINO 17.6 MB,
  GIII 15.1 MB, LRN 13.5 MB per 10-K) where disk is tight. Rate limiter (pyrate-limiter) and a
  real User-Agent are fine.
- **Cost:** MIT, maintained (5.1.0, 2026-02-02), light.
- **Proof:** latest 10-K for all three in 1.6 s; accession numbers equal ours.
- **Verdict: reject.** Duplicates our client and costs disk.

### 3. jadchaar/sec-cik-mapper

- **Fit:** none. It reads `company_tickers_exchange.json`, the same file our `tickers` table
  snapshots each run.
- **Invariants:** its User-Agent is a made-up `<unix time> <unix time>@gmail.com`, against SEC's
  fair-access rule. I did not run it for that reason.
- **Cost:** MIT, but archived; last release 2.1.0 on 2022-01-09.
- **Proof:** source read only (`retrievers.py`, `BaseMapper.py`).
- **Verdict: reject.**

### 4. bagoldbe/form4lab

- **Fit:** Form 4 signals and a backtester. We already have conviction buys (P + A), the 10b5-1
  flag, amendments effective from their own filing date, and the M5 point-in-time backtest.
- **Invariants:** weaker than ours on Form 4. It keeps only form `4` (no 4/A handling); its 10b5-1
  flag reads only the checkbox (`aff10b5One`, 2023 onward), while ours also reads footnotes; it
  collapses same-day lots into one event. Rate limit 9/s with a required real User-Agent: good.
  SQLite/Postgres via SQLAlchemy, Alpaca trading module: does not fit our DuckDB design.
- **Cost:** Apache-2.0; created 2026-07-09, one commit, 8 stars, no releases ("pre-release"). Many
  tests (39 files), but young.
- **Idea worth testing:** routine vs opportunistic insiders (Cohen, Malloy and Pomorski 2012):
  a buy is "routine" if the same insider bought in the same calendar month in each of the 3 prior
  years. On our data (read-only, `filed <= ` each buy's filing date): of 29,245 insider buy-days
  filed since 2025-01-01, 1,630 (5.6%) are routine. None of the shortlist's 17 buy-days is
  routine (DINO's Franklin Myers, 13 prior buys, matched only 1 of 3 prior years). So the flag
  would not change today's shortlist, and its value is unproven until the M5 backtest compares
  the screen with and without it.
- **Verdict: borrow the idea, not the code.** A `routine_buyer` flag plus a backtest comparison,
  as a small later milestone if the user wants it.

### 5. DMulajkar/Quantgress

- **Fit:** alternative data (congress trades, 13F, lobbying, short volume, ...). Its "resumable
  DuckDB ingestion" is skip-if-stored by key, which our ledgers already do with more care.
- **Invariants:** keeps Form 4/A as separate rows "as filed", with no effective-date logic; dates
  stored as text. User-Agent is hard-coded to the author's e-mail (would have to change). Its
  repository commits a 20 MB DuckDB file, including an `api_keys` table (hashed keys with users'
  e-mails), a maintenance warning sign. Optional dependencies include torch (~2 GB).
- **Cost:** MIT; created 2026-08-14, one commit, 8 stars, no releases; scripts with inline
  self-tests rather than a test suite.
- **Proof:** its committed database has 6,923 Form 4 rows filed 2026-06-18 to 2026-08-13 for
  1,065 issuers, none for DINO, GIII or LRN. Nothing to compare.
- **Two ideas noted:** (a) 13F holdings from SEC's quarterly data sets (fund ownership), if
  alternative data is ever wanted; (b) executive pay from SEC's `frames` API for `ecd:`
  pay-versus-performance tags: one request per tag per year covers every company. That is the
  cheapest route if the user ever wants the deferred executive-pay item.
- **Verdict: reject as code.**

## Verdicts (Codex)

_Per repository: fit, invariants, cost, proof, verdict._

## Decision (user)

_Which to adopt, and in which milestone._
