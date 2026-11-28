# Review: FIX-ttm-bridge-tiebreak Deterministic TTM bridge tiebreak (HOFT mixed_fcf_tags flip)
Status: awaiting-response

## Request (Claude)

- Branch: `fix/ttm-bridge-tiebreak`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/45 (draft)
- Diff: `git diff d789faa..fix/ttm-bridge-tiebreak` (base: main `d789faa`)
- Commits: `eb04a1d` test (failing first), `090b55f` fix, then this file.
- Tests: `uv run pytest` -> 421 passed (419 on main, plus 2 cases).
- Found while checking M12-B6 (PR #44): HOFT's `issues` changed between runs on identical data,
  on main `bac4dcd` and `d789faa` too. Not part of B6. It breaks the AGENTS.md invariant "Same
  inputs, same outputs".

### Cause
- `ttm_flow_sql` (`src/db/pit.py`) took each bridge leg (fy, ytd, ytd_prior) with
  `row_number() OVER (PARTITION BY cik ORDER BY period_end DESC)`. `_resolve_sql` keeps one row
  per `(concept, cik, period_kind, period_start, period_end)`, so two rows with the same
  `period_end` and different `period_start` tie. Row order then decides the pick.
- HOFT, cfo, prior-year H1. Both rows end 2025-08-03:
  - 2026 10-Q `0001185185-26-003941`: restated comparative from 2025-02-02, tag
    `...ContinuingOperations`, 20,924k.
  - 2025 10-Q `0001185185-25-001179`: original from 2025-02-03, tag plain
    `NetCashProvidedByUsedInOperatingActivities`, 18,107k.
  - Both fall inside the 355-375 day window. The pick flips cfo_ttm (21,415k vs 24,232k) and
    `same_tag`, so `mixed_fcf_tags` comes and goes. capex has the same tie (2,687k vs 2,562k), and
    so does ebit, with only `accns` changing under `as_last_known`.

### What changed
- `src/db/pit.py` `ttm_flow_sql`: all three legs order by `period_end DESC`, then `filed` and
  `accn` in the policy's direction (DESC for `as_last_known`; ASC for `as_first_reported` and
  `leaky_period_end`), then `period_start DESC`. Within one concept and cik, `(period_start,
  period_end)` is unique in `_resolve_sql`'s output, so the order is total. This follows
  `_resolve_sql`'s rule: the policy picks the filing.
- `tests/test_failure_modes.py::test_ttm_bridge_tie_follows_policy_not_row_order`: HOFT-shaped
  fixture, both insertion orders, `SET threads` 1 and 4, both policies. `as_last_known` must take
  the restated row (21,415, same_tag, `[k26, q26, q26]`); `as_first_reported` must take the
  original (24,232, mixed, `[k26, q26, q25]`). Both cases fail on `d789faa`.
- `tests/test_m12_insider_sells.py`: the `metrics_sql` sha256 pin is updated on purpose (see the
  cache impact below).

### How to verify
- `uv run pytest -q tests/test_failure_modes.py -k tie_follows` -> 2 passed (2 failed on `d789faa`).
- Live data: scratch copy of backup `edgar-2026-11-21.duckdb`, `screen(con, date(2026, 11, 20))`,
  10 runs: HOFT `issues == []`, `fcf_ttm == 18,728,000` (cfo 21,415k minus capex 2,687k), status
  `fail`, 4,203 rows, in all 10 runs. Before the fix: 2 of 6 probe runs gave `[]` and 4 gave
  `['mixed_fcf_tags']`, with cfo_ttm and capex_ttm moving too.
- Main vs fix, one `metrics_sql` run each on the same scratch copy at 2026-11-20 (ebit/cfo/capex
  /fcf TTM, same_tag flags, issues, status, rank): `as_last_known` 0 of 4,203 companies differ;
  `as_first_reported` 1 differs (HOFT ebit_ttm -12,594k on main vs -5,638k here). Main's result
  depends on the run, so this is one sample.
- Leg ties that exist at 2026-11-20 (`as_last_known`; first-reported matches, except 8 cfo
  ytd_prior value splits): fy cfo 7, fy ebit 11, ytd capex 5, ytd_prior capex 24, cfo 41,
  ebit 35. In 30 of those ties the candidate values differ. In 8 the candidate tags differ.
  Before the fix, each of those was a coin toss that usually landed the same way.

### Backtest cache impact
- The snapshot fingerprint (`src/backtest.py` `_fingerprint`) hashes the `metrics_sql` text. The
  text changes for every policy, so every cached snapshot recomputes once on the next backtest.
  The live cache holds 178 `as_first_reported` and 178 `leaky_period_end` snapshots (2012-01-31
  to 2026-10-31): 356 of 356 fingerprints change. No `as_last_known` snapshots are cached.
- How many historical snapshot rows change value is not measured. The recompute itself will show it.

### Where bugs are most likely
- Policy direction for `leaky_period_end`: it uses the first-reported direction (ASC), the same
  as `_resolve_sql`.
- The `ytd` leg: `pick('c')` qualifies every column with `c.`, because `fy` also has `period_end`.
- Whether "latest filing" is the right tiebreak for `as_last_known` when the restated
  comparative uses a different tag from the original. It picks the restated tag, which here
  matches the current-year tags (same_tag TRUE).

### Known gaps (not findings unless they are worse than stated)
- Run-to-run differences remain in the full screen output. With this fix, 9 of 10 full-screen
  digests differed. The cause is a one-cent jitter in insider USD sums
  (`round(sum(usd), 2)` on DOUBLE, `src/db/queries.py:394-403, 439`): for example
  `insider_buy_90d_usd` 1296103.18 vs 1296103.17, and 10 values across 7 companies in 3 runs.
  Rounding does not fix values near a half cent. That is a separate fix (sum as DECIMAL); it is
  not in this PR.
- `share_trend_sql` (`src/db/queries.py:35`) also ranks annual rows by `period_end DESC` alone.
  It has the same kind of tie and is not changed here.
- Unsorted `list(DISTINCT ...)` outputs (`src/screen.py:128` items_8k, `src/db/queries.py:394,405`
  buyer/seller_ciks, `:469` indirect_vehicles) break "including list order". In 3 runs none
  differed, but nothing guarantees it.

## Findings (Codex)

No findings in the TTM bridge change. Reviewed `d789faa..51d7f7b526f049c51423b51e098ebc96cf8e6c6d`.

- Both new insertion-order cases failed independently at `eb04a1d`: first-reported picked the
  later comparative (`21,415`, same tag) instead of the original (`24,232`, mixed tags). On the
  reviewed head, 56 focused failure-mode, M12 insider-sell and backtest fixture tests passed.
- The new ordering preserves the existing period and as-of filters. Filing order follows each
  policy, including the leaky control's first-reported order, and the input's date spans determine
  its duration bucket. The SQL fingerprint changes, so the next normal backtest recomputes its cache.
- Direct read-only November 21 backup check at `as_of=2026-11-20` for HOFT only: last-known CFO
  was $21.415M with matching tags and comparative `0001185185-26-003941`. First-reported and the
  leaky control were $24.232M with mixed tags and original `0001185185-25-001179`. Used `fetchall()`
  with a 2 GB cap and closed the backup. The live DB and full backtest were not opened or run.
- The branch still uses `d789faa` as its base. A temporary combined test tree with GitHub main
  `d716ed2` passed 126 focused tests, including 13F and 13D/13G. Please bring the branch up to date
  and hand back the resulting exact head through the response step. Existing separate output-order
  and rounding issues remain outside this diff.

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
