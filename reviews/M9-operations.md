# Review: M9 Operations
Status: awaiting-response

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

### C1. A drive error during backup setup or cleanup prevents the weekly health report
- Severity: high
- Where: src/ops.py:73-80, 98-101; src/cli.py:340-354
- Evidence: The backup's try block covers the copy, but not destination stat/listing, stale-part removal, disk-usage lookup, retention, or a failure in the except handler's part.unlink. If the external drive disconnects after is_dir succeeds, or a stale part cannot be removed, these operations can raise OSError rather than return a backup result. Independently reproduced by making disk_usage raise OSError after the directory exists: backup propagates the error. cmd_run calls backup before run_steps and catches only RunLocked, so it exits before the weekly steps, run log, and health report. This defeats the intended drive-disconnect warning and leaves the prior health note stale. Handle filesystem errors over the whole backup lifecycle, make cleanup best effort, and return a result that weekly run can record without pruning verified backups after a failed copy.
- Test: In a temporary directory, monkeypatch disk_usage to raise OSError after dest.is_dir succeeds and assert backup returns failed/skipped rather than raising. Add a run-command test with an injected backup filesystem error that asserts the run is logged and its health note warns; separately test cleanup failure, preserving any previous backups.

Verification: tests/test_m9_ops.py: 19 passed. Reproduced the drive error using temporary files and a mocked disk_usage; no live database, vault, backup drive or ingest was used by Codex.


## Response (Claude)

Reproduced on 7db0b1a: the five new tests below each failed by raising out of `backup`
(`OSError: [Errno 5] Input/output error`, `PermissionError`, `OSError: drive removed`).
Fixed in ce23246. Suite: 189 passed (M9: 24).

### C1: fixed
- Tests (tests/test_m9_ops.py):
  - `test_drive_error_after_the_folder_check_is_a_failed_backup_not_a_crash`: the reviewer's
    case, `disk_usage` raises after `is_dir`; now `failed`, old backups kept.
  - `test_listing_error_is_a_failed_backup`: `iterdir` of the folder raises.
  - `test_cleanup_failures_are_best_effort_and_keep_the_new_backup`: a stale `.part` and the
    oldest backup cannot be deleted. The new backup is in place and `ok`; the other old backup
    over the limit is still removed; both failures are in `warning`, and the health report
    says "backup cleanup: ...".
  - `test_failed_copy_whose_part_cannot_be_removed_still_returns_failed`: the copy fails and its
    `.part` cannot be removed (drive gone). Still `failed`, old backups kept.
  - `test_weekly_run_with_a_drive_error_still_logs_and_reports`: `run weekly` through the CLI
    with a drive error. The step runs, `run_log` has the run, and `Reports/Health.md` says
    "backup failed: OSError: [Errno 5] Input/output error".
- Change: `backup` wraps everything after the folder check (same-disk `stat`, listing, stale-part
  removal, `disk_usage`, copy, check, rename) and returns `failed` for any exception, so it
  never raises and `cmd_run` always reaches `run_steps`. Removing the `.part` after a failure is
  best effort. Clean-up uses `_remove` (each file on its own; errors collected, never raised);
  `prune` returns `(removed, errors)`, and a listing error during pruning is an error too. They go
  into the result's `warning` and the health report (`backup cleanup: ...`, and the Backup
  section). Pruning still happens only after the new copy is checked and renamed into place, so a
  failed copy prunes nothing.
- Live: after the fix, `backup --dest /Volumes/External/edgar-obsidian-quant-backups` (volume
  UUID 23866769-..., 441.8 GB free) gave `ok`, 4.7 GB in 52.8 s. The folder holds only
  `edgar-2026-11-05.duckdb` and its macOS `._` file.

## Re-check (Codex)

### C1: agree
Drive errors during setup and copying return a failed backup result, failed cleanup is best effort, and retention failures become health warnings after the verified copy is in place. The added tests exercise the weekly CLI, stale parts, and failed deletion.

Verification: tests/test_m9_ops.py: 24 passed. Codex used temporary files and test databases only; Claude's live backup result was not independently verified. Two unattended weeks remain unverified, as noted in the request. Confirm the PR's actual head includes the reviewed fix and this review before merging.


## Outcome (user)

_Only for findings still disputed after two rounds._


## Scheduled acceptance incident — 2026-11-07 (Codex)

This follow-up reviews the first unattended weekly run against main
`aec2e3a68e4310a0d1cacaaa9b67f5df0d58e0d0`; it does not reopen the resolved backup C1.
Branch: `review/m9-price-refresh-2026-11-07`. Claude's scheduled check wrote
`/Users/conor123/edgar-obsidian-quant-checks/weekly-2026-11-07.md`.
Codex independently read today's weekly log, current vault Health.md and backup metadata,
and fetched origin before reviewing. No ingestion, live DB connection, pipeline changes,
vault writes or backup changes were performed by Codex.

The weekly run finished at 07:13 with status ok (752 seconds), a new November 7 backup,
and updated vault notes. However, the log records eight actual price-download failures that
were reported as no-data rather than errors. A successful process exit is therefore not
evidence that every refresh succeeded. The two-week acceptance gate remains pending.

### C2. yfinance's swallowed per-ticker errors bypass retry and the health warning
- Severity: medium (wrong failure classification and missing operational warning).
- Where: src/ingestion/prices.py:90-117, 139-148, 216-229; src/ops.py:163-165.
- Evidence: installed yfinance's `multi._download_one` catches per-ticker exceptions and
  substitutes an empty DataFrame, then `download` logs the exceptions but returns normally.
  Our `_download` drops empty ticker data and `_download_with_retry` immediately returns.
  `_store` writes `no_data`, not `error`, so `_warnings` does not report a failed refresh.
  Today's log (lines 735-778) records
  `OperationalError('unable to open database file')` for KEQU, KGS, KE, KALA, JOUT, OPAL,
  OLN and STRT, while line 792 says `17 with no data, 0 errors`. Health.md warns only about
  one recovered SEC disconnect.
- Independent reproduction: exercised the real installed yfinance download/catch path,
  patching only its Ticker history provider, session creation and YfData so there was no
  network/cache access. GOOGL returned a valid frame; NVDA raised the identical sqlite
  OperationalError. An in-memory project DB seeded with existing prices produced:
  `priced=1, no_data=1, errors=0`; NVDA status `no_data` with message "no rows from source";
  exactly one NVDA history call, zero backoffs, and `ops._warnings(...) == []`.
  NVDA's two existing price rows were retained (no price deletion was reproduced).
- Test: add a regression through the actual yfinance exception-swallowing boundary, not only
  the existing test whose injected download callable raises directly. Assert an operational
  per-ticker failure is retried as appropriate and, if exhausted, counted/reported as an
  error with its diagnostic, preserving existing dates/prices and healthy ticker results.
  A genuine no-data outcome must remain distinct. The expected `errors == 1` assertion fails
  on the reviewed main. Also require the normal health warning for an exhausted refresh.
- Recommendation: make the Yahoo adapter preserve per-symbol error outcomes rather than
  inferring successful absence from an empty/missing frame. Keep the existing supported
  partial-batch behavior; this finding does not require all healthy symbols to be discarded
  or the whole job to fail. Claude should reproduce before implementing on an isolated branch.

Reproduction script (outside the repository):
`/Users/conor123/Documents/Codex/2026-11-06/continue-working-on-my-edgar-obsidian/work/weekly-2026-11-07/reproduce_masked_price_failure.py`.
Run it with the project's .venv Python and `PYTHONPATH=/Users/conor123/edgar-obsidian-quant`.
It uses only an in-memory DB, mocked provider calls and existing source modules.

Other observations, not additional findings:
- The underlying reason for the sqlite cache error remains **unverified**. Its cache path,
  permissions, storage and concurrency need diagnosis; this is not proof that DuckDB failed.
- The dividend table shrank by 21 rows. The refresh deliberately replaces corporate actions
  within refreshed windows, so the count alone does not prove loss or a defect. Source-level
  reconciliation has not been done.
- The two backup files share a byte size; that alone does not establish identical content or
  a failed backup. Codex checked the dated file's existence/size, not full backup integrity.
- Current `df` at the Codex check showed about 6.5 GiB free internally and 407 GiB externally.
  No cleanup was performed.
- Computer-control handoff is blocked: the Mac is locked and automatic unlock failed. The
  user must unlock it manually before Codex can communicate with the existing Claude Code
  session. Do not bypass or disable the lock. The GitHub review branch is the durable handoff.

### Response to C2 (Claude, 2026-11-07)

### C2: fixed
- Test: `tests/test_prices.py::test_swallowed_ticker_error_is_retried_then_reported_as_error` and
  `::test_swallowed_ticker_error_that_clears_on_retry_is_priced` run `prices.load` through the
  installed yfinance download path (only `yfinance.multi.Ticker`, `new_session` and `YfData`
  replaced; no network, no cache, in-memory DB). Before the fix both failed with
  `(priced, no_data, errors) == (1, 1, 0)`, Codex's result. Control
  `::test_yfinance_no_data_stays_no_data_and_is_not_retried` (yfinance's "possibly delisted;
  no price data found" soft error) passed before and after.
- Change: `aabbc21` `src/ingestion/prices.py`. `_download` attaches a handler to the `yfinance`
  logger for the call and collects its per-ticker error records (`['A', 'B']: <error>`);
  a ticker with such an error maps to `TickerError` instead of being dropped. yfinance's no-data
  wording ("possibly delisted", "no price data found", "no data found") is not an error and
  keeps the old `no_data` path. `_download_with_retry` re-downloads only the failed tickers up
  to `TICKER_RETRIES = 3` times (`gc.collect()`, then 5/10/20 s backoff). `load` stores what
  still fails as status `error` with yfinance's message (same `_mark_error` as a failed batch:
  first/last dates and prices kept), counts it in `errors`, and the healthy tickers of the batch
  are stored as before. The existing health warning "N companies failed their price refresh"
  now fires (asserted in the first test). After the fix: NVDA `error`, message contains
  "unable to open database file", last_date 2026-11-06 kept, its price rows unchanged, GOOGL
  `ok`, 4 NVDA calls (1 + 3 retries), 3 backoffs.
- Live data: no live database or vault write. With the fixed `_download_with_retry` (Yahoo
  only), at 10:3x the 8 failed tickers KEQU, KGS, KE, KALA, JOUT, OPAL, OLN, STRT each return 7
  days to 2026-11-06, so the failure was transient, not ticker-specific. (KBR, "possibly
  delisted" in the run, also returns 7 days now; no-data answers are not retried by design.)

### Underlying cause of `unable to open database file` (diagnosis; likely, not proven for the run)
- The error is yfinance's own SQLite cache (`~/Library/Caches/py-yfinance/tkr-tz.db`, WAL
  mode; `tkr-tz.db` was modified at 07:09 during the prices step), not `data/edgar.duckdb`.
- launchd starts the jobs with a soft open-file limit of 256 (`launchctl limit maxfiles`:
  `256 unlimited`); an interactive shell here has 1,048,576. yfinance runs up to
  `cpu_count * 2` = 16 download threads per batch and each finished thread leaves its cache
  connection open until garbage collection: in a standalone test (temporary cache folder, no
  network) open file handles went 7 -> 112 after 400 one-thread lookups. With the soft limit
  set to 64, 948 of 1,000 lookups failed with exactly `OperationalError('unable to open
  database file')`. Not reproduced at 256 with one thread at a time; the run also had 16
  concurrent threads with their HTTP connections, DuckDB's files and the SEC client open.
- Fix (separate commit `22d3a9d`, not part of C2's claim): `ops.raise_open_files()` lifts the
  soft limit to 4096 (within the hard limit, never lower) at the start of `run`. Tests
  `tests/test_m9_ops.py::test_raise_open_files_lifts_a_low_soft_limit_within_the_hard_limit`
  and `::test_run_command_raises_the_open_file_limit_first` failed first (no such function).
  No launchd reinstall is needed.
- Full suite: 194 passed (189 + 5 new).
- Not verified: that the 2026-11-07 run actually reached 256 open files (no fd count was
  logged). The dividend count change (-21) is untouched and unexplained.


### Codex recheck — 7db5704 (2026-11-07)

C2's original exhausted-error, recovered-error and genuine-no-data cases are fixed as
described. Independently ran `tests/test_prices.py tests/test_m9_ops.py` against an immutable
archive of `7db570442ff55094044e6dcf1569ff26beedcae1`: **34 passed**. No live database or
network was used. The per-process FD-limit helper stays within the hard limit and does not
change installed launchd/global limits. The actual cause of the historical incident remains
an inference, as Claude now states.

Two new failures in the retry implementation require another response:

### C3. A batch-level exception during a ticker retry discards already successful results
- Severity: medium (healthy prices are not refreshed and healthy tickers are marked failed).
- Where: src/ingestion/prices.py:162-168, 277-282.
- Evidence: after the initial download returns healthy GOOGL data and an NVDA TickerError,
  an exception from `_download(failed, start)` escapes the per-ticker retry loop. `load`
  catches it as if the *whole original batch* failed. Independently reproduced at 7db5704:
  `priced=0, no_data=0, errors=2`; both tickers marked error, and GOOGL's fetched November 6
  close is not stored (its coverage stays November 5).
- Test: inject initial `{'GOOGL': healthy_frame, 'NVDA': TickerError(...)}`, then make the
  retry download raise a batch-level transport/runtime exception. Healthy GOOGL must still
  be stored exactly once; only the requested failed ticker(s) should remain retryable/error,
  with the retry's diagnostic after exhaustion. Preserve the separate initial-batch failure
  behavior when there are no successful results yet.

### C4. A successful no-data retry retains the old operational error
- Severity: medium (wrong failure status and repeated unnecessary retries).
- Where: src/ingestion/prices.py:168.
- Evidence: `frames.update(_download(failed, start))` replaces only keys returned. An empty
  successful result, the adapter's genuine-no-data representation, never removes the older
  TickerError. Initial GOOGL success/NVDA error followed by `{}` gives
  `priced=1, no_data=0, errors=1` after all three retries, rather than a no-data outcome.
  Independently reproduced at 7db5704; no live provider call involved.
- Test: after an initial operational error, return a successful genuine-no-data result for
  the retry. Clear the obsolete error for that requested symbol, stop retrying it, and use the
  normal no-data path. Keep other successful ticker data. Also retain exhausted-error and
  error-then-price-success controls.

The standalone reproduction is
`/Users/conor123/Documents/Codex/2026-11-06/continue-working-on-my-edgar-obsidian/work/weekly-2026-11-07/review_retry_edges.py`.
It ran against an immutable 7db5704 archive, with an in-memory DB and mocked `_download`
outcomes. C3/C4 were also sent to Claude through computer control; access is restored.
Review remains open; main and the live pipeline are unchanged.
