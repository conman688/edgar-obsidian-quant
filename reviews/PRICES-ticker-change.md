# Review: PRICES-ticker-change A ticker change keeps stored price history
Status: awaiting-review

## Request (Claude)

- Branch: `fix/ticker-change-keeps-history`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/30
- Diff: `git diff b8e8c0c..6cd303d` (base: main b8e8c0c, whose runtime code is e3dd0c5)
- Tests: `uv run pytest` -> 204 passed (202 on main, plus 2 new in `tests/test_prices.py`)
- Commits: e2591fc test (failing), 6cd303d fix.
- Found by: the 11-07 -> 11-14 dividend reconciliation,
  `/Users/conor123/edgar-obsidian-quant-checks/reconcile-2026-11-14.md` (with evidence and
  SHA256SUMS).

### What changed
- `src/ingestion/prices.py` `_store`: a window's delete start is now `start` for a refresh,
  unchanged, and the source's first returned row (`raw.index[0]`) for a full fetch
  (`start == HISTORY_START`: a first load or a changed primary ticker).
- Before: a full fetch deleted prices, splits and dividends from 2010-01-01, so a renamed
  symbol whose source history begins at the rename wiped everything stored before it.

### How to verify
- `uv run pytest tests/test_prices.py -k ticker_change`:
  - `test_ticker_change_keeps_history_before_the_new_symbol`: before the fix, 0 of the 4 old
    rows were kept (`assert [(0,)] == [(4,)]`). It passes after.
  - `test_ticker_change_with_full_source_history_still_replaces_it`: passes before and after.
    It guards that a new symbol covering the stored dates is still authoritative.
- Offline repro of the incident, run against both checkouts: `repro_ticker_change_any.py
  <repo>` in the checks folder (in-memory DB, fake source, no network).
  - main: `repro_output.txt`, history lost: True.
  - this branch: `repro_output_fixed.txt`, 6 rows (4 old plus 2 new) and the old dividend
    kept.
- Live data (read-only, `reconcile-2026-11-14/cik835011.txt`):
  - CIK 835011 had 4,232 rows (2010-01-04..2026-11-05) and 48 dividends in the 11-07 backup,
    and 10 rows from 2026-10-26 and 0 dividends in the 11-14 backup.
  - No other CIK lost more than 5 price rows between 11-07 and 11-14, and none lost any in
    the 11-14 weekly.

### Where bugs are most likely
- A primary-ticker change to a **different security** whose source history starts later: old
  rows stay before the new first date. Mixed-security prices would then be caught by the
  existing 10x `price_break` guard only if the jump is that large.
- `raw.index[0]` includes source rows without a close (C5). Deleting from the first row, not
  the first close, keeps the C5 "keep a stored close on null-close days" behaviour.
- `price_status.first_date` uses `least()`, so it stays the oldest kept date. That is now true
  again.

### Known gaps (not findings unless they are worse than stated)
- No live data repair here. Restoring CIK 835011's 4,222 rows and 48 dividends from
  `edgar-2026-11-07.duckdb` is a separate data fix, the user's call (defer, or fix now and
  restart the window).
- The stored rows before the rename keep ticker `MGPI` in `prices.ticker`. Screens join on
  `cik`.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
