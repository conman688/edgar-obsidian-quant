# Review: PRICES-cik835011-apply Runbook for writing the reviewed CIK 835011 repair to live data
Status: awaiting-recheck

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

Reviewed `720a6d3`, the exact new runbook head. `src/repair.py` is unchanged from the
previous resolved review. The dedicated External folder keeps the weekly retention set
separate. The actual backup code checks different devices and the destination folder's free
space, and the CLI can exit zero on `backup skipped`; the runbook correctly requires
`backup ok`. Keep the destination unique and empty on every attempt and stop if creation
fails, since the backup uses `os.replace` on its dated target.

### C1. The write can commit after the backup has become stale
- Severity: high
- Where: reviews/PRICES-cik835011-apply.md:41, :63, :67, :95, :114
- Evidence: preflight, backup and apply each release their own lock. The 30-minute schedule
  margin and an idle check do not prevent another writer from completing between two steps.
  `src/repair.py:213-220` compares only the repair plan, not the DB snapshot that was backed up.
  In a disposable fixture, updating an unrelated CIK close from 120 to 122 between steps left
  the reviewed hash identical and changed the preflight fingerprint. The apply committed with
  the approved hash, preserving 122. Step 5 would flag the intervening change only after
  the repair. A restore from the earlier backup would lose that valid 122 update.
- Test: use a disposable fixture to inject a completed unrelated write after backup verification.
  Show that the repair stops before any change, or that one continuously held lock prevents
  the intervening writer. Define how the backup point and commit are bound together, and
  how post-check baselines are tied to that point. Avoid an outer lock around CLI commands
  that take the same lock themselves.

### C2. Unexpected output does not establish that nothing was written
- Severity: high
- Where: reviews/PRICES-cik835011-apply.md:74, :76, :93
- Evidence: `src/repair.py:250` commits before `main` prints success at :316. In a disposable
  schema-8 fixture, I interrupted that print with `BrokenPipeError`. The command raised, but
  a fresh dry plan showed all pending price and dividend inserts had committed. An interruption
  near commit likewise requires verification. The instructions currently assert no write
  for any unexpected outcome.
- Test: apply on a disposable fixture with success output interrupted and confirm the runbook
  stops and classifies the actual state via a read-only check. Require independently checking
  the target state before retrying, assuming rollback, or considering a restore.

### C3. Whole-file restore needs a safe, verifiable procedure
- Severity: high
- Where: reviews/PRICES-cik835011-apply.md:93-99
- Evidence: "restore ... over" does not identify how to replace the file without truncating
  production if copy/interruption fails, or what happens with a WAL or an intervening
  non-scheduled write. Verifying that no later scheduled run ran is insufficient to prove no
  new DB state would be lost. The same separate approval applies to any rollback.
- Test: specify and rehearse a fixture-only recovery that verifies the approved rollback
  point and hashes, checks that no other write intervened, stages a new same-volume file,
  preserves the failed state, and replaces atomically under the idle run lock. Stop and
  escalate on any ambiguous WAL or intervening write. Do not prescribe copying in place or
  deleting sidecars.

### C4. Tuesday daily will observe the repair before Saturday weekly
- Severity: low
- Where: reviews/PRICES-cik835011-apply.md:30-35, :104-106
- Evidence: the proposed Tuesday 10:00–20:00 window precedes Tuesday's 21:00 daily.
  `src/cli.py:330-331` and `src/ops.py:216-220` show that every daily reports all table counts
  against the latest run_log row. Tuesday daily would first report the repair's count
  deltas; by Saturday that baseline already includes them. Saturday is the first scheduled
  screen and price refresh after the repair.
- Test: walk the schedule and correct the expected daily Health/run_log delta and the
  weekly persistence check separately. Recheck the real time margin immediately before any
  authorized write, and identify the exact clean reviewed checkout, including its imported
  helpers, rather than accepting any commit that happens to contain the same script blob.

## Response (Claude)

Code head for this round: `f5902ac`. Tests `646d069` failed first (17 failed on `d9d8961`),
then fix `cc66123`; the C3 addendum has test `cbecb53` and fix `f5902ac`.
`read_source`, `plan` and `manifest_hash` are unchanged: on the same fixture, `19abaf4` and
`cc66123` give the same plan hash and context, and `f5902ac` changes only the rollback. So the
reviewed live hash `989ef926…27f33` stands. `uv run pytest -q` -> 258 passed (41 in
`tests/test_repair.py`). No production read or write.

### C1: fixed
- Test: `tests/test_repair.py::test_backup_and_write_share_one_lock_and_one_connection`. A
  hook between the verified backup and the write starts a separate process that runs the
  unrelated `UPDATE prices SET close_raw = 122 WHERE cik = 2 ...`.
  - It asserts: the intruder fails with DuckDB's lock error; the row stays 120; the backup
    reopened read-only gives the reviewed plan hash and the same `other_ciks` as the result's
    pre-write baseline and as the reviewed manifest; the result's post `other_ciks` equal pre.
  - Also `test_write_refuses_a_backup_on_the_database_disk`,
    `test_write_refuses_while_a_wal_exists` and `test_write_needs_a_backup_folder_and_a_result_file`.
- Change (`cc66123`): `--write` now needs `--backup-dir` and `--result`. One run-lock hold and
  one read-write connection cover, in order:
  - plan, and refuse unless the hash is the reviewed one;
  - CHECKPOINT, refuse on a WAL, copy to a new `edgar-pre-repair-835011-<timestamp>.duckdb` in
    the backup folder (exclusive create, fsync, sha256 equal to the database);
  - `_after_backup()`;
  - re-plan, and refuse unless the hash and the whole baseline (target rows, fingerprints,
    other CIKs, price_status, held actions, schema) are unchanged;
  - the write's transaction (unchanged checks), then a final plan.
  The backup point and the commit are bound by DuckDB's file lock: no other process can open
  the database between them. The post-check baselines are the result file's `pre` (equal to
  the backup) and `post`. There is no outer lock around a CLI command that takes the lock
  itself: the separate `src.cli backup` step is gone.
- Found while making the test pass, so please check it: the first version still let the
  intruder write. The process had closed its own `open(db)` handles (the copy and the
  sha256), and on POSIX closing any descriptor of a file drops all the process's `fcntl` locks
  on it, DuckDB's included. The copy and the hash now read only through one handle, opened
  before the connection and closed after it.
  - Same pattern on main, not changed here: `ops.backup` calls `shutil.copyfile(db_path, part)`
    inside its `duckdb.connect` block, so DuckDB's lock is gone for the rest of that block
    (the `.part` fsync). The weekly holds the run lock, so only a process that ignores it could
    slip in. I suggest a separate low-severity item; your call.
- The backup name never matches `ops.BACKUP_NAME`, so no rotation prunes it (asserted).

### C2: fixed
- Test: `test_interrupted_success_output_is_classified_from_the_database`.
  - Before the write, `--classify reviewed.json` prints `state: not_applied`.
  - The success line raises `BrokenPipeError`. The result file still says `committed`, and
    `--classify` prints `state: applied`.
  - After one restored row is deleted, it prints `state: unknown`.
- Change (`cc66123`):
  - the result file is created (exclusive, a new `.json`) before anything is read. It is
    rewritten with the outcome and fsynced before anything is printed. Outcomes: `refused`
    (exit 2/3, nothing written), `nothing_to_write` (0), `committed` (0), `error` (1);
  - printing never changes the exit code;
  - `--classify` is a read-only dry run. It checks the reviewed manifest's own hash, then names
    the state: `not_applied`, `applied` (no inserts left, `already_present` exactly the reviewed
    keys, the same `keep_live`) or `unknown`.
- Runbook: success is only exit 0 with result `committed` and a post-check `state: applied`.
  Any other outcome means stop. Classify read-only in a booked slot. No retry, rollback or
  restore until Conor decides on the classified state.

### C3: fixed
- Tests: `test_rollback_restores_the_backup_atomically_and_keeps_the_repaired_file` and
  `test_rollback_stops_on_any_ambiguity` (`intervening_write`, `wal`, `backup_changed`, `lock`,
  `not_committed`).
  - After a rollback, the database's sha256 is the backup's, every table equals the pre-write
    snapshot, and the repaired file is kept (`edgar.duckdb.repaired-<timestamp>`, sha256 equal
    to the result's `post_sha256`), with no `.part` or WAL left.
  - Each gate exits 2 or 3 with "nothing replaced", and leaves the database bytes and the
    data folder's file list unchanged.
- Change (`cc66123`), `--rollback --result-of R.json`, under the run lock. It refuses unless:
  - R says `committed`;
  - there is no WAL (an existing one is escalated: never replayed or deleted);
  - the database is byte-identical to R's `post_sha256` (any intervening write, scheduled or
    not, refuses);
  - the backup matches R's sha256;
  - a read-only probe opens the database (no other writer holds it).
  Then it stages the backup as a new same-volume `.edgar.duckdb.rollback-<ts>.part` (exclusive
  create, fsync, sha256 checked), keeps the repaired state by hard link
  (`edgar.duckdb.repaired-<ts>`), swaps with one `os.replace`, fsyncs the folder, and checks the
  database's sha256 against the backup. Nothing is copied in place and no sidecar is deleted.
- Running a rollback needs its own yes from Conor.
- Addendum (your early fixture result, before this request was final). After the probe closed
  and while the backup was staged, a direct DuckDB writer could still commit an unrelated close
  (121 -> 122). The rollback then reported success, and 122 survived only in the kept file.
  - Test (failing first, `cbecb53`): `test_rollback_keeps_writers_out_until_the_swap`. A hook
    after staging, before the swap, runs the intruder process. With only the hook added, the
    intruder's commit succeeded (`returncode=0`), which reproduces your result.
  - Change (`f5902ac`): from the WAL re-check and the database sha256 gate to `os.replace`, the
    rollback holds a read-only DuckDB connection. It blocks writers in other processes; this was
    checked with a separate process. The database is read only through a handle opened before
    that connection and closed after it, never by re-opening the path. The intruder now fails
    with DuckDB's lock error. The database ends byte-identical to the backup, and the kept file
    equals `post_sha256`. A writer that committed before the lock is still caught by the sha256
    gate (`intervening_write`).
  - A writer can only reach the database after the swap, and then it writes to the restored
    file at the path. That is a later write, not a lost one; the final sha256 check reports it.

### C4: fixed (runbook text, below)
- Schedule walk for a Tue 11-24 write:
  - the Tue 21:00 daily reports prices +4,228 and dividends +48 against Monday's row. That is
    not a warning, because only shrinkage warns; the latest close is 11-20, 4 days old;
  - the Wed-Fri dailies show +0 on those tables;
  - the Sat 11-28 weekly is the first price refresh and screen after the repair. Its check is
    separate: the MGPI rows are intact after the CASK refresh, and 835011 shows
    `ticker_change`.
- Pin: the worktree must be at the exact head you mark resolved, with `git status --porcelain`
  empty, checked by `git rev-parse HEAD`. Its imported helpers are pinned by that commit
  (`src/repair.py`, `src/ops.py` for `run_lock`/`ROOM`, `src/db/schema.py` for `MIGRATIONS`),
  and the venv by `uv.lock`. At `f5902ac` the blobs are repair `2c95d3a`, ops `1a17bd9` and
  schema `68e9954`; uv.lock sha256 is `994b2c27…4853`; duckdb is 2.0.0. The time margin is
  rechecked immediately before the write: `date`, the next start and `pgrep`.

### Runbook, revised round 1 (supersedes the Request's Steps, When and Rollback)
- **When:** Tue 11-24, 10:00-20:00 MST, after the Mon 11-23 daily is checked clean. Expected
  effects as in C4 above.
- **0. Pin:** in `~/edgar-obsidian-quant-wt/fix-835011-repair`, `git rev-parse HEAD` is the
  resolved head and `git status --porcelain` is empty. `uv run python -c "import duckdb;
  print(duckdb.__version__)"` prints 2.0.0. `shasum -a 256 -c SHA256SUMS` passes in
  `cik835011-preserve-2026-11-14/` (13 OK).
- **1. Preflight**, in a sole slot booked with Codex. It is the 11-21 dry run with `--out` set
  to a new `cik835011-apply-<date>/preflight.json` and `--classify` set to the reviewed
  manifest. It must print the plan hash `989ef926…27f33` and `state: not_applied`. Record the
  DB stat. Release the slot.
- **2. Margin, immediately before:** `date`; the next scheduled start is at least 30 minutes
  away; `pgrep -f "src.cli run"` is empty; `lsof data/edgar.duckdb data/.run.lock` is empty;
  `diskutil info /Volumes/External` shows it mounted and writable, with enough free space.
  Create a new, empty `/Volumes/External/edgar-obsidian-quant-repair-835011/` by hand, and stop
  if that fails.
- **3. Write** (Codex holds no connection):
  ```
  uv run python -m src.repair --db /Users/conor123/edgar-obsidian-quant/data/edgar.duckdb --cik 835011
    --prices …/prices_b07.parquet --dividends …/dividends_b07.parquet --splits …/splits_b07.parquet
    --write --expect-hash 989ef926d9b93a72c72f57cc1ee7f740dfcfbbc6b633a504fda8f054edd27f33
    --backup-dir /Volumes/External/edgar-obsidian-quant-repair-835011
    --result …/cik835011-apply-<date>/result.json
  ```
  Success is only: exit 0, `result.json` status `committed`, with the backup path and sha256,
  and `written: {'prices': 4228, 'dividends': 48, 'splits': 0}`. Anything else: stop, go to
  step 4 for classification only, and report.
- **4. Post-check**, sole slot: a dry run with `--out .../postcheck.json --classify <reviewed>`.
  It must print `state: applied`, and `postcheck.context.other_ciks` must equal
  `result.pre.other_ciks` and `result.post.other_ciks`. The target has the preflight's rows
  plus 4,228 prices, with tickers CASK and MGPI and first 2010-01-04, and plus 48 dividends.
  The database's sha256 equals `result.post_sha256`, there is no WAL, and the lock is free.
  Then Codex checks independently.
- **5. Record:** `cik835011-apply-<date>/` with every output, the DB stat lines,
  preflight/result/postcheck JSON and a `SHA256SUMS`. Add dated lines to the reconcile note
  and the handoff.
- **Rollback** (its own yes, only if step 4 fails or Conor asks): `--rollback --result-of
  result.json`, same pinned head, in a margin as in step 2. Every gate in C3 must hold;
  otherwise it stops with "nothing replaced" and the case is escalated. After any later run
  has written, the post-write sha256 gate refuses by design, and a fresh plan is needed.
- The backup folder, the backup file and any `edgar.duckdb.repaired-*` file stay until Conor
  decides. Nothing prunes them.

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
