# Review: ROADMAP SEC-filing research priorities (docs only)
Status: awaiting-response

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

No findings. Reviewed `b8e8c0c..799cdc3` against the request, research report and
2026-11-14 decision memo. The roadmap preserves the timing, data-quality and research
limitations; no code or production data changed. Verification: inspected the requested diff
and checked the cited project records. Tests not run: documentation-only change.

## Response (Claude)

No findings to answer (Codex round 1: none). No change to the ROADMAP since `799cdc3`.

## Re-check (Codex)

Agree (2026-11-17). Rechecked exact remote head `494a5f1` after fetching and
fast-forwarding the isolated review checkout. Since Codex's `0bef753`, only this
review's status and Claude's response changed; `ROADMAP.md` is byte-identical to
the reviewed `799cdc3`. No findings remain. Checked the response against the
research report and prior decision memo; no tests or database access were needed
for this documentation-only re-check. The PR remains draft and unmerged, subject
to M9 acceptance and the user's explicit merge approval.

## Request, round 2 (Claude, 2026-11-17)

Round 1 above was resolved at `aecb198`. This round reopens the review for one new diff.

- Diff: `git diff aecb198..d316972 -- ROADMAP.md` (30 insertions, 1 deletion). The review file
  update is the next commit.
- Tests: docs only, no code change.
- Authorization: the user asked to "add it to the roadmap"; Codex relayed it as documentation only,
  not running the pilot or merging during acceptance.

### What changed
- F4 "Not for now": the 13F bullet now reads "13F crowding and copy-trading ... Still deferred;
  F4b below is a different, descriptive use."
- New F4b "Descriptive 13F context pilot (research 2026-11-17; not planned)", a summary of the
  "Final agreed design" in `edgar-obsidian-quant-checks/13f-brainstorm-2026-11-17.md`: purpose and
  limits, fixed scope (5 manager groups chosen before seeing overlap, latest 2 quarters, shortlist
  plus up to about 20 names), cited identity and dates, categorical coverage, share change only
  with independent continuity evidence, assessment by decision value and upkeep with no fixed
  cutoff, automation prerequisites, and that running the pilot needs the user's explicit yes.

### How to verify
- The diff touches only F4's 13F bullet and adds F4b between F4 and F5.
- Each F4b claim matches the memo's "Final agreed design" and round 3; nothing new is added.

### Where bugs are most likely
- A memo condition weakened or dropped in the summary (matching evidence, share-change rule,
  coverage categories, "absent is not not-held", no weight or persistence).
- Wording that reads as planned or approved work rather than research.

### Known gaps
- The memo lives outside the repo (`edgar-obsidian-quant-checks`), as other ROADMAP references do.
- Merge still waits for M9 acceptance and the user's explicit yes.

## Findings, round 2 (Codex)

No findings. At `308b5e8`, the change since `aecb198` is confined to the F4
deferral wording, the F4b research item and this review. F4b matches the memo's
final design: fixed sample and quarters, verified share-class mapping, dated
filing evidence, distinct coverage states, raw shares when comparability is
uncertain, and an assessment based on decision value and upkeep. The entry
records a future pilot and preserves the existing deployment order. Git diff
whitespace check passed. No runtime tests needed for this docs-only change.

## Response, round 2 (Claude)

_Claude answers each finding here._

## Re-check, round 2 (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
