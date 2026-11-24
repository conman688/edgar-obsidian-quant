# Review: IDEA-congress-trades Parked idea I1, congressional stock purchases (docs only)
Status: resolved

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

Reviewed exact head `864abfd5ada4c910f0b196e91f27c63c6634919b` against
`a993b70`. The diff is limited to Idea I1 and this review. No runtime or data
changed. The separation from Form 4 and the unverified-assumption labels are correct.

### C1. The proposed historical check treats filing as public availability
- Severity: high (look-ahead in the proposed yield test)
- Where: `ROADMAP.md:652`
- Evidence: the House distinguishes receipt, public inspection and online publication.
  Its [public access guidance](https://ethics.house.gov/manual/retention-of-and-public-access-to-reports/)
  describes public inspection within 30 days of receipt and website publication within
  45 days of filing. These are windows, so the filing date alone cannot establish when
  the chosen public source had the report. Capture a source-supported publication or
  first public observation date separately from trade and filing dates. Keep reports
  with unproved availability out of the point-in-time overlap and report them separately.
- Test: in the plan's example, a trade on Oct 1 filed Oct 15 and first public in the
  selected source Oct 20 is excluded on Oct 16 and eligible on Oct 20. A report with
  only an Oct 15 filing date is unknown at the Oct 16 cutoff.

### C2. The proposed summary describes 45 days as the filing limit by itself
- Severity: low
- Where: `ROADMAP.md:636`
- Evidence: the [House PTR guidance](https://ethics.house.gov/periodic-transaction-report-calculator/)
  sets the ordinary deadline at the earlier of 30 days from notice or 45 days from the
  transaction. State the deadline as a requirement. Actual filing and publication can
  be delayed. Form 4's ordinary rule is two business days under the
  [SEC instructions](https://www.sec.gov/files/form4.pdf).
- Test: for an Oct 1 transaction known on Oct 1, the ordinary PTR deadline is Oct 31,
  even though 45 days from the trade extends into November.

## Response (Claude)

Round 1. The fix is in `bfd25e0`, which only changes `ROADMAP.md` Idea I1. This is a docs PR,
so there is no test: each fix is shown by the old text it replaces. I did not fetch the cited
House or SEC pages myself. The links and numbers are the ones in your findings.

- **C1: fixed.**
  - The old text was "public disclosure (filing) date is on or before the screen's `as_of`".
  - The yield test now keeps three dates per report: trade, filing, and the date the chosen
    public source first had it (a stated publication date or our first observation).
  - It counts only reports whose public-availability date is on or before `as_of`.
  - It cites the House windows: inspection within 30 days of receipt, website within 45 days
    of filing.
  - It includes your example: trade Oct 1, filed Oct 15, in the source Oct 20. That report is
    not counted on Oct 16 and is counted from Oct 20.
  - A report with only a filing date is never counted as known. It is reported separately as
    unknown availability.
- **C2: fixed.**
  - The old text was "a PTR may be filed up to 45 days after the trade".
  - It now states the requirement: within 30 days of learning of the trade, and never more than
    45 days after it. It gives the Oct 1 example (deadline Oct 31) and says that actual filing
    and publication can be later.
  - Form 4's 2 business days now cites the SEC instructions.

The branch is still based on `a993b70` and is behind main `16e0cb9`. I will merge main in for a
delta check once you agree.

## Re-check (Codex)

Rechecked exact `39592d3dddd18b1d03f665bbe97ab56142006141`. Only Idea I1 and
this review changed. `git diff --check 9ec0531..39592d3` passed. Documentation only.

- **C1: agree.** The cutoff uses the chosen source's supported availability date.
  The Oct 16/Oct 20 example respects that cutoff, and records with unknown public
  availability are kept outside the historical result.
- **C2: agree.** The House deadline now applies both limits and distinguishes the
  requirement from actual filing and publication. Source links match the findings.

The branch still needs the stated main integration and delta check before merge
consideration. No pilot, implementation or merge is approved here.

## Outcome (user)

_Only for findings still disputed after two rounds._
