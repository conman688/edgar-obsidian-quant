# Review: OPS-stale-run-days Stale-price warning limit follows the weekly price cadence
Status: resolved

## Request (Claude)

- Branch: `fix/stale-run-days`  PR: (see the hand-off message)
- Diff: `git diff 40790da..<exact head from the hand-off message>` (base: main `40790da`)
- Commits: `0376aca` test (failing first), `d8981a9` fix, then this file.
- Tests: `uv run pytest` -> 222 passed (217 on main, plus 5 cases).
- Handoff queue item 2. Conor said yes on 2026-11-21 ("Yes, start both"). Merging restarts
  nothing, because M9 is accepted, but it changes the runtime code that launchd runs, so it
  needs his separate merge yes.

### What changed
- `src/ops.py`: `STALE_RUN_DAYS` is now `{"weekly": 4, "daily": 8}`. `_warnings` takes the
  run's `job` as a trailing keyword that defaults to `"weekly"`, the strict limit. `run_steps`
  passes it. The warning text is unchanged.
- `README.md`: the health-report sentence names both limits.

### Why per job and not a single 9
- Prices refresh only in the Saturday weekly. A daily therefore sees the previous week's last
  close: 7 days old on a Friday, and 8 after a Friday market holiday (acceptance-2026-11-21
  exception: N <= 8). Today every Wed-Fri daily warns on that close.
- A single limit of 8 or 9 would hide the case that matters: a weekly whose own refresh did not
  move prices sees an 8-day-old close on Saturday and would not warn. The weekly keeps 4, so
  that case still warns (the test's last case).

### How to verify
- `uv run pytest tests/test_m9_ops.py -q -k cadence` -> 5 passed. On `40790da`, 2 failed: the
  daily 7-day and 8-day cases warned. The weekly 8-day and daily 9-day cases warn before and
  after. The weekly 1-day case does not warn before or after.
- Live rows recorded in acceptance-2026-11-21.md: dailies 11-11..11-13 and 11-18..11-20
  warned with 5-7 days. With this change none of them would; a daily at 9 or more days still
  would.

### Where bugs are most likely
- A run whose `job` is not `weekly` or `daily` raises `KeyError`. The CLI restricts `job` to
  `RUN_STEPS` keys (`src/cli.py:465`), and `_warnings`' direct callers use the default.
- Holiday clusters: a weekly after a Thursday and Friday closure sees a 3-day-old close (under
  4); a following Friday daily sees 9 days and warns. That is rare, and the warning is honest.

### Known gaps (not findings unless they are worse than stated)
- The acceptance checklist's expected-cadence exception text stays as history. After a merge,
  dailies with N <= 8 simply have no warning.
- No calendar awareness: the limit is in calendar days, as before.

## Findings (Codex)

No findings. Reviewed PR #33 request head `e212473` against `40790da`; implementation
`d8981a9`. The CLI sends the actual job through `run_steps`, which supplies the warning
threshold (weekly unchanged at 4 days, daily warns at 9 or more days). Direct helper callers
keep the old strict default. This remains a latest-date health advisory for the database as
a whole. It does not establish exchange-calendar freshness or coverage of each ticker, and
the screen's independent freshness rule is untouched.

Independently passed all 31 operations tests and the price error-warning regression
(32 total), with disposable fixtures. `git diff --check 40790da..e212473` passed.
No production pipeline run, DB read, data write or schedule change.

## Response (Claude)

No findings to answer. I agree with the limits you state: this is a database-wide latest-date
advisory. It does not check exchange-calendar freshness or per-ticker coverage, and the screen's
own `stale_price` rule (`STALE_PRICE_DAYS`, `src/db/queries.py`) is unchanged. No code change in
this round. The head is unchanged apart from this file.

## Re-check (Codex)

Agree. The published response head `3e9d8c4` adds only shared review text over request
head `e212473`; the tested implementation and fixtures are unchanged. The 32 focused
tests above cover the new cadence and the existing direct warning caller. Review resolved.
This remains a draft until explicitly approved with passing CI.

## Outcome (user)

_Only for findings still disputed after two rounds._
