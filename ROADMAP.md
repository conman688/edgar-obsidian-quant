# Roadmap

## Outcome

One weekly command turns every US SEC filer into a short list of about 20 cheap,
out-of-favour companies with insiders buying their own stock. Each name lands in
Obsidian with its numbers, insider history, footnote changes and a decision journal.
Everything runs locally, is deterministic, and costs $0/month. No LLM API.

The post's real edge is the decision, not the data. So the tool must also prove the
screens worked in the past (honest backtest) and record each decision so you can
learn from it later.

## Goals

| ID | Goal | Measured by |
|---|---|---|
| G1 | Point-in-time screen of all SEC filers | Full screen runs in < 30 s on the local DB; no data with `filed > as_of` is ever read |
| G2 | Insider conviction-buy alerts | Cluster buys known within 1 day of filing; grants, plan trades and duplicates excluded |
| G3 | Obsidian second brain | Re-sync makes zero changes when the data is unchanged; one note per person; manual notes never lost |
| G4 | $0/month, deterministic | No paid API keys in the code path; the same inputs always give the same outputs |
| G5 | Evidence the screens work | Backtest report: excess return, hit rate, drawdown, turnover vs equal-weight universe |
| G6 | Decision discipline | Every shortlisted name you act on has a thesis, kill criteria and a review date |

## Non-goals

- Placing trades or connecting to a broker.
- Intraday or real-time prices.
- Investment advice. The tool ranks; you decide.
- Valuing banks, insurers and REITs in v1. EV/EBIT is meaningless for them, so they are tagged and excluded.
- Non-US filers without XBRL.

## Milestones

Each milestone ends with checks you can run. A milestone is done only when its checks pass.

### M0 Foundation (done)

DuckDB schema, point-in-time layer, screens, Form 4 parser, Obsidian writer, 10 failure-mode tests.

### M1 Fundamentals ingestion (size: M)

- `cli.py ingest facts`: download `companyfacts.zip` with conditional GET (skip if unchanged), load all filers.
- Ticker to CIK map from `company_tickers.json`, refreshed each run.
- Industry tag (SIC code from `submissions` API) to exclude financials.
- Ledger row per bulk file version so a crash resumes without reloading.

Checks:
- Re-running the load inserts 0 rows.
- Spot-check 5 companies (one June FY, one 52/53-week, one multi-class, one recent reverse split, one bank) against their 10-K by hand.
- Full load time recorded in the README.

### M2 Prices and splits (size: M)

- Daily closes and split history for the universe.
- yfinance `Close` is split-adjusted back in time. Rebuild the raw close as
  `adjusted x product(split ratios after that date)` before storing `close_raw`.
- Survivorship: yfinance drops delisted tickers. Record which names have no price
  history so the backtest can report the gap instead of hiding it.

Checks:
- NVDA close on 2024-06-07 (day before its 10-for-1 split) stored as about $1,209, not about $121.
- Market cap for 5 names within 2% of the cover-page figure times that day's close.

### M3 Insider ingestion (size: L)

- List Form 4 filings from EDGAR quarterly `form.idx` and daily indexes; keep only issuers in the universe.
- Fetch and parse XML through `SecClient` (8 req/s), ledger per accession.
- Backfill 2 years, then run daily. Backfill is an overnight job; measure its rate and log it.
- Confirm the 10b5-1 checkbox element name on a real post-April-2023 filing and pin it in a test.
- Handle Form 4/A: amended rows supersede the original for the same transaction.

Checks:
- Replay of a day inserts 0 rows.
- 3 known cluster buys from the news reproduced by `cluster_buys`.
- No row with `txn_code = 'A'` ever counted as a buy.

### M4 Screener and shortlist (size: S, done)

- `cli.py screen --as-of DATE`: joins valuation, share trend and cluster buys into one ranked table.
- Default filters: drawdown > 50%, FCF > 0, EV/EBIT < 6, net cash or low debt, insider buying in 90 days.
- Quarantined names (missing split, stale shares, missing debt) listed separately, never ranked.
- Output: terminal table plus a dated Parquet file for history.

Checks:
- Screen finishes in < 30 s.
- Each shortlisted row traces back to the accession numbers it used.

Result (2026-11-03): full screen 8-10 s (valuation alone 6 s, was 22 s: one scan of the facts
table instead of eleven); runs under a 600 MB DuckDB cap. `--trace` and the Parquet
`sources` / `form4_accns` columns give the accessions. As of 2026-11-02 the defaults
shortlist 0 of 4,182 companies (14 quarantined); as of 2025-06-30, 1 (WFRD). Only 118
companies have EV/EBIT < 6 and 323 have an insider cluster buy; none of the clean ones is
also down 50%. M5 tested the defaults; see M5.

Open data gaps found by the quarantine:
- Debt-free companies have no debt tag, so they cannot be told from untagged debt (1,213
  `missing_debt`). Convertibles, notes and credit lines on their own lines are not summed.
- Capex split across several tags takes only one (FCF errs high).
- ADRs that file 10-Ks (AKTX) get an ADS price times ordinary shares; only 20-F/40-F filers
  are flagged. The usual 1 ADS = n shares makes them look dearer, so they are missed, not
  wrongly shortlisted.
- XOM moved to a new holding-company CIK (2115436) that has 10-Qs only; its history is under
  the old CIK, so EBIT and FCF are missing. Successor CIKs are not linked.
- Companies table (SIC, financial exclusion) and tickers are current snapshots, not as of the date.

### M5 Backtest harness (size: L, done)

- Monthly rebalance from 2012 to today using `as_first_reported` and `filed <= rebalance date`.
- Forward 3, 6 and 12-month returns vs equal-weight universe, with costs.
- Report hit rate, excess return, max drawdown, turnover and the survivorship gap from M2.
- Splits the history into halves; a screen that only works in one half is flagged.

Checks:
- A deliberately leaky version (using `period_end` instead of `filed`) scores better than the real one. This proves the harness can detect look-ahead.
- Report written to `reports/backtest-YYYY-MM-DD.md` and to the vault.

Result (2026-11-04, `reports/backtest-2026-11-04.md`): 177 month ends, 2012-01 to 2026-10.
First run 40-44 min, reruns under 1 min from the snapshot cache. Equal-weight universe
(close >= $1): 10.0% a year, max drawdown 35%. Reviewed by Codex (reviews/M5-backtest.md,
8 findings, all fixed).

| variant | months with picks | avg picks | 3m excess | 12m excess | 3m hit rate | both halves |
|---|---|---|---|---|---|---|
| default (value + insider buying) | 99 | 1.8 | +4.0% | +15.2% | 56% | yes (3m +3.8% / +4.2%) |
| drawdown_50 (the M4 default) | 27 | 1.2 | +6.8% | +35.7% | 45% | no (3m -7.3% / +19.9%) |
| value (no insider rule) | 177 | 13.4 | +0.1% | +0.4% | 48% | no |
| insider_fcf (insider buying, FCF > 0) | 177 | 60.5 | +0.2% | +2.2% | 47% | no |

Checks: leak check passes (value variant, 3m excess: real +0.1%, leaky +1.9%), but narrowly,
and for the default variant the leaky run scored worse, a sign of how few picks there are
(about 180). Report written to `reports/` and the vault.

Decision: the drawdown rule is off by default (`--min-drawdown 0.5` restores it); it only
worked after 2020. The default variant is the only one positive in both halves. Its one-month
strategy still returns 7.0% a year against the universe's 10.0%, because it holds cash in 78 of
177 months; the edge is per pick, not as a fully invested portfolio. With these filters the
live screen shortlists 3 names (2026-11-03), not the 20 the outcome asks for.

Open:
- Few picks: about 180 over 15 years. Treat the excess as a hint, not proof.
- Survivorship gap 57%; delisted names are missing from both picks and universe.
- Insider data before 2023 has no 10b5-1 checkbox; only plan footnotes exclude plan trades.

### M6 Obsidian vault sync (size: M, done)

- Jinja templates: `company.md.j2`, `executive.md.j2`, `filing.md.j2`, `event.md.j2`.
- Folders: `Companies/`, `Executives/`, `Filings/`, `Events/`, `Journal/`.
- Frontmatter keys fixed for Dataview: `ticker`, `cik`, `ev_ebit`, `net_cash_to_mcap`, `share_cagr_3y`, `insider_buy_90d_usd`, `drawdown_52w`, `as_of`.
- `Dashboard.md` with Dataview tables: shortlist, new insider buys, reviews due.
- `cli.py sync-vault` only writes notes whose content changed.

Checks:
- Second sync reports 0 files written.
- Edit a note by hand, re-sync, edit survives.
- 3 name variants of one Form 4 filer produce one Executive note.

Result (2026-11-04): first sync as of 2026-11-03 wrote 762 notes (52 companies, 156 insiders,
239 purchases, 314 filings, dashboard) in 10 s; the second wrote 0. A hand edit to a live note
(frontmatter key with a comment, text below the block) survived a re-sync. 156 insider notes for
156 owner CIKs, including CIKs filed under several spellings ("Perdue George Ervin III" and
"Perdue Sonny": one note, both as aliases). Also fixed: YAML comments in frontmatter were lost
on re-sync (now round-tripped with ruamel.yaml), and Form 4 names in the bulk data sets are
mixed case, so "Oliver George" was not turned into "George Oliver".

### M7 Text forensics (size: M, done except the deferred items below)

- Shortlist only, so request volume stays small.
- YoY redline of Risk Factors and key footnote text blocks: sentence split, numbers and dates
  replaced with placeholders, a sentence counts as new below 0.8 similarity.
- Loughran-McDonald word lists for negative, litigious and uncertainty tone change. Check its licence terms before use.
- Executive pay from DEF 14A pay-versus-performance tags (`ecd:` via edgartools), plus the company-selected measure.
- Impairments, acquisitions, buybacks and debt maturity walls from facts already loaded in M1.

Checks:
- A 10-K diffed against itself yields 0 new sentences.
- A changed number alone does not mark a sentence as new.

Result (2026-11-05): `fetch-text` as of 2026-11-03 stored Item 1A of 38 10-Ks (19 companies, the
latest two each, 4.5 MB of text) in 21 s; a second run downloads no documents. The other 33 vault
companies file 20-F/40-F or no 10-K yet. Each live 10-K diffed against itself gives 0 new
sentences. New sentences, latest 10-K against the prior one: median 9%, HRL 52% (rewritten
economic-conditions risks and a $163.7M impairment), DINO 85 of 686 (CEO and CFO leave in
February 2026, review of disclosure controls), GIII 57 removed (Calvin Klein and Tommy Hilfiger
licences ending). Forensics (latest fiscal year from facts): 20 companies; flags `debt_wall`,
`acquisitive`, `impairment` (1 acquisitive on 2026-11-03). Fixed on live data: sentences broken
across lines and page breaks (page numbers, "Table of Contents") are rejoined; a risk summary
repeating a sentence lists it once.

Tone (2026-11-05, user approved the licence: free for academic research): the 2026 Master
Dictionary (1993-2025, 86,553 words; 2,345 negative, 903 litigious, 297 uncertainty currently
listed). Risk Factors negative share 3.0-5.1% across the 18 companies with text; largest rise
HRL +0.32 points (4.73% to 5.05%).

Deferred:
- Executive pay: the `ecd:` tags are not in companyfacts.zip; needs each DEF 14A's XBRL.
- Footnote text blocks: companyfacts.zip has numbers only; needs each filing's XBRL instance.

Decision (user, 2026-11-05): build nothing while the M9 two-week check runs (to about
2026-11-19), since a merge into main changes what the scheduled jobs run. Then footnotes first,
narrow: going-concern, related-party and debt-default text blocks for the vault companies only.
Executive pay last or not at all: it says little about whether a cheap stock is a buy, and
insider buying is already tracked.

### M8 Decision journal (size: S, done)

- `cli.py journal TICKER` creates `Journal/TICKER-YYYY-MM-DD.md` from a template:
  thesis, what the market believes, why it is wrong, kill criteria, position size, review date.
- Snapshot of the screen metrics frozen into the note at decision time.
- Dashboard lists reviews due and compares then-vs-now metrics.

Checks:
- Journal snapshot does not change when the underlying data later updates.

Result (2026-11-05): `journal TICKER` takes 17 s (one screen). Trial on a copy of the vault:
DINO as of 2026-08-03 (status fail, EV/EBIT 6.3, close 88.65) against the screen on 2026-11-03
(shortlist, EV/EBIT 4.0, close 113.36): price +27.9%, total return +28.5% with the $0.525
dividend of 2026-08-11, both matching a hand calculation from raw prices. A second `journal`
for the same date is refused; JPM (a bank) is refused. Syncing the real vault wrote only
`Dashboard.md` (new table, no decisions yet), then 0 of 781 files.

### M9 Operations (size: S, done except the two-week check)

- Weekly `launchd` job: ingest, screen, sync. Daily job: Form 4 only.
- Health report: row-count drift, failed ledger entries, stale prices, SEC throttling events.
- DuckDB backup before each weekly run; keep the last 4.
- GitHub Actions runs the tests on every push.

Checks:
- Two unattended weeks with no manual fixes.
- A forced crash mid-run resumes without duplicates or wasted refetches.

Result (2026-11-05): `run`, `backup`, `schedule`, `run_log` (migration 8), health report,
GitHub Actions. Backups go to an external USB drive (`/Volumes/External`, 500 GB, exFAT,
446.5 GB free), folder `edgar-obsidian-quant-backups`: 4.7 GB in 54 s; the copy opens and has
the same counts as the database (123,556,824 facts, 424,435 filings, 5,929,257 Form 4 rows).
Daily run: 807 new Form 4 filings (2,088 rows) in 160 s, then 0 refetched in 24 s; a run started
alongside is refused by the lock. First weekly run: backup, tickers, profiles, 231,400 new facts
and 4,184 companies priced, then it stopped at Form 4 on one dropped SEC connection
(`RemoteDisconnected`). Fixed: the SEC client now retries dropped connections and timeouts.
The rerun finished in 276 s with no warnings: both zips and all 8 Form 4 data sets answered
"not modified" (no refetch), 8 new Risk Factors sections, 144 vault notes written, shortlist
DINO, GIII, LRN. Forced crash: a `SIGKILL`ed Form 4 fetch resumes with no duplicates and
refetches at most the one uncommitted batch (`tests/test_m9_ops.py`).

Installed 2026-11-05: `EOQ_BACKUP_DIR=/Volumes/External/edgar-obsidian-quant-backups` in `.env`,
both launchd agents bootstrapped (weekly Saturday 07:00, daily weekdays 21:00). The jobs run the
code checked out in the repository folder, so keep `main` checked out there. Open: the "two
unattended weeks" check, 2026-11-06 to about 2026-11-19; `run_log` and `Reports/Health.md`
show each run.

### M10 Footnote checks (size: M, planned; starts only after the M9 two-week check passes)

Decision (user, 2026-11-05 and R1): footnotes first, narrow: going-concern, related-party and
debt-default text for the vault companies only. Plan written 2026-11-07, before any code; facts
below were checked against SEC on 2026-11-07. Revised after review round 1
(`reviews/M10-footnote-checks.md`).

Ground rules:
- Informational only: M10 never changes screen status, rank or shortlist membership.
- Annual only: the latest 10-K filed by `as_of` (and the one before it, for comparison). Every
  result shows that 10-K's report date, filing date and accession, and says "10-K only". 10-Q,
  10-K/A and 20-F/40-F filers are not covered and are shown as "not covered".
- A missing tag, phrase or amount is never shown as "no problem": it is "not found" or
  "not reported in this 10-K".
- Text matches are **candidates for manual review**, not findings. No language classifier.
- Full selected text stays readable in the vault with its accession and URL.

What it adds, per vault company that files 10-Ks (25 of the 59 vault companies on 2026-11-06,
49 stored 10-Ks):

| Check | Source | Shown as |
|---|---|---|
| Going-concern | `us-gaap:SubstantialDoubtAboutGoingConcernTextBlock` in the latest 10-K's XBRL instance; and a phrase scan of the whole 10-K text (notes and auditor's report): a sentence with both "substantial doubt" and "going concern" | `tagged`, `text only` (phrase, no tag) or `none found` ("no tagged block or phrase found") |
| Related-party | `us-gaap:RelatedPartyTransactionsDisclosureTextBlock` | the current text if present; new and removed sentences against the prior 10-K, or "no comparison" when the prior 10-K has no such block or there is no prior 10-K |
| Reported default amount | `DebtDefaultLongtermDebtAmount`, `DebtDefaultShorttermDebtAmount` in `xbrl_facts` | see "Default amounts" below |
| Debt-default language | the current debt note (`DebtDisclosureTextBlock`, or `LongTermDebtTextBlock` when that is the one tagged) | every current sentence containing "in default", "not in compliance", "waiver", "forbearance" or "accelerat", each marked `new` or `unchanged` against the prior 10-K, or "no comparison"; labelled "review: default-related wording", never "in default" |

Default amounts (sparse facts; same rule as `src/forensics.py`, which reads the latest fiscal
year only so an old value is never shown as current):
- Read only facts from the selected latest 10-K's own accession with `period_end` equal to its
  report date, `filed <= as_of` and `period_end <= as_of`. Values from older reports are never
  carried forward.
- Absent: "not reported in this 10-K" (unknown, not zero). Explicit 0: "reported zero".
  Positive: the amount, its period end and filing, and the flag `debt_default_reported`.
- If the selected 10-K's report date is more than 470 days before `as_of` (forensics'
  `STALE_FY_DAYS`), all M10 results for that company are marked stale.

How (reusing M7, no new dependency; edgartools only as a test reference in a throwaway venv):
- Per new 10-K: `index.json` of the filing folder to find the one `*_htm.xml` instance (its name
  does not always follow the primary document: `darkpulse_i10k-123125_htm.xml`), then the
  instance (GIII 3.2 MB, DarkPulse 1.1 MB). Text-block facts read with the stdlib XML parser,
  their HTML turned into text with the existing `html_text`. Two requests per new 10-K through
  `SecClient`; raw files are not kept.
- The going-concern phrase scan runs on the 10-K document `fetch-text` already downloads.
- Storage in `filing_sections` (no migration), in two families, each with its own parser
  version, each written in one transaction per filing:
  - document family (one download): `risk_factors`, `gc_phrase`;
  - instance family (index + instance): `fn_going_concern`, `fn_related_party`, `fn_debt`.
- A family is complete for a filing only when all its sections exist at its current parser
  version. Each family is checked on its own: a complete document family never skips a missing
  instance family, and the other way round.
- Status per row: `ok` (found), `not_found` (the instance or document parsed and the tag or
  phrase is absent: final, not fetched again), or `error` (request failed, no or several
  `*_htm.xml` in the index, XML that does not parse: retried on the next run and listed in the
  health report's warnings). An `error` row is never shown as "not found".
- Runs inside `fetch-text`, so the weekly job's steps do not change.
- Vault: company note gets a "Footnotes" section (report date, filing, "10-K only", each check
  with its text and provenance) and a `footnote_flags` frontmatter list: `going_concern_tagged`,
  `going_concern_text`, `debt_default_reported`, `debt_language_review`, `related_party_new`.
  Journal notes are never touched.

Checks:
- GIII 10-K 0001104659-26-033891: stored text equals the inline 10-K text with continuation parts
  joined, compared with **all whitespace removed**: related-party 1,484 characters, debt 6,777.
  The first inline part alone gives 908 for debt, so the test fails on that shortcut.
- DarkPulse 10-K 0001683168-26-002937 (CIK 866439): going-concern `tagged`.
- Synthetic 10-K with going-concern language but no tag: `text only`, never `none found`.
- Synthetic 10-K with neither: `none found`, shown as "no tagged block or phrase found".
- Debt language (R1-style fixtures): "We are not in default under any debt agreement." and "If we
  fail to comply with financial covenants, lenders may accelerate repayment." are shown only as
  review candidates and never set `debt_default_reported`.
- An unchanged paragraph "We are in default under our loan agreement and the lender may demand
  repayment." in both 10-Ks is shown as a current candidate marked `unchanged`; a first debt note
  with no prior 10-K shows its candidates with "no comparison".
- Default amounts: a positive 2020 amount plus a 2025 10-K with no default tag shows "not
  reported in this 10-K", not a current default and not zero; an explicit 0 shows "reported
  zero"; a positive amount in the selected 10-K sets `debt_default_reported` with its period
  and filing.
- Point-in-time: a 10-K or a default amount filed after `as_of` is never read; comparisons use
  the latest two 10-Ks filed by `as_of`.
- Ledger: a run stopped after the `risk_factors` row and before the instance family is written
  fills the missing sections on the next run without refetching the document; a failed instance
  request, an index with zero or two `*_htm.xml` files, and malformed XML each leave an `error`
  row that is retried next run and reported; a parsed instance with no tag stores `not_found`
  and is not fetched again.
- A second `fetch-text` downloads 0 documents; a second `sync-vault` writes 0 files.
- Screen output (status, rank, shortlist) is identical with and without M10 data.
- Live run on the vault companies (vault backed up first): requests made, 10-Ks read, counts per
  status, the companies flagged; parity with edgartools on 3 vault companies offline.

Not in scope: executive pay, 10-Q footnotes, 10-K/A, 20-F/40-F (IFRS) filers, other text blocks,
classifying language automatically, events after the latest balance sheet (8-K deals such as
GIII's Marc Jacobs purchase).

### M11 Quarantine checks (size: M, planned; starts after M10)

Request (user, 2026-11-08): make the program check the issues that quarantine a company, so that
fewer of them are left for manual reading. This is a plan only. No code is written before the M9
two-week check passes (no earlier than 2026-11-22 20:26:59 MST) and M10 is done.

Why. The 2026-11-07 screen (`data/screens/screen-2026-11-07.parquet`) has 3 shortlisted names
and 51 quarantined ones. A quarantined name fails no filter, and all 51 pass the insider-buying
filter. These are the candidates nearest to the shortlist. Today the only way to clear one is to
read its filings by hand. 12 names were quarantined on all four saved screens (11-02, 11-03,
11-05, 11-07).

What is in the quarantine on 11-07. Counts are from the parquet file; a name can be in several
groups. The causes are hypotheses, to be confirmed against SEC at build time:

| Group | Names | Likely cause |
|---|---|---|
| Foreign filer | 21 flagged `foreign_filer`. FMX and GRAB are not flagged and may also be 20-F filers (to confirm) | IFRS statements and annual covers; the concept map is us-gaap only. FMX and GRAB have no shares or cash facts at all, and `foreign_filer` reads the form only from those facts |
| No DEI cover shares (`multi_class_risk`) | 11: CXM, MIR, HRL, TSN, FMX, RSI, LUCK, GRAB, INR, FLOC, GLIBA | Possibly cover counts tagged per share class, which companyfacts drops (comment in `queries.py`). A missing DEI count alone does not confirm this; 8 have `missing_shares` and 3 fall back to a stale balance-sheet count (`stale_shares`) |
| No EBIT and no FCF, US filer | 10: MFP, NVRI, SBMT, BLSM, CNXU, LTGO, MOBI, LIEN, NSLR, FBDT | TTM needs an annual value (`ttm_flow_sql`), and the stored facts may have no annual base (why is unknown: incomplete snapshot, recent listing, or tags outside the map) |
| `missing_debt`, US filer | 14: BLSM, CAVA, CNXU, EPSN, FBDT, HUBS, LIEN, LTGO, MOBI, NSLR, OFLX, RSI, SEZL, SMID | No debt at all (CAVA, OFLX?), or debt under tags outside the map (convertible notes: HUBS?) |
| `stale_debt` | 5: AN, IMMR, UPBD, CXM, INR | A debt line reported in the last 400 days is missing at the newest date |
| `share_jump` | 3: SEZL, RSI, LIEN | Listing, reorganisation or a missed split |
| `penny_price` | 3: FBDT, EVGN, SYNX | Correct as it is |

Ground rules:
- Missing stays missing. A value is filled only from a filing that states it. That filing is
  filed on or before `as_of`, and its accession and method go in `sources`. Absence of tags is
  never read as zero.
- Point-in-time. A historical screen or backtest sees a resolved value only from its filing date.
  Journal snapshots are never rewritten.
- Phase 1 changes no status, rank or shortlist. Phase 2 can change status in either direction:
  a resolved input may fail a filter.
- Every Phase 2 rule is test-first, and is measured on live data before and after. The measure
  is the status changes, each with the input and the filing that changed it.

Phase 1: diagnose from stored data (no network, informational only). For each quarantined name
and each issue, the screen adds a reason. The only filing metadata used comes from `xbrl_facts`:
`form` and `filed` grouped by `accn`, under the same point-in-time predicates as
`src/db/pit.py` (`filed <= as_of`). `loaded_filings` holds only `cik`, `accn`, `n_facts` and
`fingerprint`, so it is not a source (C4). When the facts cannot decide, the reason is
`unknown`, never a guessed cause.
- Coverage first (C4, round 2). If the company has an accession in `loaded_filings` with no
  usable rows in `xbrl_facts`, its form and filing date are unknown, and the reasons below that
  depend on the latest or a missing filing give `unknown` instead. That covers
  `foreign_filer` and `no_annual_in_stored_facts`. Stored facts describe a snapshot, not
  a complete filing inventory.
- `foreign_filer`: the company's latest accession with an annual form filed by `as_of` is a 20-F
  or 40-F. If that holds for FMX and GRAB, their issue list gains `foreign_filer`. Their status is
  already quarantine and stays so.
- `no_annual_in_stored_facts`: the stored facts filed by `as_of` include a 10-Q and no
  annual-form accession, so no TTM can be built from the stored annual base. The message says
  exactly that. It never says that no annual report was filed, or that the company listed
  recently.
- `tag_candidates`: stored us-gaap facts from the same accession and period as the newest
  balance sheet or TTM, under tags outside `concept_map` whose names fit the concept. The
  matching is a fixed pattern per concept. For debt: Debt, Notes, Borrowing, LineOfCredit or
  Loan, excluding Lease, Receivable and Asset. Each candidate is shown with its tag, value and
  accession, and marked `total` or `component` (see 2a).
- `no_cover_count`: no DEI cover count, while a 10-K or 10-Q is filed by `as_of`. Per-class
  tagging is one possible cause, not a confirmed one; 2b checks it.
- `unknown`: the facts hold no form or date evidence for the question, for example an accession
  in the ledger with no facts.
- `none_found`: the facts were enough, and nothing explains the issue; the name needs manual
  reading.

The reasons go in a new screen column, `issue_checks`, which is additive and needs no
migration. They appear in the `screen` CLI quarantine block, in a "Quarantine check" section of
the company note, and as a group in Dashboard's quarantine table.

Phase 2: resolve with evidence.
- 2a, concept map: complete totals only (C1). The resolver in `src/db/pit.py` picks one tag per
  concept and period; it never adds tags up. So a tag joins `CONCEPT_SEED` only when it is a
  complete total equivalent to the concept it joins. Each tag needs:
  - the us-gaap definition and calculation-linkbase evidence, quoted in the fixture;
  - one reviewed batch, with a fixture per tag and the live screen diff reported.

  A component tag never joins the map: a convertible-notes line, a single notes or credit-line
  balance, or any one obligation. Phase 1 shows it as a `component` candidate, and the name stays
  `missing_debt`. Adding components up needs its own completeness rule. That is out of M11's
  scope.
- 2b, cover shares per class: market cap only from a proved economic mapping (C2). For a
  `no_cover_count` name, take the latest 10-K or 10-Q filed by `as_of` and find the filing's one
  `*_htm.xml` through `index.json`, the same lookup as M10. M10 keeps no raw instance, so its
  ledger supplies nothing here; see "Requests and ledger" below.
  - Read `dei:EntityCommonStockSharesOutstanding` and `dei:TradingSymbol` in contexts with a
    `StatementClassOfStockAxis` member.
  - Store each class's count with its member, symbol, DEI date, accession and filing date in a
    new table, `cover_shares`, added by migration 9.
  - Market cap is computed only in these two cases, on the existing split basis
    (`split_factor` from the DEI date to the price date):
    - Exactly one class has a count, and the filing tags exactly one trading symbol, which is the
      priced ticker. Then market cap = that count x its close.
    - Every class with a count has its own symbol with a stored close on the same price date.
      Then market cap = the sum of count x own close.
  - A class with no symbol of its own adds no economic count. Up-C voting classes and paired
    units are such classes, and so are other unpriced classes. Their presence gives the
    blocking issue `multi_class_unverified`, with the counts shown for manual review.
  - Today `prices` stores the primary ticker only, so the second case stays blocked until
    secondary tickers are priced. Duplicate contexts for one class must agree, or the issue is
    `multi_class_unverified`.
  - `multi_class_risk` stays as a flag on top.
- 2c, debt confirmed zero. Debt becomes 0 only when all of these hold for one filing, the latest
  10-K or 10-Q filed by `as_of` (Codex, 2026-11-08). Otherwise the name stays `missing_debt`.
  - Same period: the balance-sheet column used is the one dated `bs_date`, the newest balance
    sheet the screen already uses, from that filing's own accession.
  - Same entity and scope: consolidated statements only. Contexts with any axis or member
    (segment, class, VIE, parent-only) are ignored, and parent-only statements are never used.
  - Complete, non-overlapping coverage (C3):
    - Each liability row of that column is in a fixed list of non-debt elements: payables,
      accruals, deferred revenue, operating leases, taxes and similar.
    - Each element counts once. A subtotal (`LiabilitiesCurrent`, `LiabilitiesNoncurrent`) is
      used only in place of its children, never with them. Where both appear, the children
      must reconcile to the subtotal under the same budget.
    - The rows must reconcile to `Liabilities` in the same column within a rounding budget, not
      a percentage. The budget is the sum of 0.5 x 10^(-decimals) over each row and the total,
      taken from each fact's `decimals` attribute in the instance (INF counts as 0).
    - Any residual beyond that budget keeps the quarantine, however small. So does a fact with
      no `decimals`.
    - Any other row keeps the quarantine. That includes an aggregate such as "Other
      liabilities" or "Accrued and other", any extension element, and any row matching the
      Phase 1 debt pattern. When there is no `Liabilities` total, coverage cannot be shown and
      the quarantine stays.
  - Weaker evidence is never enough on its own, alone or combined: no interest expense, a
    general "no borrowings outstanding" sentence, an empty or missing debt note, or the absence
    of debt tags. Phase 1 may show such evidence as context for manual review.
  - Sources: `FilingSummary.xml`, then the balance-sheet R page, give the rows and their order
    (each row's definition reference) and the column dates. The instance gives the values and
    their `decimals`. The result is stored with its accession, the column date and the method
    `balance_sheet_no_debt`.
  - Any debt-like or unlisted row found: the name stays `missing_debt`, and the rows are shown
    as candidates.
- Requests and ledger (C5). Documents per filing: (1) `index.json`, (2) the one `*_htm.xml`
  instance, (3) `FilingSummary.xml`, (4) the balance-sheet R page.
  - Two families, each with its own parser version and ledger row per accession in a table
    `quarantine_checks` (migration 9, with `cover_shares`):
    - `m11_cover` (2b) needs (1) and (2): 2 requests.
    - `m11_balance_sheet` (2c) needs (1) to (4): 4 requests, because the values and `decimals`
      come from the instance and the rows from the R page.
  - Eligibility (C6): only names with screen status `quarantine` (51 on 11-07). Within them,
    `m11_cover` runs only for a `no_cover_count` reason (the 11 `multi_class_risk` names on
    11-07), and `m11_balance_sheet` only for `missing_debt` or `stale_debt`. A failed name is
    never fetched, even with `multi_class_risk` (567 on 11-07).
  - Budget: at most 4 requests per filing per run, through `SecClient`. That means at most 204
    requests on a cold run (51 x 4) and 0 for filings already complete.
  - Reuse is in memory, within one `fetch-text` invocation, keyed by accession. A document
    fetched once (by M10's instance family or by either M11 family) is passed to the others and
    never fetched twice in that run. Nothing is kept on disk; raw files are not kept, as in
    M10. A later run has no cache.
  - Completion: a family is complete for a filing when its row is `ok` or `not_found` at its
    current parser version. Each family is checked and written on its own, one transaction
    per family per filing. A complete `m11_cover` never skips an incomplete
    `m11_balance_sheet`, and the other way round.
  - Retry: an `error` row (a failed request, zero or several `*_htm.xml`, no balance-sheet R
    page, malformed XML) is retried on the next run and listed in the health warnings. The
    retry makes only that family's requests: up to 4 for `m11_balance_sheet`, because no raw
    file survives the run. It never makes requests for a family already complete.
- Runs inside `fetch-text`, so the weekly job's steps do not change. Because `fetch-text` runs
  after `screen`, a resolution counts from the next weekly screen: one week of lag (open
  question 1).

Left as they are: price issues (`stale_price`, `penny_price`, `price_break`), `share_jump`,
and `foreign_filer`. Phase 1 still explains each.

Checks. The facts below are to be confirmed against SEC when the fixtures are taken:
- Phase 1 on the 11-07 live screen: a reason for every issue of all 51 names; counts per
  reason reported. FMX and GRAB gain `foreign_filer` only if their facts show a 20-F or 40-F filed by `as_of`; otherwise `unknown`. Status, rank and shortlist are identical
  with and without Phase 1.
- 2b: TSN's latest 10-K instance gives one count per class, which matches the cover-page text.
  Market cap is computed only if one of the two proved cases applies; otherwise
  `multi_class_unverified`. An Up-C filer (RSI or MIR): the voting class adds no count, so
  `multi_class_unverified`.
- 2b synthetic (C2): these give `multi_class_unverified`, never a market cap:
  - 1M class A at $10 and 1M class B at $100 with no stored class B close (the rule would have
    given $20M against $110M);
  - a voting-only paired class;
  - two disagreeing contexts for one class.

  These give a market cap: one class with one symbol; and two classes, each with its own close
  on the same date (sum of count x own close).
- 2c: CAVA and OFLX are expected to have balance sheets whose liability lines are all on the
  non-debt list and sum to `Liabilities`, giving debt 0 with the accession and column date. HUBS
  is expected to show a convertible-notes line, so it stays `missing_debt` with that line as a
  candidate. If SEC shows otherwise, the fixtures change, not the rule.
- 2c negatives, synthetic: each of these keeps the quarantine:
  - an "Other liabilities" line;
  - rows that miss `Liabilities` by more than the rounding budget, including Liabilities $100M
    against $99.1M of listed non-debt rows (the $900k residual, C3);
  - a fact with no `decimals`;
  - a subtotal counted together with its children;
  - no `Liabilities` total;
  - a balance-sheet column dated before `bs_date`;
  - a fact only in a segment or VIE context;
  - zero interest expense plus "we have no outstanding borrowings" with no qualifying balance sheet.
- 2c positive: a statement in thousands (`decimals` -3) with 6 rows plus the total reconciles
  within 7 x $500. Debt is 0 with the method recorded.
- 2a (C1):
  - Convertible notes $1M plus other notes $9M in the same accession and period, with only the
    convertible tag considered: never shortlisted on $1M. The name stays `missing_debt` with both
    rows as `component` candidates. (Codex's offline proof: mapping convertible notes as a
    `debt_total` synonym shortlisted it at net cash/mcap 0.417; the true 0.333 fails.)
  - A filer with a total and a convertible line: the total is used and the line is not added
    again.
- Phase 1 metadata (C4):
  - Annual form and date present in the facts give the reason.
  - A ledger accession with no facts gives `unknown`. So do stored 10-Q facts together with a
    ledger-only accession that might be the annual: `unknown` takes precedence over
    `no_annual_in_stored_facts`.
  - With 10-Q facts only and every ledger accession having facts, the reason is
    `no_annual_in_stored_facts`. Its message contains no claim that no annual report was filed
    or that the company listed recently.
- Requests (C5), against a mocked `SecClient`:
  - A cold 10-Q needing both families makes exactly 4 requests (index, instance,
    FilingSummary, R page), with the instance fetched once and used by both, and writes both
    rows.
  - A cold filing needing only `m11_cover` makes 2.
  - If M10 already fetched the index and instance of the same accession in that invocation, M11
    reuses both and makes only the FilingSummary and R page requests: 2 instead of 4.
  - If FilingSummary fails, the result is `m11_cover` `ok`, `m11_balance_sheet` `error` and a
    health warning. The next run makes 4 requests for `m11_balance_sheet` and 0 for
    `m11_cover`.
  - A completed filing makes 0 requests on the next invocation.
  - Eligibility (C6): a failed row with `multi_class_risk` makes 0 requests and writes no ledger
    row. A quarantined row with `no_cover_count` gets `m11_cover`. A quarantined row with only
    `missing_ebit` makes 0 requests. A 20-F filed after `as_of` does not change a
  diagnosis dated before it. Status, rank and shortlist are identical with and without Phase 1.
- Synthetic: a balance sheet with an extension element `NotesPayableToBank` is never zero. An
  index with zero or two instances, no balance-sheet R page, or malformed XML each gives an
  `error` row that is retried, reported and never zero.
- Point-in-time: a 10-Q filed after `as_of` is not used. A screen dated before a resolving
  filing is unchanged. Earlier journal snapshots are unchanged.
- A second `fetch-text` makes 0 requests for M11; a second `sync-vault` writes 0 files.
- Live run, with the database and vault backed up first: requests made, counts per ledger
  status, and every status change with its input and source. Shortlist before and after.

Not in scope: mapping IFRS (20-F/40-F) statements, resolving price issues or share jumps,
10-K/A, changing how EBIT or FCF are defined, and language models.

Open questions:
1. Accept one week of lag, or add a second `screen` after `fetch-text` in the weekly job?
   Recommendation: accept the lag. Quarantine changes slowly (12 names on all four screens), and
   the job's steps stay as they are.
2. Settled by review (C2): a multi-class market cap comes only from a proved per-class
   mapping (2b). Open: whether to price secondary class tickers later, which would unblock the
   second case.
3. Ship Phase 1 on its own first (no network, no screen change)? Recommendation: yes.

### Research R1: suggested repositories (from 2026-11-06)

GitHub's AI suggested 11 repositories to study (SEC/XBRL parsing, Form 4, DuckDB ingestion,
backtesting, Obsidian). Claude and Codex assess each one for fit, invariants, licence and cost,
with numbers on a sample: `reviews/R1-repo-candidates.md`. Research only until the M9 two-week
check ends; anything adopted becomes its own milestone.

## Order

```
M1 ──> M2 ──> M4 ──> M5
  └──> M3 ───┘  └──> M6 ──> M7
                       └──> M8
M9 after M6
M10 after M7, once the M9 two-week check passes
M11 after M10 (reuses its instance fetcher and ledger)
```

M1 and M3 can run in parallel. M5 (backtest) should finish before trusting M4's defaults.

## Risks and open questions

| Risk | Impact | Mitigation |
|---|---|---|
| yfinance is unofficial and can break or rate-limit | No prices, no screen | Wrap behind one loader; Stooq or Tiingo free tier as fallback |
| Survivorship bias from missing delisted prices | Backtest looks better than reality | Report the gap; treat results as an upper bound |
| 10b5-1 XML element unconfirmed | Plan trades counted as conviction | Verify on real filings in M3; footnote check stays as backup |
| Multi-class share counts unreliable in companyfacts | Wrong market cap | Flag multi-class filers; manual check for shortlisted ones |
| Filer tagging errors | Silent bad ratios | Quarantine rules plus accession-level traceability |
| SEC blocks traffic | Ingestion stops | Real contact in User-Agent, 8 req/s, backoff on 403 |
