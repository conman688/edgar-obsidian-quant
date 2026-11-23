# Review: DAILY daily dashboard publish
Status: awaiting-review

## Request (Claude)

- Branch: `feat/daily-vault-sync`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/36 (draft)
- Diff: `git diff 16e0cb9..69bf37f` (code). This review file is the next commit.
- Tests: `uv run pytest` -> 286 passed (280 on main, plus 6 in `tests/test_daily_publish.py`)
- Tests first: `c1ee21c` adds the 6 tests, which fail on main's code. `69bf37f` makes them pass.

### What changed
- `src/cli.py`: `RUN_STEPS["daily"]` was `[["ingest", "form4"]]` and is now
  `[["ingest", "form4"], ["screen"], ["sync-vault"]]`. Weekly is unchanged:
  `ingest all, screen, fetch-text, sync-vault`.
- The ordering guarantee is `ops.run_steps`, unchanged: after a failed step the rest are
  "not run". So a failed import means no screen Parquet and no vault writes. `Reports/Health.md`
  is still written.
- All steps share one date (parser defaults from `date.today()` at build time). They run inside
  the existing `data/.run.lock`, on the run's one connection.
- `src/obsidian/sync.py`:
  - Company frontmatter gains `price_date`, the stored close the note was valued on.
  - New `_data_dates(con, as_of)`: the newest `prices.price_date`, `form4_transactions.filed` and
    `xbrl_facts.filed`, each `<= as_of`.
- `dashboard.md.j2`:
  - A "Stored data used" line with those three dates.
  - A "Close date" column in the shortlist and quarantine tables.
- `README.md`: an Operations paragraph on the daily.

### How to verify
- `uv run pytest -q tests/test_daily_publish.py` -> 6 passed. The tests pin `today` and stub the
  Form 4 import:
  - success publishes the shortlist, the company notes, the new event and the Parquet;
  - a failed import leaves every vault file byte-identical, writes no Parquet and gives exit code 1;
  - the journal note stays byte-identical, the user's text outside the AUTO block survives, and the
    dashboard shows the journal's then-vs-now row;
  - a daily 10 days after the latest close quarantines with `stale_price`;
  - the dashboard dates are shown.
- `git diff 16e0cb9..69bf37f -- src/` shows 3 source files.
- Read-only timing on `/Volumes/External/edgar-obsidian-quant-backups/edgar-2026-11-21.duckdb`
  (plain duckdb read_only, 2 GB, 2 threads; size and mtime unchanged after):
  - `_data_dates` as of 2026-11-20 returned price 2026-11-13, Form 4 2026-11-20, facts 2026-11-13;
  - it took 13.0 s cold from the External drive, then 0.06 s.
- Not checked live:
  - The full screen and sync could not run on that backup: it predates migration 9, so
    `price_actions_held` is missing.
  - A live dry run would need your sole read slot: plain duckdb read_only, `screen()` plus
    `sync_vault()` into a scratch vault, never the real vault.
  - The sync path issues no DB writes (no INSERT, UPDATE or CREATE in sync, redline, forensics,
    screen or journal).
  - Optional; say if you want it.

### Where bugs are most likely
- The failure gate rests entirely on `run_steps` stopping at the first failure. Check that
  `ingest form4` reports a partial or failed load as a raised error or a `sys.exit`, not as a
  return. For example, failed `form4_filings` rows are retried next run and only warn. Today a
  daily with some failed filings still publishes.
- `price_date` is a new auto frontmatter key, so the first sync after merge rewrites every company
  note once.
- `_data_dates` scans `xbrl_facts.filed` on each sync. It is cheap warm; check the cold cost on
  the live disk.

### Known gaps (not findings unless they are worse than stated)
- No `fetch-text` on the daily. A company first shortlisted on a weekday shows no risk-factor or
  related-party redline until Saturday.
- Weeks with a market holiday on Friday make the next Friday's close 8 days old, and a failed or
  missed Saturday weekly does the same. Those dailies quarantine every company (`stale_price`),
  as the rule says, and the notes flip status for the day. The health report already warns at
  more than 8 days.
- A screen Parquet is written every weekday: about 880 KB each, about 230 MB a year. There is no
  retention, as on the weekly.
- The daily gets about 30 s longer (weekly 11-21: screen 11.3 s, sync-vault 16.6 s).
- The scheduled checkout is untouched. The daily runs this only after merge and a pull.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
