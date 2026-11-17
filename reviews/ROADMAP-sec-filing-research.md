# Review: ROADMAP SEC-filing research priorities (docs only)
Status: awaiting-review

## Request (Claude)

- Branch: `docs/edgar-technique-ideas`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/31
- Diff: `git diff b8e8c0c..799cdc3 -- ROADMAP.md`. The review file itself is the next commit.
- Tests: docs only, no code change. `uv run pytest` is not affected.

### What changed
- ROADMAP: the section "Ideas E1-E12" (f74b665) is replaced by "SEC-filing research: priorities
  and ideas for later (research 2026-11-16; not planned)", with F0-F5 plus other leads, placed
  before Research R1. It reflects `edgar-obsidian-quant-checks/sec-filings-market-edge-2026-11-16.md`
  and `decision-external-reviews-2026-11-14.md` (revision 4).
  - F0: timing (S2) measured against the actual entry close, including weekend/holiday month
    ends, stale closes and same-day after-close filings. Entry, exit, rebalance, benchmark,
    dividends and costs move together; current filters are revalidated. Price and security
    identity come first. Costs (the existing 50 bps each way) and the high/low limitation.
    Survivorship: sign unknown; Form 25/15 give no returns or reasons; assumed-return results
    are scenarios, not bounds.
  - F1: 4.02(a)/(b) meaning (FRM 4610-4620); weekly observation, not an announcement trade;
    a manual, dated resolution and no automatic clearing; a frequency and coverage review first.
  - F2: insider routine/opportunistic. Prior-history-only classification, missing history =
    unknown; the 82 bp is 1986-2007 buy-minus-sell, not buy-only net; post-filing execution and
    costs are not established.
  - F3: bounded MD&A/covenant usefulness pilot that keeps numbers; M7 semantics unchanged.
  - F4: S4/S4b display items; deferred and not-for-now scores and sources.
  - F5: $0 goal, Yahoo terms, the FMP probe as dated limited observations (2026-11-16, 7 calls).
- ROADMAP risks table: the survivorship row no longer says "treat results as an upper bound".
  It now says the sign and size are unknown and points to scenarios after the F0 audit. This
  follows the agreed research correction.

### How to verify
- `git diff b8e8c0c..799cdc3 -- ROADMAP.md` shows exactly one new section (between M11's open
  questions and "Research R1") and the one risks-table row.
- Every claim matches the research note or the decision memo; no new numbers were added beyond them.
- `git merge` with `docs/idea-congress-trades` (PR #29) is clean (tested locally on 799cdc3).

### Where bugs are most likely
- An overclaim in F0-F5 wording: a bound stated where only a scenario exists, a high/low estimate
  read as covering all names, or the FMP probe read as general.
- Paraphrases of paper results: the CMP sample and long-short nature, the Lazy Prices comparison
  basis, the replication figures, the Palmrose sample.
- The changed risks row, if the user prefers to keep the old "upper bound" wording.
- The section names F0-F5 were chosen to avoid clashing with "Research R1"; check for any
  leftover R0-R5 references.

### Known gaps (not findings unless they are worse than stated)
- Docs only: no S2 measurement, 4.02 count, owner-history depth or cost model was run (no DB access).
- `acceptanceDateTime` time zone, bulk-ZIP paged-history completeness and FMP paid-plan terms
  are unverified.
- Merge waits for M9 acceptance and the user's explicit yes, together with docs #28 and #29.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
