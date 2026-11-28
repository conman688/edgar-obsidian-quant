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

Ranks priced, non-financial companies on four filters: trailing FCF > 0, EV/EBIT < 6, net
debt <= 25% of market cap, and a cluster buy by insiders known within 90 days. A drawdown rule
(`--min-drawdown 0.5`: at least 50% below the 52-week high) is available but off by default;
the backtest found it worked only after 2020. Every input is point-in-time. Each company gets a status:

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
`stale_price`, `ticker_change` (the last year's prices were priced under more than one ticker, which may be two securities; journal and backtest returns across a ticker change are unknown), `held_source_action` (a source dividend or split dated before a renamed symbol's first close, where history was already stored, is held in `price_actions_held` instead of being stored: it cannot be tied to either security; returns across it are unknown). Flags, shown but not blocking: `multi_class_risk` (no cover-page share
count, so a class count or nothing), `ebit_is_pretax` (pre-tax income stands in for EBIT).

The terminal shows the shortlist and quarantine; `--trace` prints the tag, period and
accession numbers behind each number. Every company, with all metrics, issues, sources and
Form 4 accessions, is written to `data/screens/screen-<as_of>.parquet`; the filters used are
in the file metadata. A full screen takes about 9 s.

## Backtest

```bash
uv run python -m src.cli backtest --start 2012-01-31          # about 45 min first time, 1 min after
```

Asks whether the screen picked stocks that beat the market they were picked from. On every
month end since 2012 it rebuilds the screen's inputs as first reported and known on that date,
then measures 1, 3, 6 and 12-month total returns (splits adjusted, dividends added) of the
picks against the equal-weight average of every company the screen looked at. Costs are 50 bps
each way (`--cost-bps`). It compares filter variants (`VARIANTS` in `src/backtest.py`) and reports
excess return, hit rate, a one-month-holding strategy's CAGR, drawdown and turnover, and whether
a variant worked in both halves of the period.

Monthly snapshots are cached in `data/backtest/` with a fingerprint of the data and SQL, so a
rerun recomputes only what changed. The report goes to `reports/backtest-<date>.md` and to the
vault's `Reports/` folder.

Factor attribution (M12):

```bash
uv run python -m src.cli fetch-factors        # French, q5 and AQR files to data/factors/<today>/
```

The report then regresses each variant's monthly strategy (after costs) on CAPM, FF3,
FF5+momentum, q5 and FF5+momentum+QMJ+BAB, using the newest snapshot (`--factors` picks another).
An April 30 entry is explained by May's factors. It shows alpha and loadings for strategy minus
RF (invested months only) and for strategy minus universe. The factor files only explain past
returns; they never feed the screen.

Safeguards and limits:
- Leak check: the whole run is repeated with numbers visible from their period end, weeks
  before filing. That version must score better, or the harness cannot tell look-ahead from skill.
- Only names closing at $1 or more count; a 10x one-day jump is a broken price series and the
  return across it is left out.
- Survivorship: free prices exist only for companies listed today. On an average date 57% of
  active non-financial filers have no price (mostly later delisted), so results are an upper bound.
- SIC codes (the financial exclusion) and tickers are today's, not as of each date.

## Obsidian vault

```bash
uv run --env-file .env python -m src.cli sync-vault --as-of 2026-11-03   # vault: EOQ_VAULT_DIR
```

Writes the screen into the vault: `Companies/` (shortlisted and quarantined names, plus any
company that already has a note), `Executives/` (one note per insider, by SEC owner CIK, other
spellings as aliases), `Events/` (one note per Form 4 purchase), `Filings/` (every filing a
number came from, with an EDGAR link), `Journal/` (decision notes, below; never written by a
sync), and `Dashboard.md` with Dataview tables (shortlist, quarantine, new insider buys, reviews
due; needs the Dataview plugin) and a then-vs-now table of decisions. Each company note also
lists a year of officer and director sales (10b5-1 plan, discretionary or routine, with EDGAR
links), the window's net insider flow, and `signals` (`cluster_sell`: two or more discretionary
sellers; `buy_while_peers_sell`: a cluster buy while another insider sells). These are for
reading only and never change status or rank. A section "8-K events" lists Forms 8-K and
8-K/A with Item 4.02 (non-reliance), 4.01 (auditor change), 2.06 (impairment) or 3.01 (listing
notice) in the last year and 5.02 (any director or officer change) in the last 90 days, each a
signal too, plus the date of the last results 8-K (Item 2.02). `ingest companies` stores them
from the same `submissions.zip`, so they refresh on Saturdays. A "Short interest" line gives
FINRA's newest known cycle: short shares as a share of shares outstanding (signal
`high_short_interest` at 10% or more) and days to cover. An "Institutional holders (13F)" line
gives the newest quarter past its filing deadline: managers reporting a position, their shares as
a share of shares outstanding, and how many managers that filed both quarters started or stopped
reporting one. `ingest 13f` loads it by hand; it is not scheduled. `Sources/` holds one note per added
data source with its terms and known-at rule. The dashboard's "Macro" line gives the 10y-2y
Treasury spread, NFCI, VIX, VIX9D/VIX and VVIX as known on the sync date, with regime labels
`curve_inverted` (spread < 0), `tight_financial_conditions` (NFCI > 0) and `vol_backwardation`
(VIX9D/VIX > 1 on the same trade date); it never changes the screen.

Notes are found by the `cik` in their frontmatter, so renaming one is safe. Only the
`AUTO-GENERATED` block and the auto frontmatter keys change; your own keys, comments and text
outside the block are kept. A file is written only if its content changes: syncing the same
date twice writes nothing. First sync on 2026-11-03: 762 notes in 10 s.

```bash
uv run --env-file .env python -m src.cli fetch-text --as-of 2026-11-03   # before sync-vault
```

`fetch-text` downloads the latest two 10-Ks of each company the vault shows and keeps only
Item 1A (Risk Factors) in `filing_sections`; each filing is downloaded once. The company note
then lists the sentences that are new or removed against the prior 10-K (numbers and dates are
masked, below 0.8 similarity counts as new), and forensics from facts already loaded: buybacks,
acquisitions and impairments of the latest fiscal year, the debt maturity schedule, and flags
`debt_wall` (due within 2 years above cash), `acquisitive` (10% of market cap),
`impairment` (5% of market cap). Flags are for checking by hand; they do not change the screen.

`fetch-text` also reads each of those 10-Ks' related-party note
(`us-gaap:RelatedPartyTransactionsDisclosureTextBlock`) from its XBRL instance: `index.json`
to find the one `*_htm.xml`, then the instance (section `fn_related_party`, its own ledger). Only
the filer's own, non-dimensional block ending on `dei:DocumentPeriodEndDate` counts. The company
note's "Related parties" section shows it (10-K only), the new and removed sentences against the
prior 10-K as candidates to read, and the full text folded; frontmatter `related_party_new`. A
10-K without the tag is "no related-party note tagged", never "no dealings"; a note found only in
other contexts, or two different current notes, is shown as not uniquely identified; an
instance that could not be read is retried next run and listed in the health warnings.
Informational only: it never changes status, rank or the shortlist.

### Decision journal

```bash
uv run --env-file .env python -m src.cli journal DINO              # --as-of DATE, --review-days 90
```

Creates `Journal/DINO-YYYY-MM-DD.md`: sections for the thesis, what the market believes, why it
is wrong, kill criteria, position size and the review, frontmatter keys `decision`,
`position_size`, `thesis`, `review_date` (90 days on by default) for you to fill in, and the
company's screen row on that date frozen under `snapshot` (status, rank, EV/EBIT, FCF yield, net
cash, raw close and its date, issues, filters). The note is never rewritten: `journal` refuses a
note that exists, and `sync-vault` does not write in `Journal/`. A journaled company keeps its
company note updated even after it leaves the screen.

The dashboard sets each snapshot decided on or before the sync date against the screen on that
date: status, EV/EBIT, FCF yield, net cash, price change and total return from the snapshot's
close (splits adjusted; dividends that went ex after the decision date added). If a dividend
went ex between that close and the decision, the total return shows "-". "Reviews due" lists notes whose `review_date` has passed.

### Tone

The note also shows the share of Risk Factors words on the Loughran-McDonald negative,
litigious and uncertainty lists, this 10-K against the prior one (frontmatter
`tone_negative_change` etc., in share points). The word lists are free for academic research;
commercial use needs a licence from the authors. They are not in the repository: download
`Loughran-McDonald_MasterDictionary_1993-2025.csv` from
https://sraf.nd.edu/loughranmcdonald-master-dictionary/ into `data/lm/` (or set `EOQ_LM_DICT`).
Without the file, notes say the lists are not installed.

## Operations

```bash
uv run --env-file .env python -m src.cli run weekly    # backup, ingest all, screen, fetch-text, sync-vault
uv run --env-file .env python -m src.cli run daily     # Form 4, prices (both strict), then sync-vault
uv run --env-file .env python -m src.cli backup        # the backup step alone
uv run python -m src.cli schedule                      # launchd files in data/launchd (not installed)
```

A run takes a lock (`data/.run.lock`), so two runs never overlap, and stops at the first step
that fails. Every load keeps a ledger, so the next run picks up where it stopped: a run killed
mid-way refetches at most the batch it was writing. Each run adds a row to `run_log` and writes
a health report (stdout, and `Reports/Health.md` in the vault). The report lists step results,
row counts against the previous run, and warnings for: a step that failed, a table that shrank,
failed Form 4 or price ledger entries, a stale latest price (more than 4 days old after a run
that refreshed prices; more than 8 on a daily without a vault, which does not), SEC throttling, and a
backup that did not happen.

The weekday daily republishes the dashboard on that day's closes. It runs `ingest form4
--strict`, then `ingest prices --strict`. Each fails its step if any filing or company failed;
the failures stay in their ledger (`form4_filings`, `price_status`) and are retried next run.
A provider outage ends the daily's price step after 3 batches in a row fail whole (each after
its own retries, about 10 minutes in all); the rest are not attempted and keep their stored
closes. The weekly and a standalone `ingest prices` still try every batch.
Only after both are complete does it run `sync-vault`, which recomputes the screen. A failed step
stops the run, so the vault keeps the previous dashboard. On a market holiday the provider has no
new close and the dashboard uses the last one. Without `EOQ_VAULT_DIR` the daily is what it
was before it published: `ingest form4` alone, without `--strict` (failures warn and are retried)
and without a price fetch. Tickers, company profiles, XBRL facts, 10-K text and the dated screen Parquet
(`data/screens/`) refresh only on Saturday's weekly. The dashboard shows the newest dated close,
Form 4 and XBRL fact in the database (across all companies, not a coverage claim), and each
company note keeps its own close date (`price_date`) and sources. The screen's freshness rules
apply as on any day: a close more than 7 days old quarantines the company (`stale_price`).
Journal notes are never rewritten.

Backups: set `EOQ_BACKUP_DIR` to a folder on another disk, for example
`/Volumes/External/edgar-obsidian-quant-backups`. The weekly run copies the database there as
`edgar-YYYY-MM-DD.duckdb` and checks the copy (it opens and has the same number of loaded
filings). Then it deletes all but the newest 4 files with that name pattern. It never deletes
any other file, and never creates the folder. If the folder is missing (drive unplugged), on
the database's own disk, or the disk has too little room, the backup is skipped and the health
report says so. The rest of the run goes ahead.

Scheduling: `schedule` writes two launchd agents: weekly on Saturday at 07:00, daily on weekdays
at 21:00. Both run under `caffeinate`, and a run missed while the Mac sleeps starts when it
wakes. Install them with the `launchctl bootstrap` commands that `schedule` prints. Logs go to
`data/logs/`. The jobs run the code checked out in this folder: keep `main` checked out here and
do branch work in a separate worktree. GitHub Actions runs the tests on every push (`.github/workflows/tests.yml`).

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
| `reviews/` | Milestone reviews: Claude Code builds, Codex reviews (`reviews/PROTOCOL.md`) |
| `AGENTS.md`, `CLAUDE.md` | Standing instructions each agent reads on start |
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
| `src/obsidian/writer.py` | Atomic note writer, AUTO-block merge (comments kept), CIK-keyed person registry |
| `src/ingestion/filing_text.py` | `fetch-text`: 10-K primary document to text, Item 1A cut, `filing_sections` ledger |
| `src/ingestion/related_party.py` | `fetch-text` (M10): 10-K XBRL instance, current related-party block, `fn_related_party` rows |
| `src/redline.py` | Risk Factors and related-party redline: sentences, number/date masking, new and removed sentences |
| `src/tone.py` | Loughran-McDonald word lists (current words only) and word-share tone |
| `src/forensics.py` | Buybacks, acquisitions, impairments, debt maturities of the latest fiscal year; flags |
| `src/obsidian/sync.py`, `src/obsidian/templates/` | `sync-vault`: companies, insiders, purchases, filings, dashboard (Jinja); `journal` notes |
| `src/ops.py` | `run`, `backup`, `schedule`: run lock, steps, `run_log`, health report, backup and retention, launchd files |
| `src/journal.py` | Decision journal: frozen screen snapshot, journal notes read back, return since the decision |
| `tests/test_failure_modes.py` | One test per audited failure mode |
| `tests/test_ingest.py` | Bulk load idempotency, crash resume, reference data, conditional download |
| `tests/test_ingest_recovery.py` | 304 after failed or filtered loads, M0 upgrade, facts added to known filings |
| `tests/test_schema.py` | Migrations: fresh, legacy adoption, new, failing, newer-than-code |
| `tests/test_prices.py` | Split undo, refresh idempotency, primary-ticker choice, failed batches |
| `tests/test_review_fixes.py` | Price split direction, filing order before tag priority |
| `tests/test_m2_review.py` | Price refresh windows, corporate actions, failure status, filing replacement |
| `tests/test_form4_sources.py` | Data set vs XML parity on a real filing, ledger, amendments, identical lots |
| `tests/test_m4_screen.py` | Debt source by date, stale items, EBIT/capex ladders, typo dates, screen status and trace |
| `tests/test_m6_vault.py` | Frontmatter comments survive, idempotent sync, hand edits, renamed notes, one note per insider, name order |
| `tests/test_m7_text.py` | Self-diff and number-only checks, extraction, fetch ledger, PIT redline, forensics, note |
| `tests/test_m9_ops.py` | Backup checks and retention, run log and health warnings, run lock, killed Form 4 fetch resumes, launchd files |
| `tests/test_m8_journal.py` | Journal template and snapshot, never overwritten, then-vs-now returns, PIT, missing keys |
| `tests/test_m5_backtest.py` | Split/dividend returns, delisted exits, statistics, costs, halves, leaky policy, cache, report |

## Invariants

- Every screen sees only data with `filed <= as_of`. No look-ahead.
- A Risk Factors redline compares the latest two 10-Ks filed on or before `as_of`.
- A fact whose period ends more than 7 days after its filing date, or after `as_of`, is ignored.
- Balance-sheet items are compared to the newest one; an older item is stale, never current.
- Schema changes are append-only migrations in `src/db/schema.py`. `connect()` applies pending ones; it refuses a database newer than the code.
- Restatements are new rows under a new filing; earlier filings are never overwritten by them.
  A filing's rows are replaced only when SEC's snapshot of that same filing changes (fingerprint).
- A price refresh replaces prices, splits and dividends only inside windows that returned data.
- Market cap = raw close on `as_of` x cover-page shares. Historical share trends are split-adjusted.
- Conviction buys = non-derivative, code `P`, acquired `A`, price > 0, not a 10b5-1 plan.
- Insider sells (M12) = the same row rules with code `S`, disposed `D`; discretionary only when
  outside a 10b5-1 plan and not routine (same month in each of the 3 prior years). They and
  `signals` are informational: they never change a filter, status, rank or the shortlist.
- 8-K events (M12) are known from their SEC filing date (`filed <= as_of`), never the acceptance
  time. Items are SEC's raw string, matched as whole tokens; the legacy numbers used before
  2004-08-23 are not mapped to the current ones.
- FINRA short interest (M12) is known at the later of 9 weekdays after its settlement date
  (FINRA publishes after 7 business days) and the day it was first fetched, because rows are
  revised in place; it is matched to companies by current ticker. FINRA requests use a
  generic User-Agent, never the SEC one.
- 13F holdings (M12) are known from the quarter's filing deadline (quarter end + 45 days, the next
  business day). A quarter counts only reports filed by then, so it never changes; it is never
  dated by the quarter end. CUSIPs are matched by SEC fails-to-deliver symbols to the current
  ticker: under one symbol they take turns by first sighting, and a CUSIP that later trades under
  another symbol for more than 92 days only borrowed the ticker. New and exited managers are
  counted per company across its CUSIPs, so a CUSIP change is not a change of holders.
- FRED macro series (M12) are known at their ALFRED vintage date (`realtime_start`), so a revised
  value is read as it was published on or before `as_of`. The FRED key (`FRED_API_KEY`) goes only
  to api.stlouisfed.org, with redirects refused. Cboe closes (M12) are known at the later of the
  trade date and the first fetch, because Cboe backdated the modern VIX to 1990.
- Factor files (M12) are recomputed in full by their publishers, so they are kept as dated
  snapshots with a sha256 manifest; a report names the snapshot it used, and a same-date fetch
  keeps the first one. They explain backtest returns only and never feed the screen.
- Notes are written atomically. Only the `AUTO-GENERATED` block and auto frontmatter keys change.
- A journal note is written once, on its decision date, and never rewritten; its snapshot is the
  screen as it was then.
- A backup goes only to an existing folder on another disk, and deletes only its own
  `edgar-YYYY-MM-DD.duckdb` files beyond the newest 4.

## SEC access

SEC requires a `User-Agent` with a real contact email and caps traffic at 10 req/s.
`SecClient` enforces 8 req/s.

## Status

Done: schema with migrations, PIT layer, screens, Obsidian writer, M1 ingestion (live SEC data),
M2 prices/splits/dividends, M3 insider trades, M4 screener, M5 backtest, M6 vault sync,
M7 Risk Factors redline and forensics, M8 decision journal, M9 operations (run, backup, health,
launchd files, CI), CLI.
M10 related-party footnotes: merged 2026-11-22; first live weekly run pending (ROADMAP M10, Status).
Not done: M7 executive pay (deferred); footnotes other than related-party (out of M10's scope);
two unattended weeks of the installed launchd agents (M9 check, to about 2026-11-19).

See [ROADMAP.md](ROADMAP.md) for goals, milestones and acceptance checks.
