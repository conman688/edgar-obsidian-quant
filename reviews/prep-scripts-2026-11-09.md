# Review: acceptance prep scripts (reconcile_dividends.py, deep_check.py)
Status: awaiting-review

Review-only branch `review/prep-scripts-2026-11-09` (from main b8e8c0c; worktree
/private/tmp/edgar-prep-review). It carries only this file: no PR, no runtime change, never merged.
The scripts live in `/Users/conor123/edgar-obsidian-quant-checks` (not a git repository); the hashes
below pin them. Codex records findings and re-checks here and commits/pushes only this file. Claude
owns and edits the scripts and answers here with new hashes. Statuses as in `reviews/PROTOCOL.md`.

## Request (Claude, 2026-11-09 20:25 MST)

Files under review (SHA-256 at handoff):

| File | SHA-256 |
|---|---|
| `reconcile_dividends.py` | `2164f873891b87c61ec183ad8961e9b24677fe07ffb8af9f6f9d93874732318e` |
| `test_reconcile_dividends.py` | `7eed619652f855496893b0d2973dd1edb001aaea9b410c80485c3b7f060f47b6` |
| `deep_check.py` | `a3af31567edc24c4810c460070c043197a3785bb6853f415171e4fa90224475e` |
| `test_deep_check.py` | `5f2f99386bada1669d460862f0bfa0e5744e602dd7a93d819cd41407645fe74c` |
| `dividends-2026-11-07.md` (two lines corrected) | `4294bb4a647f6bd0e2ebcdaef6ba3f6b0feb9cee07198f4c853bd199a519d681` |

What they do:
- `reconcile_dividends.py OLD NEW`: attaches both backups READ_ONLY to an in-memory DuckDB (2 GB, 2
  threads), EXCEPT both ways, splits into removed keys / added keys / amount changed, and per removed
  key prints the close on ex_date in OLD and NEW, the nearest added row for the same cik within 10
  days (|days|, ties to the earlier date) with its amount, and OLD's approximate window flag. It also
  lists non-daily runs in NEW's run_log after OLD's last run (possible price writers). Output is
  observations only; no causal verdicts (revised after Codex's notes: "moved" removed, min(ex_date)
  replaced by nearest, amount shown).
- `deep_check.py job YYYY-MM-DD`: the checklist in `acceptance-2026-11-21.md` for one run: launchctl
  print, lstat/stat DB vs backup device, External volume UUID, internal free, run_log rows that day
  (one row, status, steps, warnings, counts delta vs the previous run_log row), weekly backup
  (status, name, size on disk = logged bytes, at most 4 kept), price_status errors and the 8 tickers
  from 11-07, the log segment for that run (tracebacks, lines to read), Health.md frontmatter and body
  against run_log and the log's Health block. It opens the live DB once, read_only, and refuses when
  `lsof data/.run.lock` shows a holder, a `src.cli run` process exists, or a scheduled start is
  within 20 minutes.
- `dividends-2026-11-07.md`: the PR #23 line now says merged (e3dd0c5) and that the 11-07 weekly ran
  `aec2e3a`; decision step 2 now carries the historical-code condition (delete-then-insert at
  aec2e3a; a surviving close excludes the NaN-close drop for that row only if that weekly is the only
  price writer; a missing close is consistent with (b), not proof; no backward projection of PR #23).

How it was verified:
- `test_reconcile_dividends.py <empty dir>`: synthetic OLD/NEW files; covers removed with no close in
  NEW, nearest-vs-min selection, a tie, other/same amount, amount change, writers list, identity case.
  Passes.
- Real dry-run `reconcile_dividends.py edgar-2026-11-05.duckdb edgar-2026-11-07.duckdb`: both 63,310
  rows / 1,566 ciks, EXCEPT 0 both ways, writers = the 11-05 22:15 weekly; 0.8 s; both backups' size
  and mtime unchanged.
- `test_deep_check.py <empty dir>`: fixture log Z (11-06) + A (11-09 output, Health) + B (11-10 output with a
  Traceback, Health). Written first; on the earlier deep_check.py (12c1784c…) A's segment contained B's
  Traceback (Codex's chat finding, reproduced). Fixed: `health_end` bounds each Health block at the last
  `|` row after `## Row counts`; `log_segment` returns (output from the previous block's end to this
  header, this Health block). Passes.
- `deep_check.py weekly 2026-11-07`: no flags (752 s, 4 steps ok, dividends 63,310 -> 63,289 marked
  for explanation, backup 4,736,167,936 bytes = logged, 8 tickers all no_data). `deep_check.py daily
  2026-11-06`: flags only Health.md, which the 11-07 weekly overwrote (expected). Live DB size and
  mtime unchanged (4,736,167,936, 2026-11-08 20:26:06).

Where bugs are most likely:
- `deep_check.health_end` / `log_segment`: block bounds rely on render_health ending with the
  `## Row counts` table; a truncated block (run killed mid-print) stops at the next header or EOF.
  The log is appended by launchd; a manual run printing to a terminal would not appear there.
- `deep_check.next_start` guard: Python weekdays (Mon=0, Sat=5) against the plists (launchd
  Weekday 1-5 for daily, 6 for weekly).
- `deep_check` "exactly one run_log row that day": a weekly's started can slip past 07:00 but not
  past midnight; a manual run the same day would show as a second row.
- Chat notes already applied before this request (Codex, 20:1x): no causal verdicts; nearest not
  min(ex_date); amount shown; historical-code condition (aec2e3a delete-then-insert) in the docstring
  and in dividends-2026-11-07.md step 2; PR #23 line corrected.
- `reconcile_dividends` writers query assumes OLD's run_log max(started) is the last run in OLD.
- Health.md body comparison is exact string equality after strip.

Known gaps: deep_check cannot prove launchd started the run (it shows launchd's run count and the log;
the run count must be compared with the previous check by hand). It does not judge whether a warning
is external and self-recovering.

## Findings (Codex)

## Response (Claude)
