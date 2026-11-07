# Review: M10 Footnote checks (plan)
Status: awaiting-review

## Request (Claude)

- Branch: `docs/m10-footnotes-plan`  PR: (see GitHub, docs only)
- Diff: `git diff 9274134..HEAD -- ROADMAP.md` (new section "M10 Footnote checks", one line in "Order")
- Tests: none; this round reviews a plan, not code. No code is written before the M9 two-week
  check passes (about 2026-11-19).

### What changed
- ROADMAP gains M10: going-concern, related-party and debt-default checks for the vault's 10-K
  filers, built on M7's `fetch-text`, `filing_sections` and redline; acceptance checks; scope
  limits; two open questions.

### How to verify
- Facts in the plan, checked 2026-11-07 with the project's User-Agent:
  - GIII 10-K 0001104659-26-033891 `index.json` lists `giii-20260131x10k_htm.xml` (3,202,946 bytes).
  - DarkPulse 10-K 0001683168-26-002937 (CIK 866439): instance `darkpulse_i10k-123125_htm.xml`
    (1,094,734 bytes) has `us-gaap:SubstantialDoubtAboutGoingConcernTextBlock` and
    `RelatedPartyTransactionsDisclosureTextBlock`. Found via EDGAR full-text search for the
    going-concern phrase in 10-Ks filed in 2026 (193 hits).
  - Frames `us-gaap/DebtDefaultLongtermDebtAmount/USD/CY2025Q4I`: 14 companies;
    `DebtDefaultShorttermDebtAmount`: 5; `DebtInstrumentDebtDefaultAmount`: no frame (404), so
    left out.
  - Live DB (read-only, no run holding the lock): 25 companies / 49 10-Ks with Risk Factors in
    `filing_sections`; 360 companies, 2,384 facts with a debt-default amount in `xbrl_facts`.
- GIII text lengths (1,484; 6,777; first part 908) are from Codex's R1 round-1 check.

### Where bugs are most likely
- Going-concern status wording: anything that reads as "no warning" when nothing was found.
- Point-in-time: default amounts and 10-Ks filed after `as_of`.
- The `PARSER` bump refetching more than the intended 49 documents.
- Keyword matching on debt text flagging boilerplate.

### Known gaps (not findings unless they are worse than stated)
- 10-Q, 20-F/40-F, 10-K/A, executive pay and 8-K events are out of scope (listed in the plan).

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
