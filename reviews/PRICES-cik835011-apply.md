# Review: PRICES-cik835011-apply Runbook for writing the reviewed CIK 835011 repair to live data
Status: awaiting-review

## Request (Claude)

- Branch: `fix/cik835011-repair`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/32
- Diff: `git diff 4fa1a20..<exact head from the hand-off message>`. It adds only this file.
  Code is unchanged since the resolved review `reviews/PRICES-cik835011-repair.md` (code
  `19abaf4`).
- Asked by Conor (2026-11-21, "Yes, write plan"). This is a plan only. **Approving this
  runbook does not approve running it.** Running it needs Conor's separate, explicit yes,
  given against the hash and backup plan below.

### What is being applied
- Script: `src/repair.py`, git blob `2a9dcc1abc8a33c303481a20bdeda6064bb4c88f` (sha256
  `62269a5e6ad76f8cf440539c48035b3bdbb3a18a2b78f1a076ac7a2fa4f9ce61`). It is identical at
  `19abaf4`, `4fa1a20` and this head. It runs from the worktree
  `~/edgar-obsidian-quant-wt/fix-835011-repair` at a commit with that blob, whether or not
  PR #32 is merged. Step 1 checks the blob.
- Reviewed plan hash: `989ef926d9b93a72c72f57cc1ee7f740dfcfbbc6b633a504fda8f054edd27f33`
  (manifest `edgar-obsidian-quant-checks/cik835011-repair-2026-11-21/manifest.json`).
  - inserts: 4,228 MGPI prices (2010-01-04 .. 2026-10-23) and 48 dividends
    (2010-09-13 .. 2026-08-14);
  - 4 kept-live CASK prices (2026-11-02 .. 11-05);
  - no splits, no price_status change, nothing blocked.
- Inputs: `edgar-obsidian-quant-checks/cik835011-preserve-2026-11-14/{prices,dividends,splits}_b07.parquet`,
  verified against their `SHA256SUMS` by the script itself.

### When
- After the Mon 11-23 21:00 daily (the first run with migration 9) has been checked clean, so
  that a Monday anomaly is not mixed up with the repair. Proposed: Tue 11-24, any time from
  10:00 to 20:00 MST. Before the Sat 11-28 07:00 weekly, so that the weekly is the first
  scheduled run on repaired data.
- Hard window: no `src.cli run` process, the run lock free, and at least 30 minutes to the
  next scheduled start (weekdays 21:00, Sat 07:00). Expected work time is under 5 minutes: the
  backup took about 1 minute on each weekly, and the dry run 0.87 s.
- If the 11-28 weekly has run first, the plan is re-derived. A weekly refresh starts at about
  last_date - 7 days, after every hashed key, so the same hash is expected. If it differs,
  stop: the write refuses anyway, and a new manifest needs review.

### Steps (Claude runs them; Codex holds no DB connection from step 2 to step 5)
1. **Preconditions** (no DB access):
   - `git -C ~/edgar-obsidian-quant-wt/fix-835011-repair rev-parse HEAD:src/repair.py` prints
     `2a9dcc1a…`; `git status --short` is empty.
   - `shasum -a 256 -c SHA256SUMS` passes in `cik835011-preserve-2026-11-14/` (13 OK).
   - `pgrep -f "src.cli run"` is empty; `lsof data/edgar.duckdb data/.run.lock` shows no
     holder; the next scheduled start is at least 30 minutes away.
   - `diskutil info /Volumes/External` shows the drive mounted and writable, with at least
     5 GB free (279 GiB on 11-21). It is a different device from `data/` (the backup guard
     checks this too).
2. **Pre-flight dry run**, in a sole reader slot booked with Codex:
   - command: the 11-21 dry run, with `--out` set to a new
     `edgar-obsidian-quant-checks/cik835011-apply-<date>/preflight.json`;
   - record the DB size, mtime and inode before and after;
   - continue only if it exits 0 and the plan hash is `989ef926…27f33`. Otherwise stop.
3. **Pre-repair backup to its own folder**, so the KEEP=4 rotation is not disturbed:
   - create `/Volumes/External/edgar-obsidian-quant-repair-835011/` by hand (`mkdir`, after the
     diskutil check);
   - from `/Users/conor123/edgar-obsidian-quant`, run
     `uv run --env-file .env python -m src.cli backup --dest /Volumes/External/edgar-obsidian-quant-repair-835011`.
     It takes the run lock, CHECKPOINTs, copies, checks the copy's `loaded_filings` count, and
     prunes only `edgar-YYYY-MM-DD.duckdb` files in that new folder, where there is just one;
   - continue only on `backup ok`. Then record the copy's sha256 and the database's sha256;
     they must be equal.
   - Why not the normal folder: a manual backup there would prune 11-05 now, and the 11-28
     weekly would then delete 11-07, the last pre-loss copy, a week early.
4. **Write**:
   - from the worktree, run the dry-run command without `--out`, plus
     `--write --expect-hash 989ef926d9b93a72c72f57cc1ee7f740dfcfbbc6b633a504fda8f054edd27f33`;
   - the script re-derives the plan under the lock and writes only if the hash matches. In one
     transaction it checks: every other CIK's [count, xor hash] per table is unchanged; the
     target equals its old rows plus the plan's rows; a fresh plan has nothing left. Otherwise
     it rolls back.
   - expected output: `written: {'prices': 4228, 'dividends': 48, 'splits': 0}, first_date None;
     plan hash 989ef926…`, exit 0;
   - any other output or exit code: stop, do not retry, report. Nothing is written in that
     case (fixture tests).
5. **Post-check**, read-only, in a sole slot (Claude's dry run, then Codex's own check):
   - a dry run with a new `--out .../postcheck.json`. Expected:
     - prices: insert 0, already_present 4,228, keep_live 4;
     - dividends: insert 0, already_present 48;
     - splits empty, set_first_date none, blocked none;
   - `context.other_ciks` is identical to preflight.json's (same duckdb 2.0.0);
   - target: prices = preflight rows + 4,228 (17 + 4,228 = 4,245 if no weekly ran between),
     tickers `["CASK", "MGPI"]`, first 2010-01-04; dividends = preflight + 48;
     `target_price_status` unchanged;
   - no `edgar.duckdb.wal` left, and the lock is free.
   - Codex then checks independently, as on 11-21: plain duckdb read-only, against the manifest.
6. **Record**: `edgar-obsidian-quant-checks/cik835011-apply-<date>/` with every command's
   output, DB stat lines, the backup path and sha256, preflight.json, postcheck.json and a
   `SHA256SUMS`. Add a dated line to `reconcile-2026-11-14.md` and to the handoff.

### Rollback
- A failure inside the write rolls back by itself: nothing to undo.
- If the post-check fails after a commit, or Conor asks for a rollback: restore the step-3 copy
  over `data/edgar.duckdb`, under the run lock. This is only valid before any later run, and
  it needs Conor's yes, because it replaces the live file. It must not be done after a
  scheduled run has written: that run's changes would be lost, so a fresh plan comes first.
  There is no reviewed targeted delete script, and none is proposed.

### Effects to expect afterwards (not failures)
- The screen quarantines 835011 with `ticker_change` while MGPI rows are in its 365-day window
  (until about 2027-10-23). Backtest and journal returns break at 2026-10-23 -> 10-26
  (PR #30 guards). The 11-28 weekly screen is the first to show it.
- The 11-28 weekly's drift check sees prices +4,228 and dividends +48. Only shrinkage warns.
- The 11-28 weekly deep check should confirm the MGPI rows are intact after the CASK refresh.
- The step-3 folder and file stay on External until Conor decides. They are outside the
  rotation, so nothing prunes them.

### Where bugs are most likely
- Step 3 is the only new write path outside the reviewed script: a hand `mkdir` on External,
  then the existing `backup` command with `--dest`. Please check the prune scope and the
  same-device guard in `ops.backup`.
- Order: the write opens read-write under the lock straight after the backup released it, and
  no scheduled run can start in a 30-minute margin. A manual run could still start;
  `run_lock` refuses it, and vice versa.
- The xor-of-hashes comparison in step 5 is valid only with the same duckdb version (2.0.0).

### Known gaps
- There is no automated rollback script. Rollback means restoring the whole file from step 3.
- Approval of the PR (merge) and of this runbook (execution) are separate yeses, plus the
  Codex slot bookings in steps 2 and 5.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
