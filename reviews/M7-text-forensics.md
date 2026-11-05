# Review: M7 Text forensics
Status: awaiting-review

## Request (Claude)

- Branch: `feat/m7-text-forensics`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/13
- Diff: `git diff d338ac7..64ac550` (code: 97efe3f, tone in 64ac550; the review commits touch only this file)
- Tests: `uv run pytest` -> 144 passed (`tests/test_m7_text.py`: 24)

### What changed
- `src/ingestion/filing_text.py` (new): `html_text` (block tags become lines, source line breaks
  become spaces, `script`/`style`/`ix:header`/`display:none` skipped), `risk_factors` (longest
  span from an `Item 1A` line to the next `Item 1B/1C/2` line, title stripped), `annual_reports`
  (10-K and 10-KT from the submissions JSON, filed <= as_of, newest first), `fetch_sections`
  (latest two per company, skips accns already in the table, stores `ok` or `not_found`).
- `src/db/schema.py`: migration 6, `filing_sections` (PK accn + section), also the fetch ledger.
  No concept_map change: forensics reuse the ladders already in `CONCEPT_SEED`
  (`buybacks_paid`, `acquisitions_paid`, `goodwill_impairment`, `asset_impairment`, `debt_due_*`),
  so the backtest fingerprints are unchanged.
- `src/redline.py` (new): `sentences` (abbreviations masked, lines joined when the next starts in
  lower case or the previous ends with a comma, page numbers and "Table of Contents" lines dropped,
  fewer than 4 words dropped), `normalize` (dates -> `<date>`, numbers -> `#`, letters only),
  `redline` (new = no prior sentence at >= 0.8 word-level difflib ratio among the 12 sharing the
  most words; exact matches first; repeats listed once; removed the same way reversed),
  `risk_factor_redline` (latest two rows filed <= as_of; statuses ok / no_prior / no_section /
  no_prior_section).
- `src/forensics.py` (new): `forensics_sql` (latest fiscal year = latest annual `cfo` period end;
  flows of exactly that annual period, maturities as instants at that date), `forensics`,
  `assess` (buyback yield, acquisitions / mcap, flags `debt_wall`: due within 2 years > cash_like,
  `acquisitive`: >= 10% of mcap, `impairment`: one line >= 5% of mcap; none when stale > 470 days).
- `src/obsidian/sync.py`: `vault_ciks` (companies a sync would write, for `fetch-text`); redline,
  forensics, 10-K and forensics filings added to `filing_link`/Filings notes; company frontmatter
  `rf_new_sentences`, `forensic_flags`. Template: two new sections.
- `src/cli.py`: `fetch-text --as-of DATE [--vault PATH] [--cik N ...]`.
- Tone (commit after the request, user approved the licence): `src/tone.py` (`load_lists`: words
  whose Negative/Litigious/Uncertainty column is a positive year, i.e. currently listed;
  `tone`: share of words per list), `risk_factor_redline(..., lists)` adds
  `tone = {list: (this year, prior)}` for ok redlines; `config.LM_DICT`
  (`data/lm/Loughran-McDonald_MasterDictionary_1993-2025.csv`, not in git); note table and
  frontmatter `tone_<list>_change`. Missing file: the note says so, nothing fails.
- Docs: README (vault section, layout, invariant), AGENTS.md (invariant, ledger), ROADMAP (result).

### How to verify
- `uv run pytest` -> 140 passed.
- Live, only with my OK (it writes to the database, the vault, and makes SEC requests):
  `uv run --env-file .env python -m src.cli fetch-text --as-of 2026-11-03` -> 0 found, 0 not found
  (all 38 sections already stored); `sync-vault --as-of 2026-11-03` -> 0 written, 781 unchanged.
  Reading the vault and querying `filing_sections` read-only is fine (one process at a time).

### Where bugs are most likely
- Extraction: `risk_factors` on 10-Ks that put Item 1A in an exhibit, use "Item 1A" mid-line,
  split the heading across elements, or have a long table of contents. Text from nested
  `display:none` and void tags in `_Text`.
- Redline: sentence splitting (abbreviations, bullets, headings joined to text), the line-join rule
  (a heading ending with a comma; a lower-case line that is a new item), the candidate cut
  (`CANDIDATES = 12`) missing the true match, `normalize` masking too much or too little.
- Point-in-time: `annual_reports` uses `filingDate`; `risk_factor_redline` uses `filed <= as_of`;
  a 10-K fetched for a later as_of must not change the redline of an earlier as_of beyond adding
  the filings known then. `forensics_sql` reference year from `cfo` only.
- Forensics semantics: a missing tag in the latest year shows "-" (unknown), not zero;
  `due_soon` vs cash only (no revolver or FCF); `impairment` uses the larger of two lines.
- Idempotence and determinism of the new note sections and Filings notes.

### Known gaps (not findings unless they are worse than stated)
- Executive pay and footnote text blocks are deferred (ROADMAP M7). Tone counts words only:
  no negation handling, and numbers do not count as words.
- 20-F/40-F filers get no redline; IFRS filers get no forensics (the `cfo` ladder is US-GAAP).
- `fetch-text` is a separate command; `sync-vault` never makes network requests.
- `fetch-text` stores only the latest two 10-Ks per run date; a backtest over text is out of scope.
- Sentences are shown in full and are not escaped for Markdown.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
