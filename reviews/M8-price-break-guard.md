# Review: M8 price-break guard for journal returns
Status: resolved

<!-- Recorded by Claude on 2026-11-06 at the user's request. Until now this review lived only in the
     untracked shared file reviews/GEMINI-TASKS.md on the user's Mac. Sections below are copied
     from that file word for word, under the name of the agent who wrote them. Codex's two
     recheck findings (rounds 1 and 2) reached Claude in chat; they are restated in Claude's
     answers. Merge waits for the M9 two-week unattended check (until about 2026-11-19); PR #15
     stays a draft until then. -->

## Request (Gemini)

- Branch: `feat/m8-price-break-guard`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/15
- Built by Gemini (no network in its sandbox; Claude pushed the branch). Gemini did not start the
  C1 fix, so Claude took the task over on 2026-11-06 at Codex's request.

#### M8: Guard journal returns against broken price series (`feat/m8-price-break-guard`)

- **Worktree:** `/tmp/edgar-gemini-price-break`
- **Branch:** `feat/m8-price-break-guard`
- **Base commit:** `4c3895aa25fa9b0d5f9693bc9ce596e53cb6356d` (`main` after M8 merge)
- **Head commit:** `a9664828be2c7d715197b4b3ad7c1352d2216ff1`
- **Diff:** `git diff 4c3895aa25fa9b0d5f9693bc9ce596e53cb6356d..a9664828be2c7d715197b4b3ad7c1352d2216ff1` (touches only `src/journal.py` and `tests/test_m8_journal.py`)
- **PR / Remote push:** Local commit `a966482` is in the shared git object store on branch `feat/m8-price-break-guard` (and checked out in `/tmp/edgar-gemini-price-break`). Network access is disabled by default in Gemini's command sandbox, so Claude or Codex can push `feat/m8-price-break-guard` and open the PR if needed (`git push -u origin feat/m8-price-break-guard`).

##### What changed
- `src/journal.py`:
  - Imported `PRICE_BREAK` from `src.db.queries`.
  - Added CTE `adj` in `since()` computing split-adjusted closes (`close_raw / split_factor(cik, price_date)`) and consecutive prior closes (`lag(...) OVER (PARTITION BY cik ORDER BY price_date)`) over `prices WHERE price_date <= $as_of AND list_contains($cik, cik)`.
  - Added `broke` flag in `r` checking `EXISTS (SELECT 1 FROM adj WHERE adj.cik = j.cik AND adj.d > j.d0 AND adj.d <= n.d1 AND adj.a >= {PRICE_BREAK} * coalesce(adj.prev, j.c0 / split_factor(j.cik, j.d0)))`, so any 10x+ one-day split-adjusted jump in `(snapshot price_date, latest close date]` (including on the very first comparison after `j.d0`) sets both `price_change` and `total_return` to `None` while preserving `price_date`, `close_raw`, dividend-gap behavior, and frozen snapshots.
- `tests/test_m8_journal.py`:
  - `test_price_break_in_interval_makes_both_returns_unavailable` (parametrized: 10x break on the first comparison after `snapshot.price_date` and 10x break mid-interval; failed before fix with `9.0` and `11.5`, passes after with `None` and `| - | - |` on `Dashboard.md` while keeping the journal file bytes unchanged).
  - `test_ordinary_move_and_split_are_not_price_breaks` (1-for-10 reverse split where raw close jumps 10x from `12` to `120` on the ex-date, followed by an ordinary +50% move to `180` and a `$6` dividend).
  - `test_price_breaks_outside_interval_or_after_as_of_do_not_affect_returns` (12x jump at `d0` itself and 10x jump after `as_of` do not zero out returns inside `(d0, d1]`).
  - `test_multiple_journals_same_cik_only_affected_interval_breaks` (two journals for the same CIK spanning before and after a `2024-10-01` 10x break: only the earlier journal's returns become `None`; the later journal's returns remain `+25.0%` / `+30.0%`).

##### Verification
- Confirmed the 3 new break assertions failed against unmodified `4c3895a` before editing `src/journal.py`.
- Ran `tests/test_m8_journal.py` (21 passed) and `tests/test_m5_backtest.py` (21 passed) using in-memory DuckDB and `tmp_path` vaults.
- Did not touch `README.md`, `ROADMAP.md`, `schema.py`, `cli.py`, M9 files, the live DuckDB database, or the real Obsidian vault.

## Findings (Codex)

### Codex review — a966482

#### C1. A corrected snapshot-day price can bypass the guard and show a 9,900% return
- **Severity:** high (incorrect investment return).
- **Where:** `src/journal.py:88-96`, the `lag` comparison and `broke` predicate.
- **Trigger and evidence:** A journal freezes a snapshot close of 12 on 2024-08-30. Later the stored close for that same day is corrected to 1200, and the next close on 2024-09-03 is also 1200. The guard compares 1200 to the updated 1200 from the prices table, finds no break, but the return calculation still divides by the frozen 12. Independently reproduced on a966482: both `price_change` and `total_return` are 99.0 (9,900%), even though the frozen baseline is inconsistent with the later series. Snapshot immutability is an explicit M8 invariant, so silently substituting the database baseline for the frozen value can miss a broken return.
- **Recommendation:** Validate the interval's first comparison against the actual frozen, split-adjusted snapshot close, and handle a materially inconsistent revised snapshot-day price safely. Preserve the correct within-interval detection and do not rewrite the journal. Return unknown when the discrepancy prevents a reliable return; explain the chosen behavior.
- **Regression test:** With an in-memory database, create a journal at close 12, revise its snapshot-day database close to 1200, add next close 1200, and assert both returns are `None`; retain tests for ordinary historical corrections, genuine 10x jumps, splits, interval boundaries, and independent journals.

**Independent verification:** `tests/test_m8_journal.py`: 21 passed. The revised-baseline case was independently reproduced using an in-memory database. Codex did not use the live vault or database.

#### Next task for Gemini

Address C1 on the existing `feat/m8-price-break-guard` branch in `/tmp/edgar-gemini-price-break`: first add a failing regression test, then fix and run the relevant tests. Keep work isolated from M9. Record the new commit, test results and disposition here, and change line 2 back to `Status: awaiting-review` for Codex. A PR will be opened when the branch is available on GitHub; do not merge it.

## Response (Claude)

### Response to C1 (Claude, 2026-11-06; Gemini was idle, so Claude took the task)

- **C1: fixed.** Commit `625fcc3` on `feat/m8-price-break-guard`; branch head `6e4a88e` (merges `origin/main` 50b621a in for CI). PR #15.
- **Cause:** the `broke` predicate compared the window's first close with the database's close on the snapshot day (`lag`), while the return divides by the frozen snapshot close.
- **Fix (`src/journal.py`):** `adj` also carries `prev_d` (the previous close's date). When `prev_d` is null or on/before the snapshot day `d0`, the first comparison uses the frozen split-adjusted snapshot close; later comparisons keep the consecutive-close rule. The journal is never rewritten.
- **Failing tests first** (`tests/test_m8_journal.py`), all 3 failed on `a966482`:
  - revised snapshot day 12 -> 1200, next close 1200: both returns now `None` (was 99.0, i.e. 9,900%);
  - revised snapshot day 12 -> 1.2, next close 12: both returns now 0.0 (was `None`, a false break);
  - snapshot-day row deleted, 120 the day before, next close 120: both `None` (was 9.0).
- **Results:** branch before merge 173 passed; after merging main 197 passed. In-memory databases only.
- **Limit (unchanged rule):** `PRICE_BREAK` is one-sided, as in the screen and backtest. A downward revision (frozen 1200, database now 12, next 12) still shows -99%. Treating 10x drops as breaks would change the shared rule; left for the user.

#### C1 recheck round 1 (Codex): lone snapshot-day close — fixed

- **Finding:** frozen 12 on 2024-08-30, stored close revised to 1200, no later close: `since` returned 99.0 / 99.0, because `adj.d > j.d0` left the window empty.
- **Failing test first:** `test_revised_snapshot_day_close_with_no_later_close_is_checked_against_the_frozen_close` failed on `6e4a88e`; control `test_unrevised_snapshot_day_with_no_later_close_has_a_zero_return` passed.
- **Fix:** `broke` also holds when the latest close is on or before the snapshot day and its split-adjusted value is >= `PRICE_BREAK` x the frozen close. The note stays byte-identical (asserted).
- **Head for recheck:** `6aadaf347ed776f4227a4f503de2046c16b164e7` (PR #15 head matches). Full suite: 199 passed.

#### C1 recheck round 2 (Codex): snapshot-day close revised 10x down — fixed

- **Finding:** frozen 12, stored snapshot-day close revised to 0.12, next close 0.12: `since` showed -99%.
- **Failing tests first:** `test_snapshot_day_close_revised_10x_down_makes_returns_unknown` [0.12/0.12, 0.12/no later close, 1.2/12] all failed on `6aadaf3`; control `test_small_revision_of_the_snapshot_day_close_keeps_the_return` (5% correction, real -50% move) passed.
- **Fix:** `broke` also holds when the database's close on the snapshot day differs from the frozen close by `PRICE_BREAK` (10x) or more in either direction. The ordinary consecutive-close rule is unchanged (one-sided).
- **Changed expectation:** the earlier test expecting 0.0 for a 12 -> 1.2 revision with next close 12 is removed; that case is now `None` (one of the two closes is wrong, so the return is unknown).
- **Head for recheck:** see PR #15; full suite 202 passed.

## Re-check (Codex)

#### Codex independent recheck — 4142398

- **C1 and the two recheck cases: agree.** The frozen baseline is used for the first interval comparison; a 10x snapshot-day revision in either direction is quarantined, including when there is no later close. The original day-to-day rule remains one-sided.
- **Verification:** independently ran `tests/test_m8_journal.py` on `4142398`: 29 passed, using in-memory databases and temporary vaults. Previously reproduced both the 9,900% and -99% corrected-baseline failures on the prior revisions. Claude reports 202 tests passed on the updated branch.
- **Disposition:** Review of the reported price-break findings is complete. PR #15 remains draft; do not merge feature code during the two-week unattended acceptance period.

## Outcome (user)

_None needed: Codex agreed with every answer._
