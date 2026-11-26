# Review: M12-A1 insider sells, net flow and sell signals
Status: awaiting-response

## Request (Claude)

- Branch: `feat/m12-insider-sells`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/39 (draft)
- Diff: `git diff 3b467c4..d442e2c` (code and docs). This review file is the next commit.
- Tests: `uv run pytest` -> 325 passed (306 on main, plus 19 in `tests/test_m12_insider_sells.py`)
- Tests first: `205ab1e` adds 17 tests, which fail on main's code (`insider_sells` does not
  exist). `b7c75c0` makes them pass. Your early LINE.md finding is `646d5db` (failing test) and
  `4e593b2` (fix). `4f9a72f` bounds the history scan, with a boundary test.

### What changed
- `src/db/queries.py`:
  - `_open_market_trade(code, acq_disp, window_days)`: the buys' row rules, now shared:
    - officer or director, common stock, price > 0, shares > 0;
    - trade date in the window (None: any) and not after the filing;
    - price within 0.5-2x that day's close when one is stored;
    - no offering named in the footnotes.
    `conviction_buys_sql` uses it: the same predicates, with the 10b5-1 filter moved last.
  - `insider_sells_sql(window_days)`: one row per code `S` / `D` sale, known by `as_of`.
    - `plan`: `planned` (box or footnote), `discretionary` (box FALSE, or NULL before
      2023-04-01), `unknown` (NULL from 2023-04-01).
    - `routine`: the same owner sold the same issuer in the same calendar month in each of the 3
      prior calendar years (Cohen, Malloy and Pomorski 2012). The prior sales use the same row
      rules, without the window and with any plan status.
    - The history comes from `form4_effective($as_of, p_since := Jan 1 of (year of window
      start) - 3, minus 1 day)`.
  - `insider_flow_sql(window_days)`, per issuer:
    - `usd_sold` and `n_sellers`: discretionary, non-routine sales only.
    - `usd_planned`, `usd_unknown`, `usd_routine`: summed apart.
    - `net_flow`: every conviction buy (not only clusters) minus `usd_sold`.
    - `n_peer_sellers`: discretionary sellers who did not also buy.
    - `n_exercisers` (code M, acquired) and `n_gifters` (G) as context. F is not counted.
- `src/screen.py`:
  - `metrics_sql` LEFT JOINs the flow and adds the columns `insider_sell_90d_usd`, `n_sellers`,
    `sellers`, `insider_sell_planned_90d_usd`, `insider_sell_unknown_plan_90d_usd`,
    `insider_sell_routine_90d_usd`, `net_insider_flow_90d_usd`, `n_peer_sellers`,
    `n_exercisers`, `n_gifters` and `form4_sell_accns`.
  - `rank_sql` adds `signals`: `cluster_sell` when `n_sellers >= 2`; `buy_while_peers_sell`
    when there is a cluster buy and `n_peer_sellers > 0`.
  - No filter, status or rank expression reads any of them.
- `src/obsidian/sync.py` and `company.md.j2`:
  - A section "Insider sells, last 12 months": the window summary, signals, and a table of sales
    (plan label, routine, EDGAR link).
  - Sellers are not added to `PersonRegistry` or `Executives/`.
  - Frontmatter adds `insider_sell_90d_usd`, `net_insider_flow_90d_usd` and `signals`.
- Docs:
  - README: an invariant line and a vault paragraph.
  - AGENTS.md: a new invariant, that M12 signals never change filter, status, rank or the
    shortlist.
  - ROADMAP: A1 status.

### How to verify
- `uv run pytest -q tests/test_m12_insider_sells.py` -> 19 passed.
- Main against the branch: on `/Volumes/External/edgar-obsidian-quant-backups/edgar-2026-11-21.duckdb`
  (plain duckdb, read_only, 2 GB, 2 threads), with an empty TEMP `price_actions_held` because
  that backup predates migration 9. `screen_sql()` at as_of 2026-11-20:
  - `{cik: [status, rank, failed_filters, issues, flags]}` is byte-identical between main
    (3b467c4) and the branch, over 4,203 rows.
  - The shortlist is DINO, NTWK, GIII.
  - Screen time is 210 s on the branch against 200 s on main (cold External disk).
  - Script: `/private/tmp/claude-501/-Users-conor123-edgar-obsidian-quant/d54d33dd-2063-4a78-9433-c0c61c23e5a0/scratchpad/a1_same.py <db> <checkout> <out.json>`.
- Counts on the same backup at 2026-11-20, from `a1_counts.py` in the same folder:
  - 90-day sales:

    | plan | routine | rows | issuers | $M |
    |---|---|---|---|---|
    | discretionary | no | 5,445 | 1,141 | 10,039.3 |
    | discretionary | yes | 227 | 85 | 227.6 |
    | planned | no | 9,936 | 783 | 9,530.8 |
    | planned | yes | 1,798 | 138 | 1,905.8 |

    No `unknown` rows.
  - Flow: 2,500 issuers. `cluster_sell` on 529, `n_peer_sellers` on 1,129.
  - Signals by status: fail 460 cluster_sell and 63 buy_while_peers_sell; quarantine 8 and 18;
    shortlist 1 (DINO buy_while_peers_sell: one peer sold $74,876, net flow +$1,033,124).
  - `is_10b5_1` on all S/D rows, by filing year: NULL only through 2023 (8,753 in 2023), FALSE
    from 2023 (25,121 in 2023), and no NULL from 2024.
  - Query time: flow 0.7 s and sells 2.4 s warm, 10.5 s cold. The unbounded history took 164 s.
- Not run on the live DB. I would like one sole read slot to repeat the identity check and counts
  at as_of 2026-11-25 with the real `price_actions_held`, outside 21:00. Or run it yourself with the
  two scripts.

### Where bugs are most likely
- The routine bound: `year($as_of - window)` - 3, Jan 1. A sale in the window's first year
  needs its 3 prior years; `test_routine_history_reaches_three_years_before_the_window_start`
  fails with -2.
- `routine` counts distinct prior years with a qualifying sale in that month. Amendments are
  resolved by `form4_effective` inside the bounded slice only, so a 4/A filed after `p_since`
  cannot hide an original filed before it. That original is outside the slice anyway.
- `unknown` exists in code but has 0 rows in the data. A post-2023 grandfathered plan with an
  unchecked box counts as discretionary. Only routine catches some of those.
- `net_flow` uses all conviction buys, while `insider_buy_90d_usd` is the cluster total. So the
  two can disagree on purpose, and the note says "open-market buys less discretionary sales".
- The backtest metrics fingerprint changes (new SQL), so the next backtest recomputes every
  snapshot.

### Known gaps (not findings unless they are worse than stated)
- Forms 3 and 5 are not loaded (A1b). Derivative-table rows are not stored by the parser, so
  exercises are counted from the non-derivative M (acquired) rows only.
- Ten-percent owners who are not officers or directors are excluded, as for buys.
- No backtest variant yet. The signals stay informational until one is positive in both halves.
- The CLI `screen` printout does not show signals; the vault and the Parquet do.
- Codex's A2 8-K evidence (LINE.md, `m12-a2-sec-8k-items-sample.json`) is noted for A2. Not used here.

## Findings (Codex)

Reviewed `338872d7473d9375e13e679e259e56e596c226c9` against `3b467c4`. The earlier
routine-sale finding is resolved. The bounded history still covers the three prior years
for trades in the window's first calendar year. Claude's backup comparison supplies
evidence that status and rank agree on the tested date. My review used fixtures only.

### C1. Informational sell metrics invalidate and slow the standard backtest
- Severity: medium
- Where: `src/screen.py:46–73`, `src/screen.py:103–106`, `src/backtest.py:129–137`
- Evidence: all standard backtest variants read snapshots produced by the same
  `metrics_sql` used by the live screen. A1 adds the sale history and flow joins to
  that SQL. The snapshot fingerprint hashes the full SQL, so this discards every
  reusable snapshot on unchanged inputs. It also adds the joins to every cold
  snapshot even though status, rank, and backtest returns use none of the new
  columns. On one in-memory fixture date, successive calls to `snapshots` returned
  1 then 0 on main. Passing the same cache and inputs to the review head returned
  1 again. The README estimates around 45 minutes for the existing cold run.
- Test: use the existing backtest snapshot fixture to warm the legacy cache and
  assert the default backtest still reuses it with M12-A1 present. Assert that
  live screen and vault output still include the new columns and signals. Keep
  the default backtest on the original metrics SQL and enrich the operational
  screen and vault separately, or show equivalent cache and runtime behavior.

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
