# Review: M9 Operations
Status: awaiting-review

## Request (Claude)

- Branch: `feat/m9-operations`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/16
- Diff: `git diff 4c3895a..1ab7c2b` (base is main after the M8 merge; the review commits touch only this file)
- Tests: `uv run pytest` -> 184 passed (`tests/test_m9_ops.py`: 19). GitHub Actions: green on the PR.

### What changed
- `src/ops.py` (new):
  - `backup(db_path, dest, today, keep=4)`: skipped when `dest` is unset, missing (never
    created), on the database's own disk (`st_dev`), or short of room (1.05 x the database).
    Deletes stale `.edgar-*.part` files first. Holds a DuckDB connection (the file lock) through
    `CHECKPOINT` and the copy, fsyncs the `.part`, opens it read-only and compares
    `loaded_filings` counts, then `os.replace`s it into place. `prune` deletes only names
    matching `^edgar-\d{4}-\d{2}-\d{2}\.duckdb$` beyond the newest 4. A failed copy removes
    nothing old.
  - `run_lock` (`flock` on `data/.run.lock`).
  - `run_steps`: steps run in order; after a failure (an Exception or a SystemExit) the rest
    are "not run".
  - `run_log` row per run; `_warnings` covers failed steps, drift (ledgers on any drop,
    other tables beyond 1%), failed Form 4 and price ledger entries, a latest price more than 4
    days old, SEC throttled and dropped counts, and a backup that is not ok.
  - `render_health`.
  - `write_schedule`: launchd plists (caffeinate, unbuffered logs, Saturday 07:00 /
    weekdays 21:00).
- `src/db/schema.py`: migration 8, `run_log`.
- `src/ingestion/sec.py`: `throttled` and `dropped` counters. It now retries
  `ConnectionError` and `Timeout` with the same backoff (the first live weekly run died on one
  `RemoteDisconnected`).
- `src/cli.py`:
  - `run weekly|daily`: lock, backup (weekly), one connection, steps built from the
    command's own parser.
  - `backup [--dest]` and `schedule [--out]`.
  - Every command takes an optional connection, and `_client` is cached (one rate limit and one
    throttle count per process).
- `src/config.py` `BACKUP_DIR` (`EOQ_BACKUP_DIR`); `.env.example`;
  `.github/workflows/tests.yml`. Docs: README (Operations, layout, invariant, status),
  AGENTS.md (invariants, commands not to run), ROADMAP (result).

### How to verify
- `uv run pytest` -> 184 passed. `tests/test_m9_ops.py::test_killed_form4_fetch_resumes_without_duplicates_or_refetches`
  starts a real child process, `SIGKILL`s it mid-batch, and reruns.
- `uv run python -m src.cli schedule --out /tmp/launchd` writes two plists; `plutil -lint` them.
  Do not install them.
- Do not run `run`, `backup` or `ingest` (they write the live database, the vault and the
  external drive). Live results are in ROADMAP M9; `run_log` on the live database holds 4 runs
  (daily ok 160 s, daily ok 24 s, weekly failed 681 s, weekly ok 276 s), readable with
  `duckdb.connect('data/edgar.duckdb', read_only=True)` when no run is going.

### Where bugs are most likely
- Retention: anything that could delete a file that is not ours (case-insensitive file systems,
  exFAT `._` files, symlinks, a `keep` of 0), or an old backup before the new one is checked.
- The drive: a mount point that exists but is empty, a drive removed mid-copy, the same-disk check
  on other layouts.
- Consistency: the copy while the connection is held (WAL, checkpoint), the read-only check.
- Runs: a step that changes `sys.exit` semantics, a run that dies between steps, the lock under
  launchd, the health report's baseline (it compares with the last run of any job).
- SEC client: retrying a non-idempotent request (all are GETs), the backoff while a stream is
  open, the counts with the cached client.

### Known gaps (not findings unless they are worse than stated)
- Installing the launchd agents and "two unattended weeks" are left to the user.
- A connection dropped mid-stream (inside `download`'s `iter_content`) is not retried: the
  step fails and the next run downloads again.
- The backup is checked by one table's row count, not a full integrity scan.
- The backup runs before the weekly steps, so it holds the state before that week's load.
- Drift compares with the previous run of any job, not of the same job.
- No alert outside the vault and the log (no e-mail).

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
