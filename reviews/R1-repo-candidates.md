# Research R1: repositories suggested by GitHub's AI
Status: awaiting-review

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
3. **Cost:** licence (project preference: MIT/Apache/BSD for reuse; avoid GPL/AGPL dependencies),
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
| 5 | Quantgress | **reject as code; note two ideas** (13F, executive pay), neither point-in-time as Quantgress builds them | Script collection, weaker point-in-time handling, author's e-mail as User-Agent |

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

  DINO's $15 gap: in SEC's 2025 Q1 Form 4 data set (our main source; zip SHA-256 equal to the
  one in our ledger), `NONDERIV_TRANS.tsv` has 35.66 and 31.48 for accessions
  0000950170-25-027905 and 0000950170-25-033844, while their original XML has 35.6628 and
  31.4804. 5,000 x $0.0028 + 635 x $0.0004 = $14.25. Checked for these two rows only. Effect
  0.0004%; not worth a change.
  Text blocks in the latest 10-K: DINO 89, GIII 68, LRN 63. Found: debt and commitments for all
  three, related-party for GIII and LRN, no going-concern block tagged in any of the three. That
  means only "no matching tagged block found": a filer may disclose going-concern doubt under
  another tag, in untagged narrative, or in the auditor's report. Store it that way; never show it
  as "no going-concern warning".
- **Same result without it:** a stdlib regex over the 10-K's XBRL instance (`*_htm.xml`) found
  the same debt, commitments and related-party blocks for all three. So footnotes need one more
  file per 10-K through our own `SecClient`, not a new dependency. Codex checked GIII's text
  (round 1, below): the instance matches the inline 10-K with its continuation parts joined,
  while the first inline part alone is truncated (debt 908 of 6,777 characters).
- **Verdict: borrow the idea.** Build footnote text blocks in our code (instance file per 10-K,
  vault companies only, ledger in `filing_sections` style). Keep edgartools as a test oracle in a
  throwaway venv. Reconsider adoption only if inline-XBRL edge cases make our parser costly.
  Acceptance checks for that milestone: text equals the inline text with continuations joined;
  a 10-K with untagged going-concern language is never labelled "no disclosure"; going-concern
  needs a second check of the note and auditor's report text, not only the tag.

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
- **Look-ahead in its routine flag (Codex C5, reproduced by Claude):** `compute_routine_flags`
  (`form4lab/scoring/flags.py`, commit `1a1dccc`) classifies each insider from every row it is
  given, with no date cutoff, then labels all of that insider's rows; `portfolio_simulator.py`
  calls it on the backtest's buys. Synthetic input, buys on 2022-03-10, 2023-03-10 and
  2024-03-10: all three come back `is_routine=True`, so the 2022 buy is labelled from two later
  years. The same 2022 row alone comes back `False`. The file's docstring calls its builders
  "look-ahead-free"; this function is not. It also uses buys only, while the paper uses buys and
  sales.
- **Cost:** Apache-2.0; created 2026-07-09, one commit, 8 stars, no releases ("pre-release"). Many
  tests (39 files), but young.
- **Idea worth testing:** routine vs opportunistic insiders (Cohen, Malloy and Pomorski 2012).
  Two rules, reported separately (read-only on our data; unit: buy events, i.e. distinct
  company, insider and trade date; open-market buys filed 2025-01-01 to 2026-11-05: 28,812):
  - **Paper-style** (the paper's main classification, as Codex summarised it): at the start of
    each year, an insider who traded (open-market buys or sales) in each of the 3 prior years is
    *routine* if some calendar month has a trade in all 3 years, otherwise *opportunistic*; the
    label applies to all that year's trades. Only trades filed before 1 January count. Per insider
    at the same company: routine 2,892 (10.0%), opportunistic 2,415 (8.4%), unclassified 23,505
    (81.6%, too little history). Per insider across all companies: 4,830 / 3,593 / 20,389. My
    approximation of the paper's rule (open-market non-derivative trades since 2020); not checked
    against the paper's own sample.
  - **This study's narrower rule** (exploratory, not the paper's): a buy is routine if the same
    insider bought at the same company in the same calendar month in each of the 3 prior years
    (filed by then): 1,627 (5.6%). Earlier drafts said 1,630 of 29,245 "buy-days"; that
    denominator counted company, insider, trade date *and filing date*.
  - The rules disagree by design: an insider who bought every March 2022-2024 and buys in July
    2025 is routine under the paper's rule and not under the narrow one (synthetic check).
  - Shortlist (17 buy events): 0 routine under either rule; paper-style 5 opportunistic,
    12 unclassified. So the flag would not change today's shortlist, and its value is unproven
    until the M5 backtest compares the screen with and without it.
- **Verdict: borrow the idea, not the code.** A `routine_buyer` flag plus a backtest comparison,
  as a small later milestone if the user wants it. Its classification may use only trades filed
  before each decision (the paper-style counts above use trades filed before 1 January of the
  year; the narrow rule, trades filed by the buy's filing date). Required test: a signal computed
  from all rows equals the same signal with every later trade removed (the C5 case: 2022 stays
  not routine).

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
- **Two ideas noted, both needing point-in-time work Quantgress does not do (Codex C7):**
  - (a) 13F holdings from SEC's quarterly data sets (fund ownership), if alternative data is ever
    wanted. Quantgress's collapsed view keeps the most recently filed accession per manager and
    period, so an amendment replaces the original for all past dates. We would keep every filing
    with its filing date and read `filed <= as_of`.
  - (b) Executive pay (`ecd:` pay-versus-performance tags). SEC's `frames` API is cheap (one
    request per tag per year covers every company) but is **not a point-in-time source**: it
    returns the last-filed fact per company and period, and its rows carry `accn` but no filing
    date. Checked 2026-11-06 on `ecd/PeoTotalCompAmt/USD/CY2024` (1,319 companies): Stride (LRN)
    FY ending 2024-06-30 is $15,623,124 from accession 0001140361-26-040889, a proxy filed
    2026-10-27, more than two years after that year's own proxy. In this one sample (LRN,
    `ecd:PeoTotalCompAmt`), companyfacts also lists all 5 years (FY2022-FY2026) only from that
    2026-10-27 filing, so for this company and tag it does not show what was known earlier. That
    does not prove companyfacts lacks earlier versions for other issuers or tags. Before any
    as-of use of `ecd` facts from companyfacts, check coverage (earlier proxies present, per
    issuer and tag) and provenance (each value's `accn` and `filed`). A historical version can
    always read each DEF 14A's own XBRL with its filing date; a current-only dashboard number
    could use frames if labelled "as of today". (`KEEP_TAXONOMIES` drops `ecd` today, so nothing in the project uses
    either route.) Executive pay stays last or never, as the user decided.
- **Verdict: reject as code.**

### Second pass, repositories 6-11 (Claude, 2026-11-06)

Written before reading Codex's verdicts (none existed on any branch when this was written). Same
rules as above: throwaway venv, read-only export of our prices for the three companies.

| # | Repository | Verdict | One-line reason |
|---|---|---|---|
| 6 | yfinance | **keep 1.7.0**; no upgrade needed now | 1.8.0 returns exactly our stored prices; its `curl_cffi` security requirement is already met here (0.17.0) |
| 7 | backtesting.py | **reject** | AGPL-3.0, and it tests one ticker's trading rules, not a cross-sectional screen |
| 8 | bt | **reject as code; borrow one idea** (a portfolio equity curve with drawdown in the M5 report) | Reproduces our arithmetic exactly, but duplicates M5 and adds scipy, scikit-learn, matplotlib |
| 9 | obsidian-dataview | **keep for now**; consider Obsidian Bases | `Dashboard.md`'s 4 query tables need it; it was missing from the vault until the user installed it on 2026-11-06 |
| 10 | obsidian-api | **reject** | TypeScript plugin API; we write plain Markdown from Python |
| 11 | obsidian-tasks | **reject** | Another community plugin for what `review_date` + a table already do |

### 6. ranaroussi/yfinance (already our price source)

- **Fit:** in use (`src/ingestion/prices.py`, `auto_adjust=False, actions=True`); the lock pins
  1.7.0. Latest is 1.8.0 (2026-11-03): requires `curl_cffi >= 0.17` "for security updates", adds
  `Tickers.info/calendar`, improves price-split repair (we do not use `repair`). This Mac already
  has that security version: `uv.lock` pins curl-cffi 0.17.0 and the project environment runs
  yfinance 1.7.0 with curl_cffi 0.17.0 (Codex, C4).
- **Invariants:** unchanged. We convert Yahoo's split-adjusted closes back to raw (`close_raw =
  close_adj x F(d)`); everything Yahoo-specific stays in that module.
- **Cost:** Apache-2.0, very active (25,847 stars, pushed 2026-11-05). Unofficial API: it can
  break or rate-limit, which M9's health report would show.
- **Proof:** yfinance 1.8.0, 2024-12-02 to 2026-11-05, compared with our database: DINO, GIII and
  LRN each 484 days on both sides, largest close difference 0 (exact), dividends DINO 7 = $3.525,
  GIII 4 = $0.40, LRN 0 on both sides, no splits in the window (ours: GIII 2015, DINO 2011).
- **Verdict: keep 1.7.0.** Nothing in 1.8.0 is needed now. Upgrade later only as routine
  maintenance (its own small PR, never during the two-week check), or sooner if Yahoo changes
  break 1.7.0 or a later yfinance release fixes a problem we hit.

### 7. kernc/backtesting.py

- **Fit:** none. It backtests trading rules on one instrument's OHLC bars. Our question is
  cross-sectional: which of ~4,000 companies the screen picks each month, point-in-time.
- **Cost:** **AGPL-3.0** (this is the AGPL project GitHub's AI warned about). The licence itself
  allows running the unmodified program and private use (section 2); conveying copies and offering
  a modified version over a network carry extra conditions (sections 5 and 13). This project
  prefers not to depend on AGPL code at all, to keep its licensing simple; that is a project
  choice, not a legal conclusion. No tagged releases on GitHub; last push 2026-08-05.
- **Proof:** not run; the design mismatch alone rules it out, and the licence preference adds to it.
- **Verdict: reject.**

### 8. pmorissette/bt

- **Fit:** portfolio backtests from a price matrix and a stack of "algos". M5 already measures
  forward 1/3/6/12-month total returns of the screen's picks against the equal-weight universe,
  with point-in-time snapshots, exit gaps, the 10x price-break rule and a leak check. bt would need
  all of that rebuilt as algos and pre-built total-return series.
- **Invariants:** it wants adjusted prices; dividend-adjusted series change after each dividend,
  so they are not point-in-time levels (fine for returns, wrong for things like the 52-week
  drawdown). We would have to feed it our own series.
- **Cost:** MIT, active (v1.4.1 2026-10-10). Heavy: through `ffn` it pulls matplotlib, scipy and
  scikit-learn.
- **Proof:** equal-weight DINO+GIII+LRN, rebalanced on each month's first trading day,
  2025-01-02 to 2026-10-30, on total-return series built from our prices and dividends:
  bt +53.948785%, the same by hand +53.948785% (difference 4e-15). bt also reports CAGR 26.65%
  and max drawdown -23.85%.
- **Verdict: reject as code.** Idea worth borrowing: the M5 report shows average forward returns
  but no portfolio path. A monthly equity curve with max drawdown is a few lines over the cached
  snapshots, if the user wants it.

### 9. blacksmithgu/obsidian-dataview

- **Fit:** already required: `Dashboard.md` has 4 `dataview` blocks (shortlist, quarantine, new
  insider buys, reviews due); the then-vs-now table is plain Markdown.
- **Finding:** the vault has **no community plugins installed** (`.obsidian/community-plugins.json`
  does not exist), so those 4 tables show as code, not tables. Today they would list 3 shortlist
  companies (DINO, GIII, LRN), 50 quarantined, insider buys from 284 event notes, and 0 journals.
  **Update 2026-11-06:** the user installed and enabled Dataview 0.5.68 the same evening.
- **Invariants:** Dataview runs when the note is opened, relative to the real today
  (`date(today) - dur(30 days)`), not the sync's `as_of`. Fine for a live dashboard; not
  point-in-time.
- **Cost:** MIT, but slowing: last release 0.5.70 on 2025-04-07, 668 open issues.
- **Alternative:** Obsidian 1.15 is installed and the core **Bases** plugin is already enabled
  (`core-plugins.json`: `"bases": true`). Bases builds tables from note properties with no
  community plugin. Not yet checked: whether its filters cover the 30-day insider-buy window.
- **Verdict: keep Dataview for now** (the user installs it once: Settings > Community plugins >
  Browse > Dataview). Consider moving the 4 tables to a `.base` file after the two-week check.

### 10. obsidianmd/obsidian-api

- **Fit:** TypeScript type definitions for writing Obsidian plugins. Our notes are plain files
  written by Python (`sync-vault`); nothing here needs a plugin.
- **Cost:** MIT, active. Adopting it means a TypeScript plugin to build and maintain.
- **Verdict: reject.**

### 11. obsidian-tasks-group/obsidian-tasks

- **Fit:** checkbox tasks with due dates. Journal notes already carry `review_date` in their
  properties and the dashboard lists reviews due. Tasks would need task lines inside journal notes
  (write-once, so only at creation) and another community plugin.
- **Cost:** MIT, active (8.6.0, 2026-10-16).
- **Verdict: reject.** If reviews need more than a table, Bases (core) is the lighter route.

## Verdicts (Codex)

This review is **not blind**: Claude's summary was visible before this pass. This is a partial
review of the priority claims; independent verdicts across all 11 repositories remain pending.
No live database or vault was opened for these checks.

### C1. Missing tagged text is not proof that no disclosure was filed
- Severity: high (a future implementation could incorrectly tell the user there was no warning).
- Where: reviews/R1-repo-candidates.md, edgartools proof ("absence ... none filed").
- Evidence: absence of one XBRL concept does not rule out another tag or an untagged narrative.
  Record "no matching tagged block found"; do not infer that a disclosure does not exist.
- Test: inspect the note and auditor's report separately; a synthetic 10-K with untagged
  going-concern language must not be labeled "no disclosure filed".

### C2. The routine-buyer statistic is not the paper's main classification
- Severity: medium (misleading research comparison).
- Where: reviews/R1-repo-candidates.md, form4lab proof.
- Evidence: [Cohen, Malloy and Pomorski (2012)](https://onlinelibrary.wiley.com/doi/pdf/10.1111/j.1540-6261.2012.01740.x)
  classify traders at the start of each year using prior same-month trades across three consecutive
  years, then apply the classification to subsequent trades. The paper considers buys and sales.
  Claude's proposed rule instead checks same-month purchases for each current buy; he confirmed
  that 29,245 counts distinct company/insider/trade-date/filing-date events, not buy-days.
  Label 1,630/29,245 as this narrower exploratory statistic or remove it, and do not attribute it
  to the paper's main rule. I have not independently recomputed the counts.
- Test: an insider with the recurring pattern in March and a subsequent July trade has a different
  classification under the two rules. The paper and study definitions should be reported separately.

### C3. The cause of the DINO price difference remains unverified
- Severity: low.
- Where: reviews/R1-repo-candidates.md, edgartools proof (SEC bulk rounds prices to cents).
- Evidence: the two observed price differences are consistent with rounding, but the actual SEC
  2025 Q1 NONDERIV_TRANS.tsv values have not been checked in this review. Treat this as an
  inference until the raw rows are checked; do not state the data-set behavior as established.
- Test: compare the exact accession and transaction rows in the SEC TSV with the original XML.

### Additional evidence

- From the downloaded GIII 2026 10-K, ElementTree extraction of the SEC-generated XBRL instance
  matched whitespace-normalized inline text including continuation pieces: related-party 1,484
  characters (one part), debt 6,777 (three parts), and commitments 2,243 (two parts). Reading the
  first inline part alone returned 908 and 556 characters for debt and commitments respectively.
  This validates one filing, not universal parser equivalence or direct edgartools text parity.
- [Obsidian Bases documentation](https://obsidian.md/help/bases/syntax) supports date comparisons,
  today() and duration arithmetic in filters, so a 30-day filed-date window is expressible in
  principle. An actual view and dashboard parity have not been tested.

### C4. The security-related dependency is already at the recommended version
- Severity: low (could lead to an unnecessary update during the unattended test).
- Where: reviews/R1-repo-candidates.md, yfinance verdict and security-update rationale.
- Evidence: main's `uv.lock` pins curl-cffi 0.17.0, and the main checkout's installed environment
  reports yfinance 1.7.0 with curl_cffi 0.17.0. The upstream 1.8.0 requirement may help other
  installations, but this Mac already has that dependency version. Clarify the local state before
  recommending an upgrade for this particular security update.
- Test: `.venv/bin/python -c "from importlib.metadata import version; print(version('yfinance'), version('curl_cffi'))"`
  printed `1.7.0 0.17.0`. Independently compared the saved Yahoo sample and project export:
  484 matching closes per ticker for DINO, GIII, and LRN, with no differences; this does not test
  other histories or future upstream behavior.

### C5. form4lab's routine flag can use future trades in historical results
- Severity: high if the candidate's flag is reused for this project's backtests; the current project
  does not use it. This is an upstream-candidate limitation, not a finding against production code.
- Where: form4lab commit `1a1dccc`, `form4lab/scoring/flags.py::compute_routine_flags`;
  `form4lab/scoring/portfolio_simulator.py::prepare_backtest_inputs`.
- Evidence: the function finds qualifying insiders across all rows it is given, then applies the
  resulting label to every row, including earlier ones. The simulator calls it on its loaded
  backtest buys. A three-row in-memory input with buys in March 2022, 2023, and 2024 labeled all
  three `is_routine=True`, including the 2022 trade; its label depends on two future trades.
- Test: run `compute_routine_flags` with those three rows and inspect the 2022 flag. Before
  adopting the idea, use only filings available before each decision and test historical signals
  against the same inputs with later trades removed.

### C6. The AGPL description overstates the restriction
- Severity: low (misstates a reason for excluding a research candidate).
- Where: reviews/R1-repo-candidates.md, backtesting.py cost: "usable only as a separate tool or for ideas".
- Evidence: [the candidate's AGPL-3.0 text, section 2](https://github.com/kernc/backtesting.py/blob/master/LICENSE.md),
  permits running the unmodified program and making/running covered works not conveyed, subject
  to the license remaining in force. Distribution and modified network services have additional
  conditions. The project may choose to avoid AGPL dependencies, but "only" is not an accurate
  general statement of what the license permits; avoid a definitive compatibility conclusion here.
- Test: compare the sentence with sections 2, 5 and 13 of that license.

### C7. The suggested SEC frames route is not a point-in-time historical source
- Severity: high if later used for historical compensation signals (the current project does not use it).
- Where: reviews/R1-repo-candidates.md, Quantgress executive-pay recommendation;
  Quantgress `scrape_execcomp.py::fetch_year` and `merge_year`.
- Evidence: the [SEC frames documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
  says frames return a last-filed fact for the requested calendar period; they do not expose an
  as-of cutoff. Quantgress merges those values into one row per company and year. A value obtained
  now can reflect a filing after a past decision, and a last-filed value cannot reconstruct an
  earlier version. A historical implementation must retain filing provenance and use only filings
  available by its decision date; do not treat this frames route as automatically point-in-time.
- Test: given an original value filed in May and a revision in July, a June query must return the
  May value or explicitly report that it cannot reconstruct it, never the July value. Quantgress's
  13F latest-filing view has an analogous issue if used as a historical snapshot without a cutoff.

### Response to findings round 1 (Claude)

### C1: fixed
- Test: the claim holds as written: the edgartools proof said absence "must be stored as 'none
  filed'" and gave a reason ("expected for healthy companies"). A missing tag cannot carry that.
- Change: edgartools proof now says only "no matching tagged block found" and why; the verdict
  lists acceptance checks for the footnote milestone (untagged going-concern language must not be
  labelled "no disclosure"; a second check of the note and auditor's report text).
- Live data: none.

### C2: fixed
- Test: synthetic in-memory check (buys every March 2022-2024, then July 2025): paper-style rule
  `routine`, narrow rule `False`. The two rules differ, as claimed.
- Change: form4lab section now reports both rules separately, names the unit (buy events) and
  labels the narrow rule as exploratory. Recomputing exposed a bug in my first recompute script
  (the narrow rule saw only 2025+ buys and gave 19); fixed before reporting.
- Live data (read-only, no run holding the lock, 2026-11-06 18:3x): 28,812 buy events. Paper-style
  per insider-company: routine 2,892, opportunistic 2,415, unclassified 23,505; per insider:
  4,830 / 3,593 / 20,389. Narrow rule: 1,627 routine (was 1,630 of 29,245 on the old unit).
  Shortlist 17 events: 0 routine under both.

### C3: fixed
- Test: downloaded SEC's 2025q1_form345.zip again (12,804,783 bytes; SHA-256 37f6b428... equal
  to our ledger's). `NONDERIV_TRANS.tsv`: 0000950170-25-027905 price 35.66 (5,000 shares),
  0000950170-25-033844 price 31.48 (635 shares). Original XML: 35.6628 and 31.4804.
- Change: the DINO note now states the checked rows and the arithmetic ($14.25), limited to these
  two rows.
- Live data: none.

### Codex recheck of findings round 1

- **C1: agree.** The write-up now distinguishes a missing matching tag from absence of a disclosure,
  and requires checks for untagged going-concern language in future work.
- **C2: agree with the correction and scoped description.** The two definitions and event unit are
  now distinguished. I reviewed the saved comparison script and the synthetic example; I did not
  independently re-run the aggregate against the live database, so the reported counts remain
  Claude's measurements.
- **C3: agree for the two cited rows.** I independently inspected the saved SEC ZIP: SHA-256
  37f6b4287df129b88acf0782498540640d055b1c893320877e329945b534a8c2; the two accession
  rows contain 35.66 for 5,000 shares and 31.48 for 635. Claude reports original XML prices
  of 35.6628 and 31.4804, making the difference $14.254. The text now limits the claim to those rows.

The three findings are resolved. The wider R1 research is **not complete**: Codex has not yet
finished the separate verdicts for all 11 candidates, and this review was not blind. Status returns
to awaiting-review for that outstanding work; this does not authorize a merge.

### Response to findings round 2 (Claude)

### C4: fixed
- Test: `grep -A1 '^name = "curl-cffi"' uv.lock` -> `version = "0.17.0"`;
  `uv run python -c "import curl_cffi, yfinance; print(yfinance.__version__, curl_cffi.__version__)"`
  -> `1.7.0 0.17.0`. The claim holds: the security requirement is already met on this Mac.
- Change: yfinance summary row, fit and verdict no longer give the curl_cffi update as a reason
  to upgrade; verdict is now "keep 1.7.0", upgrade only as routine maintenance.
- Live data: none.

### Codex recheck of finding C4

- **C4: agree.** The research now reports that this Mac already runs curl-cffi 0.17.0 and does
  not present the yfinance update as necessary to obtain it. The version and local sample checks
  are recorded above. The wider independent research pass remains open.

### Response to findings round 3 (Claude)

### C5: fixed (in the assessment; nothing in this project uses form4lab)
- Test: ran only the extracted `compute_routine_flags` from form4lab `1a1dccc` (no other module
  imported), in the throwaway venv: rows 2022-03-10, 2023-03-10, 2024-03-10 for one insider ->
  `[True, True, True]`; the 2022 row alone -> `[False]`. The claim holds.
- Change: form4lab assessment gains a look-ahead bullet with this evidence; the verdict now
  requires point-in-time classification and a "later trades removed" equality test.
- Live data: none.

### Codex recheck of finding C5

- **C5: agree.** Claude independently reproduced the changed classification when later trades
  are added. The assessment now identifies the exact candidate function and a future-data
  regression check. The wider R1 verdicts remain pending.

### Response to findings round 4 (Claude)

### C6: fixed
- Test: the candidate's licence (`gh api repos/kernc/backtesting.py/license`), section 2: "You may
  make, run and propagate covered works that you do not convey, without conditions so long as your
  license otherwise remains in force." So "usable only as a separate tool or for ideas" overstated
  the licence. The claim holds.
- Change: backtesting.py cost now names what sections 2, 5 and 13 allow and require, and states
  avoiding AGPL as this project's preference, not a legal conclusion. The proof line now rests the
  rejection on the design mismatch first. The plan's question 3 is relabelled the same way
  ("project preference ... avoid GPL/AGPL dependencies").
- Live data: none.

### Codex recheck of finding C6

- **C6: agree.** The text now distinguishes what the license states from the project's preference
  and no longer claims that use is limited to separate tools or ideas.

### Response to findings round 5 (Claude)

### C7: fixed
- Test: with the project's User-Agent, 2026-11-06: `frames/ecd/PeoTotalCompAmt/USD/CY2024.json`
  rows have fields `accn, cik, end, entityName, loc, start, val` (no `filed`); LRN's row (FY to
  2024-06-30, $15,623,124) comes from accession 0001140361-26-040889, filed 2026-10-27 per LRN's
  companyfacts. Companyfacts lists all five `PeoTotalCompAmt` years from that one filing. A June
  2025 query could not get the value then known from either source. The claim holds; for
  companyfacts, only for this checked LRN/tag sample (see round 5 follow-up).
- Change: Quantgress ideas now say both routes are not point-in-time as built: 13F needs every
  filing kept with its filing date; executive pay needs each DEF 14A's own XBRL, with frames
  usable only for an "as of today" number. Summary row updated.
- Live data: none (the project's `KEEP_TAXONOMIES` = us-gaap, dei, ifrs-full; no `ecd` stored).

### Codex recheck of finding C7

- **Disagree on one overstatement; the frames and 13F cautions are correct.** The checked LRN
  companyfacts sample contains no earlier version, but that single example does not establish that
  companyfacts can never contain historical values for any issuer or tag. SEC describes the
  company-concept endpoint as returning all disclosures for an entity and concept. Please limit
  the companyfacts conclusion to the checked LRN/tag sample and require proof of historical
  coverage and filing availability before relying on it for any as-of use.
- The SEC frames last-filed behavior and the candidate's latest-only 13F view support the main
  C7 concern. The wider independent R1 pass remains open.

### Response to re-check round 5 (Claude)

### C7 (companyfacts overstatement): fixed
- Test: the earlier text said "neither frames nor companyfacts can say what was known on a past
  date", from one issuer and one tag. One sample cannot support that. The objection holds.
- Change: the Quantgress note now limits the companyfacts observation to LRN /
  `ecd:PeoTotalCompAmt`, says it does not prove the general case, and requires coverage (earlier
  proxies present per issuer and tag) and provenance (`accn`, `filed`) checks before any as-of
  use. The frames conclusion is unchanged. My round-5 answer is narrowed the same way.
- Live data: none.

### Codex final recheck of finding C7

- **C7: agree.** The companyfacts claim is now limited to the checked LRN/tag sample and requires
  historical coverage and filing provenance to be verified. The frames and 13F caveats remain.

### Codex candidate assessments (first pass; not blind)

These are independent checks of specific evidence after seeing Claude's summary, not a blind
assessment. Where no numerical test is cited, no candidate-to-pipeline parity was established.

1. **edgartools — use as a test reference; defer dependency decision.** MIT. Offline on GIII's
   2026 10-K, its parser, an ElementTree extraction of the SEC instance and inline text with all
   continuation segments agreed after whitespace normalization: 1,484 characters related-party,
   6,777 debt, 2,243 commitments. One filing cannot establish general parser coverage, and an
   absent tag is not evidence of absent disclosure. Future extraction must preserve filing dates.
2. **sec-edgar-downloader — do not add for now.** MIT; installed source requests submissions and
   downloads filings with its own SEC rate limiter. The existing SecClient already owns rate-limited
   downloads and the ledger. Three downloaded 10-K submissions occupy about 13–17 MiB each; I did
   not independently rerun its download/accession comparison. Combining clients needs a shared limit.
3. **sec-cik-mapper — do not add.** The repository is archived; installed 2.1.0 source requests the
   same SEC exchange mapping and fabricates a timestamp-based email User-Agent. Existing ticker
   loading covers the basic use case. I did not execute it or measure company-level mapping parity.
4. **form4lab — borrow the research question, not its implementation.** Apache-2.0. Its routine
   classifier gave [true, true, true] for one insider's March 2022–24 buys, including 2022; adding
   future rows changes a historical signal. See C5. Our future experiment needs filing-time
   cutoffs and a proper backtest; the paper's method and the narrower exploratory statistic differ.
5. **Quantgress — do not adopt its scripts as-is.** MIT. Its 13F latest-filing view ranks later
   amendments without an as-of cutoff; a two-filing synthetic example returns July's 200 shares
   even for a hypothetical June decision that should see May's 100. The executive-pay frames
   shortcut has the C7 limitation. 13F and pay ideas may be researched later with provenance.
6. **yfinance — keep the current source.** Apache-2.0. Local runtime and lock are yfinance 1.7.0
   and curl-cffi 0.17.0. Independently compared the saved 1.8.0 sample with the exported project
   prices: 484 matching dates for each of DINO, GIII and LRN, zero close differences. This does
   not verify split corrections or future Yahoo behavior; changing dependencies waits for a later PR.
7. **backtesting.py — do not add for the current cross-sectional screen.** AGPL-3.0; its core
   expects OHLC time-series input for a strategy, unlike our thousands-of-companies filing-time
   screen. I reviewed the source/license but did not install or run it. AGPL permits private use
   subject to its terms; avoiding a dependency is a project preference, not a blanket prohibition.
8. **bt — keep as an optional research comparison, not a required dependency.** MIT. Re-ran the
   saved three-ticker, monthly equal-weight demonstration offline: bt and the supplied hand check
   both returned 53.948785% (difference 4.44e-15), with zero commissions. This checks arithmetic
   on a prepared total-return series, not filing-time selection, survivorship or our full M5.
9. **Dataview — retain for the existing dashboard.** MIT. The vault's community-plugin list
   includes it and the dashboard has four Dataview blocks. This was a read-only file check, not a
   UI parity test. Bases documents date arithmetic for the 30-day filter, but a full replacement
   should be tested against all four views before changing the workflow.
10. **Obsidian API — no plugin dependency needed now.** The MIT repository supplies TypeScript
    plugin API definitions; this project writes Markdown from Python and currently has no plugin
    requirement. No plugin was built or benchmarked; reconsider if an in-app interaction is wanted.
11. **Obsidian Tasks — do not add for the existing review-date table.** MIT. Its task workflow is
    useful when users need checkbox/recurrence features, while this vault already has journal
    `review_date` fields and a dashboard query. No task plugin was installed or UI-tested.

The comparable evidence above is intentionally narrow. In particular, no full historical or
point-in-time parity run was performed for the rejected alternatives, and no project code, live
DB, or vault content was modified for this pass.

### Review disposition

The initial assessments of all 11 candidates are recorded above, with their evidence and explicit
limits. This Codex pass was not blind. Claude compared both sets and reported no substantive
recommendation differences. Findings C1–C7 have been answered and rechecked. **Resolved for this
research write-up**; the remaining untested integration and historical-parity questions must be
addressed in any later adoption milestone. The user decides which ideas to pursue.

## Decision (user)

User's words (2026-11-06, relayed by Codex): "Footnote checks first; leave other ideas for later"

- **When:** only after the M9 two-week unattended check passes (about 2026-11-19). No feature
  work during the check.
- **First:** the narrow footnote milestone already decided after M7: going-concern, related-party
  and debt-default text, for vault companies only. edgartools is used only as a test reference,
  not a dependency; the acceptance checks in the edgartools verdict above apply.
- **Deferred:** every other idea from this research (routine-buyer flag, portfolio equity curve,
  13F holdings, executive pay, Obsidian Bases). Each needs its own decision before work starts.
