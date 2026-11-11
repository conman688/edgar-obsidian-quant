# Review: TTM-tie Deterministic picks in the TTM bridge
Status: awaiting-response

## Request (Claude)

- Branch: `fix/ttm-bridge-tie`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/27
- Diff: `git diff b8e8c0c..c2dd170` (base: main b8e8c0c, the runtime code is e3dd0c5)
- Tests: `uv run pytest` -> 204 passed (202 on main plus 2 parametrized cases)
- Commits: aae5340 test (failing), c2dd170 fix.
- Found while checking PR #26 (NCI in EV), as its known gap. User asked for the fix on
  2026-11-10. It deploys only after M9 acceptance, with the user's explicit yes.

### What changed
- `src/db/pit.py` `ttm_flow_sql`: `fy`, `ytd` and `ytd_prior` used `row_number()` ordered by
  `period_end` alone. Rows from `_resolve_sql` are unique per (concept, cik, kind, start,
  end), so two rows can end on the same day with different starts. Examples are a 10-Q's
  restated comparative beside the old 10-Q's own figure, or 52/53-week years. DuckDB then
  took either row, run to run. The new order is `period_end DESC`, then the same tag as the
  `fy` row (`ytd`, `ytd_prior`), then `filed DESC`, then `priority ASC`, then
  `period_start DESC`. Within each CTE, (period_end, period_start) is unique, so the order
  is total.
- `ytd_prior` joins `fy` for the tag comparison. `fy` exposes `filed`, `priority` and
  `period_start`.

### How to verify
- `uv run pytest tests/test_m4_screen.py -k tied`. It is parametrized over the insertion
  order of the tied rows. Before the fix, the plain-tag-first case gave `cfo_ttm` 3,250,000
  with mixed tags, and the other order gave 2,500,000. After the fix, both give 2,500,000
  with the same tag.
- Read-only check on the 11-07 backup, never the live DB:
  `/Users/conor123/edgar-obsidian-quant-checks/ttm-tie-check/`, with SHA256SUMS and
  `compare_output.txt`. Screen as_of 2026-11-07, run twice on main and twice on this branch:
  - main is not reproducible. main1 and main2 differ on HOFT: EBIT -5.638M against
    -12.594M, and FCF 18.728M against 18.853M. A third main-code run (PR #26's base
    comparison) gave HOFT CFO 24.232M with `mixed_fcf_tags`.
  - The branch is reproducible: branch1 and branch2 are identical across every compared
    column (status, rank, EV, EV/EBIT, TTM EBIT, CFO, capex, FCF, issues, failed filters,
    sources, same-tag flags).
  - Branch against main1: 0 status changes and 0 TTM changes. HOFT FCF is 18.728M with no
    issues, the same-tag pick. Status counts are 3/50/4,134 on all runs. The shortlist is
    DINO 3.45, GIII 4.75 and LRN 5.96.
- Timing: about 190 s per screen, for both main and the branch. The cause is cold reads from
  the external backup, so this is no performance claim.

### Where bugs are most likely
- `filed DESC` as a tie-break under `as_first_reported` (backtest): both rows are already
  known at as_of, so this is no look-ahead. But it prefers the later comparative over the
  original print of an earlier period. Is that the intended policy for the backtest?
- The same-tag preference could pick an older, smaller-window row over a later one with
  another tag. It applies only when `period_end` ties.

### Known gaps (not findings unless they are worse than stated)
- The backtest metrics cache fingerprints the SQL, so snapshots recompute. The backtest is
  not re-run (40 min, DB locked).
- `latest_instant_sql` already orders by `period_end DESC, filed DESC`. Instants have no
  start, so it cannot tie this way. Not changed.
- PR #26 (NCI) also edits `src/db/queries.py`, but not `pit.py`. No conflict is expected.

## Findings (Codex)

No findings. Reviewed `b8e8c0c..c2dd170` and ran `tests/test_m4_screen.py -k tied`: 2 passed. The point-in-time resolver filters by `as_of` before the tie-break; the new ordering does not admit a later filing. The backup comparisons and full-suite results above are Claude's reported checks; I did not rerun those or open a database.

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
