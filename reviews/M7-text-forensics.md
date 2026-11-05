# Review: M7 Text forensics
Status: awaiting-recheck

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

### C1. A hidden void element suppresses the remainder of a filing
- Severity: high
- Where: src/ingestion/filing_text.py:41-48, 58-68 and 130-137
- Evidence: HTMLParser calls handle_starttag for ordinary HTML void tags such as <img> without an end tag. If one has style="display:none", the parser pushes it onto skip and never pops it, so all subsequent text is discarded. Reproduced with a hidden image before a valid Item 1A: html_text returns an empty string and risk_factors returns None. fetch_sections then permanently records not_found and skips that accession on later runs, so fixing the parser alone will not repair affected cached rows. Handle void elements without entering a skip scope and provide a safe way to retry affected not_found records.
- Test:
```python
def test_hidden_void_element_does_not_hide_following_risks():
    raw = (b'<html><body><img style="display:none" src="pixel">'
           b'<h2>Item 1A. Risk Factors</h2><p>We face serious operational risks.</p>'
           b'<h2>Item 1B.</h2></body></html>')
    assert "We face serious operational risks." in risk_factors(html_text(raw))
```
Also test reprocessing an affected cached not_found row.

### C2. Missing cash is treated as zero for the debt-wall warning
- Severity: high
- Where: src/forensics.py:67-76
- Evidence: assess uses row.get("cash_like") or 0.0, so an unknown cash balance is indistinguishable from zero. Reproduced with cash_like=None, first-year debt 100, and mcap 1000: it returns the debt_wall flag although the comparison cannot be made. Missing maturity buckets similarly result in an incomplete total and an unqualified absence of the flag. This violates the missing-input invariant and can mislead the user about the company's refinancing risk. Preserve unknowns and explicitly report when the comparison cannot be made; only assert the warning when supported by known data.
- Test:
```python
def test_unknown_cash_does_not_claim_debt_exceeds_cash():
    f = {"stale": False, "debt_due": [100, None, None, None, None, None],
         "goodwill_impairment": None, "asset_impairment": None,
         "acquisitions": None, "buybacks": None}
    result = assess(f, {"mcap": 1000, "cash_like": None})
    assert "debt_wall" not in result["flags"]
    # Also assert an explicit unknown status for incomplete inputs.
```

Verification: tests/test_m7_text.py: 24 passed. Both cases were reproduced independently; no live database, network fetch, or vault write was used. This review did not validate live SEC extraction or the live vault.


## Response (Claude)

Both reproduced with the reviewer's tests on a84ca3d (failed: risk_factors returned None; the
debt_wall flag was raised with unknown cash), fixed in d388f69. Suite: 149 passed (M7: 29).

### C1: fixed
- Test: tests/test_m7_text.py::test_hidden_void_element_does_not_hide_following_risks (failed:
  `None`; passes). Also asserts a hidden `<br>` between paragraphs keeps both.
- Change: d388f69 `_Text.handle_starttag` handles void elements (`img`, `br`, `hr`, `input`, `meta`,
  ...) before the skip check, so they never open a skipped scope (`br`/`hr` still break lines).
- Cached rows: migration 7 adds `filing_sections.parser`; `PARSER = 2`. `fetch_sections` treats
  a row as done only when `parser >= PARSER`; any other row in the latest two 10-Ks is fetched
  once more and replaced (`INSERT OR REPLACE` on accn + section), whatever its status, since an
  `ok` row could also have been cut short. Test:
  tests/test_m7_text.py::test_sections_cut_by_an_older_parser_are_fetched_again_once (a
  not_found row with no parser version is re-fetched to ok with parser 2, one document request,
  no duplicate row; the next run requests only the submissions index). It failed before the fix
  (no `parser` column).
- Live: all 38 stored rows predated the column and were re-fetched once (22 s); a third run
  downloaded nothing. 34 rows came back identical; 4 (GIII and LRN, both years) are 10 to 332
  characters shorter. Those were cut in my first trial run, before zero-width spaces were
  stripped, not by this bug; no row was or is `not_found`. Redline counts unchanged
  (GIII 44 new, LRN 73 new). `sync-vault`: 52 company notes written (new key), then 0.

### C2: fixed
- Test: tests/test_m7_text.py::test_unknown_cash_does_not_claim_debt_exceeds_cash (failed: flag
  raised; passes, `unknown == ["debt_wall"]`). Added
  tests/test_m7_text.py::test_a_missing_maturity_bucket_makes_the_debt_wall_unknown_unless_known_debt_suffices
  (year 2 missing: unknown when known debt <= cash, flag when known debt alone > cash, neither
  when both buckets are known) and
  tests/test_m7_text.py::test_note_says_when_the_debt_wall_cannot_be_checked (note line and
  frontmatter `forensic_unknown`).
- Change: d388f69 `assess` keeps unknowns: `debt_wall` only if cash is known and the known debt due
  within 2 years exceeds it (true whatever a missing bucket holds); otherwise, if cash or a
  bucket is missing, `unknown = ["debt_wall"]`. `due_soon` is None unless both buckets are known.
  The note says "Debt due within 2 years against cash: unknown (...)"; frontmatter
  `forensic_unknown`.
- Live: 11 of the 20 companies with forensics now say the check cannot be made; none had a
  false `debt_wall` before (the only live flag was 1 `acquisitive`).
- Not changed: `acquisitive` and `impairment` still read a missing tag as "none this year". These
  tags are filed only in years with an acquisition or impairment, so a missing tag in the
  latest 10-K is the normal "nothing happened"; marking it unknown would mark nearly every
  company. The note shows "-" for those values, not $0. Say if you want them treated as unknown.

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
