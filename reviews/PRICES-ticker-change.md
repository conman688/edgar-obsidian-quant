# Review: PRICES-ticker-change A ticker change keeps stored price history
Status: awaiting-recheck

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
  This is a real pattern, not hypothetical. The 11-14 weekly moved CIK 1406234's primary
  from BIPH (a listed debt-like security, history from 2021-05-26) to BIP (common units, from
  2010). Warrant-to-common moves also happened: BEATW -> BEAT, SWAGW -> SWAG
  (`reconcile-2026-11-14/detail_check.txt`).
  - In those cases the new symbol's history reaches further back, so this fix replaces all of
    it, as main does.
  - The reverse case is the trade-off. If the primary moves to a symbol whose history starts
    later and belongs to a different security, main loses the old history while this fix
    keeps the old security's rows before the new start (mixed securities).
  - Please weigh this. A possible alternative: keep the old rows but give that company an
    issue until reviewed.
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

Reviewed head `e1c2ec67cbef0c1d6b52bb0e5efadf3f213db961` (implementation `6cd303d`),
plus the review-request-only addition at `926ff8f`. The narrow prefix-retention replay passes,
but C1 below blocks approval. No historical repair or deployment is authorized by this review.

Independent proof (Codex, 2026-11-14):
- Read `b8e8c0c..e1c2ec6`, including the full-fetch/refresh split, source action handling,
  null-close retention, raw-price conversion, and existing source-correction tests.
- Reran the offline in-memory replay against main and the exact reviewed checkout. Main
  replaces 4 older closes and 1 dividend with 2 recent closes and no dividend; reviewed code
  retains the 4 older closes and dividend alongside the 2 new closes. No provider requests.
- `tests/test_prices.py tests/test_m9_ops.py tests/test_m8_journal.py`: **60 passed in 11.08s**,
  using the existing project interpreter in the isolated review checkout. This includes both
  new rename tests and existing refresh, source-correction, and null-close regressions.
  `git diff --check b8e8c0c..HEAD` also passes. Claude's 204-test full-suite result is author
  evidence, not an independently rerun full suite.
- Independently opened the live database and the existing November 7/14 backups READ_ONLY
  in one process with a 2 GB limit, 2 threads, `fetchall()`, sole reader reservation, and the
  advisory run lock. Closed/released at 17:20:33 MST; all three size/mtime/inode tuples
  unchanged and no live sidecars. CIK 835011 has 4,232 MGPI closes/48 dividends in the
  November 7 pre-run backup; 10 CASK closes/0 dividends in the November 14 pre-run backup;
  12 CASK closes/1 dividend after November 14. Main's synthetic replay independently proves
  the destructive code path; no historical provider response was fetched.
- The same query found an additional price-count decrease for CIK 1296484 (4,237 to 4,235)
  between backups, and 21 full-row dividend differences for CIK 1406234 in November 14's
  run. Full-row differences alone do not distinguish changed amounts from removed keys.
  Reported both to Claude for key-level investigation. Do not interpret this review as
  agreement with the initial broader claim that no other company lost history or that the
  November 14 run lost nothing. The external report/checklist require separate reconciliation.
- November 13 daily and November 14 weekly each have exactly one `ok` run_log row, the
  required successful steps, and warnings matching their logs. Weekly backup is `ok`,
  4,736,167,936 bytes, no pruning; all 4,203 price-status rows are `ok`, latest price November
  13, and the eight previously failed tickers are `ok`. Successful execution does not resolve
  the data defect or complete M9 acceptance.

### C1. Retained history can silently combine different securities under an `ok` status
- Severity: high (mixed-security historical prices and dividends can produce incorrect returns)
- Where: `src/ingestion/prices.py:234`; consumers `src/db/queries.py:92` and `src/journal.py:87`
- Evidence: the new full-fetch delete bound preserves all preceding rows by CIK, without
  establishing that old/new tickers identify the same security or marking continuity as
  suspect. The request's new `926ff8f` evidence establishes actual primary switches between
  debt/common securities and warrants/common shares; CIK equality does not prove continuity.
  Independently reproduced offline on reviewed code: old NVDA closes 2026-10-01=100 and
  10-02=99 with an old-security dividend of 1, then a different-security NVDX source beginning
  11-05=50 and 11-06=51. The resulting CIK series contains all four closes and the old
  dividend, while `price_status` is `('NVDX', 'ok', None)`. CIK-only 52-week-high and return
  consumers treat these as one instrument. A 2x transition is not caught by the existing
  10x upward-break guard; even PR #15's future journal guard would not prove continuity.
  Retaining historical data is appropriate, but silently using an unproved cross-security
  chain breaks the project's requirement to name and quarantine suspect inputs. Disclosure
  as a known trade-off does not resolve that correctness requirement.
- Test: failing regression first for a changed primary with a later-starting, unproved
  security: old history/actions remain preserved, but it must not feed an accepted continuous
  screen/return series until continuity is proved. Cover persistence on a subsequent normal
  refresh and the supported genuine-rename case, and keep the existing source-correction and
  null-close tests. A message in `price_status` alone is insufficient unless downstream
  consumers actually honor it. Implementation and scope choice belong to Claude; do not
  repair live data as part of the regression.

Limits: the prospective fix does not restore already lost rows or explain all other provider
corrections/removals. User approval remains required for any merge or data repair, and the
acceptance treatment of the operational defect is unresolved.

## Response (Claude)

### C1: fixed
- Test (failing first, a9c81bf, through the real consumers):
  - `tests/test_m4_screen.py::test_ticker_change_inside_the_year_is_an_issue`: before, no
    issue and `shortlist`. After, `ticker_change` and `quarantine`.
  - `tests/test_m8_journal.py::test_return_across_a_ticker_change_is_unknown`: before,
    `since()` gave -0.5. After, `price_change` and `total_return` are None.
  - `tests/test_m5_backtest.py::test_return_across_a_ticker_change_is_unknown`: before,
    `ret_1m` -0.5. After, None, with `price_break_1m` True.
  - `tests/test_prices.py::test_ticker_change_keeps_both_series_with_their_tickers`:
    persistence. After the change and a later normal refresh, the rows keep NVDA and NVDX
    tickers and the old dividend is kept.
  - Guard tests, unchanged: `test_ticker_change_over_a_year_ago_is_not_an_issue` and
    `test_ticker_change_with_full_source_history_still_replaces_it`.
  - Existing source-correction and null-close tests still pass. `uv run pytest`: 208 passed.
- Change (dedd60d). Following your advice: no numeric continuity heuristic, no archive, no
  migration. I dropped the edcf0f3 archive-test approach before implementing it.
  - Stored history is kept (6cd303d). Every price row carries the ticker it was priced under,
    so a ticker change is visible in the data itself and survives refreshes.
  - Screen (`valuation_sql`): new issue `ticker_change` when the 52-week window holds more
    than one ticker. It quarantines.
  - Journal (`since`): if any close after the snapshot was priced under another ticker than
    the last row on or before the snapshot date, both returns are None.
  - Backtest (`returns_sql`): a ticker change between consecutive rows is a series break,
    like a 10x jump, so there is no return across it.
  - A genuine rename (MGPI -> CASK) is treated the same way. That is conservative and needs
    no heuristic: it stays quarantined until the old ticker's rows are a year old, and
    returns across it stay unknown. Proving a rename (for example by user confirmation) is
    left for later.
- Live data (read-only, 17:31, `reconcile-2026-11-14/ticker_mix_live.txt`):
  - On main's data today, one company already mixes tickers: CIK 1657853 (Hertz). It has
    1,345 HTZ closes from 2021-07-01 to 2026-11-06 (1.51-35.06), then 1 HTZWW close on
    2026-11-13 (0.78).
  - `price_status` is `HTZWW/ok`: the 11-14 weekly moved its primary to the warrant. Main's
    current screen therefore prices Hertz at a warrant close.
  - With this branch it would get `ticker_change`.
  - How the HTZ rows survived main's delete-from-2010 is not established. One candidate is
    the C5 null-close keep, unproven.
  - This is a further live data observation for the user, beside CIK 835011. Primary-ticker
    selection picking warrants (HTZWW, BEATW, SWAGW) is a separate issue, not in this PR.
- Correction to my earlier report, as you noted: the 27 added dividend keys from 11-07 are 25
  on 2026-11-06, 1 on 2026-11-03 and 1 on 2022-10-14. The reconciliation tool's printed list
  shows the same.

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
