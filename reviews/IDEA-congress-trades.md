# Review: IDEA-congress-trades Parked idea I1, congressional stock purchases (docs only)
Status: awaiting-review

## Request (Claude)

- Branch: `docs/idea-congress-trades`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/29
- Diff: `git diff <main>..<exact head from the hand-off message>` (main `a993b70`). Docs only:
  `ROADMAP.md` "Idea I1" plus this file.
- Tests: none touched (docs only).

### What changed
- Main `a993b70` merged in (clean).
- Idea I1 revised after Codex's documentation finding (2026-11-21, relayed in chat):
  - the yield test is now point-in-time: it counts only purchases whose public disclosure date
    is on or before the screen's `as_of`, with a trade date in the 90 days before it. Trade
    and disclosure dates are reported separately;
  - PTRs are distinguished from Form 4 (who files, about what). They never count toward the
    insider-buy rule;
  - the issuer-mix and published-returns points are marked as unverified assumptions, with no
    study cited, and a revival must source them;
  - "after M9 acceptance" became "its own milestone and the user's yes"; it approves no pilot
    or build.

### How to verify
- `git diff a993b70..<head> -- ROADMAP.md`: only the Idea I1 section changes.

### Where bugs are most likely
- Factual statements kept: the 45-day PTR limit, Form 4 within 2 business days, ranges for
  amounts, the House and Senate sources and their access. They are stated from earlier
  research, not re-checked in this round.

### Known gaps
- No sources are cited for the two marked assumptions, on purpose.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
