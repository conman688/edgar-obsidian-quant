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
also down 50%. M5 should test looser defaults.

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

### M5 Backtest harness (size: L)

- Monthly rebalance from 2012 to today using `as_first_reported` and `filed <= rebalance date`.
- Forward 3, 6 and 12-month returns vs equal-weight universe, with costs.
- Report hit rate, excess return, max drawdown, turnover and the survivorship gap from M2.
- Splits the history into halves; a screen that only works in one half is flagged.

Checks:
- A deliberately leaky version (using `period_end` instead of `filed`) scores better than the real one. This proves the harness can detect look-ahead.
- Report written to `reports/backtest-YYYY-MM-DD.md` and to the vault.

### M6 Obsidian vault sync (size: M)

- Jinja templates: `company.md.j2`, `executive.md.j2`, `filing.md.j2`, `event.md.j2`.
- Folders: `Companies/`, `Executives/`, `Filings/`, `Events/`, `Journal/`.
- Frontmatter keys fixed for Dataview: `ticker`, `cik`, `ev_ebit`, `net_cash_to_mcap`, `share_cagr_3y`, `insider_buy_90d_usd`, `drawdown_52w`, `as_of`.
- `Dashboard.md` with Dataview tables: shortlist, new insider buys, reviews due.
- `cli.py sync-vault` only writes notes whose content changed.

Checks:
- Second sync reports 0 files written.
- Edit a note by hand, re-sync, edit survives.
- 3 name variants of one Form 4 filer produce one Executive note.

### M7 Text forensics (size: M)

- Shortlist only, so request volume stays small.
- YoY redline of Risk Factors and key footnote text blocks: sentence split, numbers and dates
  replaced with placeholders, a sentence counts as new below 0.8 similarity.
- Loughran-McDonald word lists for negative, litigious and uncertainty tone change. Check its licence terms before use.
- Executive pay from DEF 14A pay-versus-performance tags (`ecd:` via edgartools), plus the company-selected measure.
- Impairments, acquisitions, buybacks and debt maturity walls from facts already loaded in M1.

Checks:
- A 10-K diffed against itself yields 0 new sentences.
- A changed number alone does not mark a sentence as new.

### M8 Decision journal (size: S)

- `cli.py journal TICKER` creates `Journal/TICKER-YYYY-MM-DD.md` from a template:
  thesis, what the market believes, why it is wrong, kill criteria, position size, review date.
- Snapshot of the screen metrics frozen into the note at decision time.
- Dashboard lists reviews due and compares then-vs-now metrics.

Checks:
- Journal snapshot does not change when the underlying data later updates.

### M9 Operations (size: S)

- Weekly `launchd` job: ingest, screen, sync. Daily job: Form 4 only.
- Health report: row-count drift, failed ledger entries, stale prices, SEC throttling events.
- DuckDB backup before each weekly run; keep the last 4.
- GitHub Actions runs the tests on every push.

Checks:
- Two unattended weeks with no manual fixes.
- A forced crash mid-run resumes without duplicates or wasted refetches.

## Order

```
M1 ──> M2 ──> M4 ──> M5
  └──> M3 ───┘  └──> M6 ──> M7
                       └──> M8
M9 after M6
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
