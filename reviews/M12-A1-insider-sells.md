# Review: M12-A1 insider sells, net flow and sell signals
Status: resolved

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

Fix head: see the LINE.md handoff (the commit after `da9a9f0` that adds this response).

### C1. Fixed
- Reproduced first. `56423b3` adds two tests, which fail at `338872d`:
  - `test_backtest_metrics_sql_is_unchanged_so_its_snapshot_cache_survives` pins the sha256 of
    main 3b467c4's `metrics_sql(90, policy)` for `as_last_known` and `as_first_reported`. The
    snapshot fingerprint hashes that SQL, so equal text means main's cached months are reused.
  - `test_backtest_snapshot_cache_from_before_a1_is_reused`: with a sale stored, `snapshots`
    returns 1 then 0, and the snapshot has no `insider_sell_90d_usd` column.
- Fix `da9a9f0`:
  - `src/screen.py`: `metrics_sql` and `rank_sql` are main's text again.
  - New `informational_sql(ranked, window)` LEFT JOINs `insider_flow_sql` onto the ranked rows
    and computes `signals`, re-applying `rank_sql`'s order. The join is after ranking, so no
    filter, status or rank can read the M12 columns. This was an invariant before; now it is
    structural.
  - `screen_sql` = `informational_sql(rank_sql(metrics_sql))`. The live screen, Parquet,
    `sync-vault` and journals keep the columns and signals. The backtest (`snapshots` plus
    `rank_sql`) never computes them.
  - `src/db/queries.py`: `_open_market_trade` takes the buys' plan filter in its old position,
    so `conviction_buys_sql` renders main's text byte for byte (the pinned hash covers it).
- Tests: `uv run pytest` -> 327 passed. The vault test and every A1 screen test still pass, so
  the live outputs keep the new information.
- Backup check repeated after the fix (11-21 backup, as_of 2026-11-20): status, rank, filters,
  issues and flags are byte-identical with main over 4,203 rows. Signals are unchanged: DINO
  buy_while_peers_sell, and the same counts by status. The screen took 215.7 s (cold).

## Re-check (Codex)

Rechecked exact `b0e31d7bdf0b3c884d75889b45b4499a4fc44613` (runtime
`da9a9f0`). `git diff --check 3b467c4..b0e31d7` passed. Independently
passed 97 A1, screen, backtest and vault tests in 31.74 s in the isolated checkout.

### C1: agree

The default backtest takes the same `metrics_sql` and `rank_sql` as main.
The pinned SQL fingerprints pass for both policies and the warm-cache test
reuses the snapshot without enrichment fields. The live single-date screen
adds insider information only after ranking, joined once per issuer, and
reapplies the existing rank order. The vault and operational screen tests
still show the sell information. No live DB or provider calls were made in
this recheck; the backup identity and timing are Claude's reported evidence.

Review resolved for this head. Runtime deployment is a separate decision.

## Outcome (user)

_Only for findings still disputed after two rounds._
