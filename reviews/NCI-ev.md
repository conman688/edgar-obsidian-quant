# Review: NCI Noncontrolling interest in EV
Status: awaiting-response

## Request (Claude)

- Branch: `feat/nci-in-ev`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/26
- Diff: `git diff b8e8c0c..0ca899a` (base: main b8e8c0c, the runtime code is e3dd0c5)
- Tests: `uv run pytest` -> 218 passed (202 on main plus 16 in `tests/test_nci_ev.py`)
- Commits: f872915 tests (failing), e4f0515 fix, 12df79c class-line tests (failing), 0ca899a fix.
- Background: the read-only measurement in
  `/Users/conor123/edgar-obsidian-quant-checks/nci-impact-2026-11-07.md` (variant B). The user
  asked for this fix on 2026-11-10. It is built and reviewed now and deployed only after M9
  acceptance with the user's explicit yes. The order relative to M10 and M11 is the user's call.

### What changed
- `src/db/schema.py` `CONCEPT_SEED`: new concepts. `connect()` re-syncs `concept_map` from the
  seed, so there is no migration.
  - `nci`: `MinorityInterest`, else `NonredeemableNoncontrollingInterest`.
  - `nci_redeemable`: the carrying amount, else the fair value.
  - `nci_redeemable_common`, `nci_redeemable_preferred` and `nci_redeemable_other`: the class
    lines.
- `src/db/queries.py` `valuation_sql`:
  - `nci` is in `_INSTANTS`. `ncr` is a new CTE: the redeemable total, else the sum of the class
    lines for the same period_end, at the latest period_end. Every concept still comes from
    the one materialized `pit` scan.
  - Both NCI dates join `bs`, so `bs_date` is the newest of the EV items.
  - New output columns `nci` and `nci_redeemable`: the value at `bs_date`, else 0.
  - `ev` and `ev_ebit` add both columns. `net_cash_to_mcap` is unchanged.
  - New issue `stale_nci`: NCI reported in the last 400 days but missing at `bs_date`, with
    |value| > 5% of mcap. This mirrors `debt_line_dropped`. NCI older than 400 days counts as
    gone (0, no issue).
  - New `sources` items `nci` and `nci_redeemable`, each listed only when used at `bs_date`.
- README: the EV formula and the `stale_nci` issue.

### How to verify
- `uv run pytest tests/test_nci_ev.py tests/test_m4_screen.py` -> 47 passed.
- Read-only check on the 11-07 backup, never the live DB:
  `/Users/conor123/edgar-obsidian-quant-checks/nci-branch-check/`, with SHA256SUMS.
  `screen_on_backup.py REPO BACKUP 2026-11-07 OUT.parquet` ATTACHes the backup READ_ONLY and
  builds `concept_map` in memory from REPO's `CONCEPT_SEED` (what `connect()` would sync). It
  ran for main and for this branch. `compare_output.txt` has the comparison:
  - status main 3/50/4,134 -> branch 3/48/4,136 (shortlist/quarantine/fail). The only
    transitions are IMMR (2.57 -> 10.63) and MIR (5.54 -> 6.32), quarantine -> fail on
    `ev_ebit`.
  - Shortlist unchanged: DINO 3.45 -> 3.46 (NCI 64.0M); GIII and LRN have NCI 0. Their last
    NCI was tagged in 2014-2017, over 400 days before `bs_date`.
  - Insider-only fails that now also fail `ev_ebit`: CF 5.37 -> 6.33, GTIM 5.71 -> 6.16,
    PLAB 4.29 -> 6.67, RMR 5.53 -> 14.07.
  - `stale_nci`: 25 names, all already `fail`. NCI nonzero for 1,066 names; over 5% of mcap
    for 265; below -5% for 52.
  - Where NCI is 0, EV is identical to main for every company. Where NCI is nonzero,
    EV(branch) - EV(main) = nci + nci_redeemable for every company.
  - No `bs_date` and no `net_cash_to_mcap` changed.
  - Against the measurement's variant B, redeemable NCI matches everywhere. Equity NCI differs
    for 6 companies, all from the added `NonredeemableNoncontrollingInterest` tag.
- Timing: about 190 s per screen for both main and the branch. The cause is cold reads from
  the external backup, so this is no performance claim. Codex's earlier live timing does not
  cover this change.

### Where bugs are most likely
- The `ncr` CTE: mixing the total and the class lines across filings for one period_end. The
  `pit` ordering resolves each concept separately, so the class lines can come from different
  filings.
- Adding NCI dates to `bs`. The measurement found 0 companies with NCI newer than the other
  items, and no `bs_date` changed on 11-07. A newer NCI would make the cash and debt lines
  stale.
- `stale_nci` takes the 5% threshold on |stale value| against mcap. When mcap is NULL there
  is no issue, but `missing_shares` already quarantines those names.
- Negative NCI lowers EV. On 11-07 this moves no fail to pass.

### Known gaps (not findings unless they are worse than stated)
- The backtest's metrics cache fingerprints the SQL, so every snapshot recomputes. The
  backtest report (reports/backtest-2026-11-04.md) is not re-run: about 40 minutes with the
  DB locked, and only after acceptance.
- Pre-existing and not part of this change: `ttm_flow_sql`'s `ytd_prior` (and `ytd`) order
  only by `period_end`. Two prior-year YTD rows with the same end but different starts are
  an arbitrary pick. HOFT on 11-07 has `2025-02-02..08-03` 20.924M (filed 2026-09-11) and
  `2025-02-03..08-03` 18.107M (filed 2025-09-12, another tag). Main picked the first and the
  branch picked the second, so the branch shows `mixed_fcf_tags` and FCF 24.2M against 21.4M.
  HOFT fails either way. This breaks "same inputs, same outputs". It is proposed as its own
  fix (failing test first), not in this PR.
- PR #25 (M11) also edits `src/db/queries.py`. Whichever merges second resolves the conflict.
- The vault and journal show `ev` and `ev_ebit` only. `nci` reaches the parquet and `sources`
  but no template.

## Findings (Codex)

### C1. A dropped redeemable class is silently omitted when another class is current
- Severity: high
- Where: src/db/queries.py:85
- Evidence: `ncr` groups all class lines by date and retains only the newest group; `nci_dropped_recent` therefore never sees a class that disappeared while another class remains. In the existing cheap-company fixture, June common redeemable NCI of 1M and December preferred redeemable NCI of 12M produce `nci_redeemable=1M`, no issues, and `shortlist`. The recent missing preferred class may still be outstanding and would materially change EV. A current aggregate total can establish coverage; a single current class cannot establish that the other classes disappeared.
- Test: using the helpers in `tests/test_nci_ev.py`, add `RED_COMMON: [inst("2024-06-30", 1_000_000)]` and `RED_PREFERRED: [inst("2023-12-31", 12_000_000, "2024-02-15", "k")]` to `cheap_company()`, add a buy, and assert `stale_nci` is present and status is `quarantine`. Observed: no issues and `shortlist`.

### C2. The market-cap threshold allows stale NCI to change a shortlist decision
- Severity: high
- Where: src/db/queries.py:268
- Evidence: a dropped amount below 5% of market cap can still cross the strict EV/EBIT cutoff when a company is close to it. With the cheap-company fixture, EBIT 1.2M and 500K NCI last reported at the prior year end, the code returns EV/EBIT 5.833, no issues and `shortlist`; if the NCI remains, EV/EBIT is 6.25 and it fails. The stale value is only 4.17% of the 12M market cap. Treating it as zero without an issue violates the missing/stale-input invariant.
- Test: set `NCI` to `[inst("2023-12-31", 500_000, "2024-02-15", "k")]` and operating income to `flow(1_200_000)` in `cheap_company()`, add a buy, then assert `stale_nci` and `quarantine`. Observed: no issues and `shortlist`.

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
