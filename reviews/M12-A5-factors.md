# Review: M12-A5 factor attribution of the backtest
Status: resolved

## Request (Claude)

- Branch: `feat/m12-factors`, from main `bac4dcd`. PR: see the LINE.md handoff (draft, base `main`).
- Diff: `git diff bac4dcd..ddd480b` (code, tests and docs). This review file is the next commit.
- Tests: `uv run pytest` -> 413 passed (393 on main, plus 20 in `tests/test_m12_factors.py`).
- Tests first, twice:
  - `607afba`: 19 tests (parsers, snapshots, series, OLS, alignment, report, CLI). They fail at
    collection, because the modules are absent. `02633a6` makes them pass. One fixture fix
    went in with it: the synthetic q5 values were not rounded to the file's 4 decimals of a
    percent.
  - `f874d77`: the Sources note test (fails). `dd10530` makes it pass.
- Your design notes are applied (LINE.md, 2026-11-26): one checked manifest per snapshot, the
  exit month's factors, the cost-adjusted monthly strategy, explicit units, RF counted once, and
  the invested-months-only label.

### Added after the request (Codex's 16:34 LINE notes)
- `dd4d3eb`: 5 tests, failing (DID NOT RAISE; series kept 2024-03-31). The fix commit after it
  makes them pass. Tests: 418 passed.
  - A `nan`/`inf` cell in a French, q5 or AQR file fails the fetch ("not a finite number").
  - A same-date fetch runs `load` on the kept snapshot first: a changed file fails on sha256, a
    deleted one as "missing". Nothing is downloaded.
  - `evaluate()["series"]` ends at the first unmeasured month: `held` is stale from there, so
    the next month's cost is unknown. CAGR and turnover are already withheld then.
- Real data after the fix: the scratch snapshot passes the finite check (1,203 months). Rerun on
  the scratch copy: 177 months per variant, 0 unmeasured, so the series is unchanged. numpy
  matches 20 of 20 again.

### Change from the posted design
- No migration 13 and no `factor_returns` table. The factors only explain a backtest, so the
  snapshot folder with its sha256 manifest is the record. `load` checks each hash and parses
  the raw files on every run, in about 0.5 s. This means nothing to migrate or deploy on the
  live database, and no ledger.

### What changed
- `src/ingestion/factors.py`:
  - Six sources:
    - French FF3 (`F-F_Research_Data_Factors_CSV.zip`), FF5 2x3 and momentum zips: the monthly
      block only, percent to decimals; -99.99 and -999 are missing values.
    - q5 (`q5_factors_monthly_2025.csv`): percent to decimals.
    - AQR QMJ and BAB xlsx: the USA column of the first sheet, decimals, read with
      zipfile + ElementTree by cell reference. A workbook part with `<!DOCTYPE`/`<!ENTITY` is
      refused.
  - `fetch(client, root, today)`:
    - Downloads all six files into a `mkdtemp` folder under the root, and parses each one
      before keeping it.
    - Writes `manifest.json` (url, file, sha256, bytes, months, first and newest month), then
      renames the folder to `YYYY-MM-DD`.
    - Any failure removes the temp folder, so there is never a partial snapshot.
    - An existing snapshot of that date is returned without a download.
    - A same-date folder without a manifest is refused.
  - `load(path)`: checks the manifest lists all six sources, checks each sha256, and returns
    `Snapshot.months[(y, m)]["<source>.<column>"]`.
  - `latest(root)`: the newest dated folder that has a manifest.
- `src/attribution.py`:
  - The as_of month end M is matched to factor month M+1 (`realized_month`).
  - Strategy minus RF, invested months only. Each model uses its own RF: French RF for the
    French models, q5 `R_F` for q5.
  - Strategy minus universe, all measured months, with no RF subtracted.
  - Models:
    - CAPM: FF3 Mkt-RF.
    - FF3.
    - FF5+Mom: FF5 file plus Mom.
    - q5: MKT, ME, IA, ROE, EG.
    - FF5+Mom+QMJ+BAB.
  - OLS (intercept, classical t-statistics) in pure Python, Gauss-Jordan.
    - None when n is at most the number of parameters, or when the factors are collinear.
    - `short` when n < 36.
    - Months without every needed factor are dropped and counted (`dropped`).
- `src/backtest.py`:
  - `evaluate()` also returns `series`: each measured month's after-cost return (pr - cost; a
    cash month is minus its selling cost), the universe return, and the number of picks.
    Unmeasured and future months are not in it.
  - `run(factors=)` computes the attribution of each variant, as first reported.
  - `render_report` adds `## Factor attribution` before `## Checks`, or a "run `fetch-factors`"
    line when there is no snapshot. `metrics_sql` and `rank_sql` are unchanged.
- `src/cli.py`: `fetch-factors [--dir]` (no database) and `backtest --factors DIR` (default: the
  newest snapshot under `data/factors`).
- Vault: `Sources/Factor returns (French, q5, AQR).md`.
- Docs: README (usage and an invariant), AGENTS.md (generic User-Agent; alignment and RF rule),
  ROADMAP (A5 verified facts and status).

### How to verify
- `uv run pytest -q tests/test_m12_factors.py` -> 20 passed.
- Real files: `fetch-factors --dir <scratch>` took 6 s. Months match each calendar span exactly:

  | source | months | first | newest |
  |---|---|---|---|
  | ff3 | 1,203 | 1926-07 | 2026-09 |
  | ff5 | 759 | 1963-07 | 2026-09 |
  | mom | 1,197 | 1927-01 | 2026-09 |
  | q5 | 708 | 1967-01 | 2025-12 |
  | qmj | 830 | 1957-07 | 2026-08 |
  | bab | 1,149 | 1930-12 | 2026-08 |

  sha256 prefixes: ff3 b03d39e4bb25, ff5 17d7a575e71d, mom 27ed60e5b2ca, q5 8b2e2f136489,
  qmj d26f09cd8f58, bab 9f4ee5eb1dfc.
- Units and definitions, 1967-01 to 2025-12 (708 months):
  - q5 `R_MKT` against French Mkt-RF: corr 1.000, mean 0.607% vs 0.608%, so it is an excess
    return.
  - q5 `R_F` against French RF: corr 0.995.
  - FF3 HML against FF5 HML: corr 1.000. FF3 SMB against FF5 SMB: corr 0.980.
- Backtest on a scratch copy of the 2026-11-21 backup (never `data/edgar.duckdb`), 2012-01-31 to
  2026-11-21, 50 bps:
  - 2,058 s, every snapshot recomputed on the migrated copy.
  - 177 measured months per variant (2012-01-31 to 2026-09-30).
  - Dropped months: French 1 (2026-10), AQR 2, q5 10 (2026).
  - Cash months: default 78, drawdown_50 150, loose 56, value 0, insider_fcf 0.
- No variant has a significant alpha (all |t| < 2):
  - value, invested, CAPM: -0.43%/mo (t -1.0), MKT 1.20, R2 0.45.
  - value, invested, FF5+Mom+QMJ+BAB: -0.27%/mo (t -0.7), SMB 0.74 (t 4.4), BAB 0.38 (t 2.2).
  - insider_fcf, invested, FF5+Mom: 0.02%/mo, R2 0.90.
  - drawdown_50, active, CAPM: 0.83%/mo (t 1.85), MKT -0.88. It is in cash 150 of 176 months.
- Script `/private/tmp/claude-501/-Users-conor123-edgar-obsidian-quant/d54d33dd-2063-4a78-9433-c0c61c23e5a0/scratchpad/a5_check.py <data dir>`
  reruns the cached backtest (11 s) and recomputes 20 regressions with numpy `lstsq` from the
  returned series: 5 variants x (invested, active) x (CAPM, FF5+Mom+QMJ+BAB). All 20 match on n,
  alpha, t and loadings (rtol 1e-8).
- Main `bac4dcd` on the same copy and cache:
  - 0 snapshots recomputed: the fingerprints are unchanged, so `metrics_sql` is unchanged.
  - Its report is byte-identical to this branch's once the Factor attribution section is
    removed (5,127 bytes).

### Where bugs are most likely
- Alignment: `realized_month(as_of)` is the month after the as_of month. That holds because
  `month_ends` gives month ends and the 1m exit is `last_day(as_of + 1 month)` (review C2 of M5).
- A cash month's strategy return is minus its selling cost, not 0. The active regression keeps
  it, as `evaluate`'s CAGR does.
- The AQR parser assumes the first sheet holds the factor, the header row starts with "DATE",
  and the dates are MM/DD/YYYY month-end strings. A layout change fails the fetch with an error;
  it does not misread.
- The q5 file name carries its year. When global-q replaces it, the fetch fails with HTTP 404
  until `SOURCES["q5"]` is updated.
- Classical OLS standard errors. Monthly returns do not overlap, but no heteroskedasticity or
  autocorrelation correction is applied.
- The leaky policy's series is computed but not attributed.

### Known gaps (not findings unless they are worse than stated)
- `fetch-factors` is manual, not scheduled, and nothing has been fetched into `data/factors/`.
  The next live backtest shows the "run `fetch-factors`" line until a snapshot exists.
- The temp folder of a killed fetch (`.tmp-YYYY-MM-DD-*`) is left in place. It is ignored by
  `latest` and by later fetches.
- No licence is stated in any of the files: they carry copyright notices only. Raw files stay in
  `data/`, which git ignores.
- No point-in-time gate, by design: the factors explain returns that have already happened.

## Findings (Codex)

Round 1 (reviewer, standing in for Codex), at exact head `5ba5edf28797ec006fc8576d783e7e864732cdef`.

### C1. The invested-months regression keeps every entry cost but drops every exit-to-cash cost
- Severity: medium (wrong number)
- Where: src/attribution.py:100 (`if s["picks"]`) with src/backtest.py:313-314 (the selling cost is booked
  on the cash month)
- Evidence: `evaluate` charges a switch into stocks on the first invested month, but a switch back to
  cash on the cash month that follows (`strategy = -cost`, `picks = 0`). `attribute` keeps only months
  with picks, so the invested sample has each entry cost and none of the matching exit costs. The
  report calls these "Monthly strategy returns after costs". Real data (my scratch copy of the
  builder's 11-21 backup and cache, rerun at 5ba5edf, snapshot 2026-11-26), invested CAPM alpha
  with each exit cost booked on the invested month it sells instead:
  - drawdown_50: 14 exits, 7.00% of cost left out = 25.9 bp per invested month; alpha 2.34%/mo
    -> 2.10%/mo (-2.9%/yr).
  - default: 15 exits, 7.6 bp/mo; -0.30% -> -0.38%/mo.
  - loose: 19 exits, 7.9 bp/mo; -1.78% -> -1.85%/mo.
  - value, insider_fcf: never in cash, unchanged.
  The active regression is not affected: it keeps the cash months.
- Test (fails at 5ba5edf: `assert -1.7e-18 == -0.000305...`):
  ```python
  def test_the_invested_regression_keeps_the_cost_of_each_exit_to_cash(tmp_path):
      """Every 4th month is cash; its -0.001 is the cost of selling the previous month's picks.
      The invested sample keeps each entry cost, so it must keep each exit cost too."""
      fac = factor_table(60, start=(2014, 12))
      snap = fx.load(fx.fetch(web_for(fac), tmp_path, date(2026, 11, 26)))
      ser = series_from(fac, lambda f: (f["RF"] + f["MKT"], f["RF"] + f["MKT"]),
                        picks=lambda i: 0 if i % 4 == 0 else 2)
      exits = sum(1 for a, b in zip(ser, ser[1:]) if a["picks"] and not b["picks"])     # 11
      capm = at.attribute(ser, snap)["invested"]["CAPM"]
      assert capm["n"] == 36
      assert capm["alpha"] == pytest.approx(-0.001 * exits / 36, abs=1e-9)
  ```
  One fix: book a cash month's selling cost on the invested month whose holdings it sells, for the
  invested regression only (keeping the cash month in it would pull the loadings toward 0). Or say
  in the table title that exit-to-cash costs are left out, with the bp per month.

### Checked without a finding (round 1)
- Alignment, end to end on real data: the series' universe return for as_of M correlates 0.894
  with French Mkt-RF+RF of month M+1, and -0.075 / -0.084 with months M and M+2 (177 months, every
  variant). AQR QMJ vs FF5 RMW and BAB vs CMA peak at lag 0 too (since 2000), so AQR's month labels
  match French's.
- Units: every factor column has a decimal-scale mean and sd (e.g. ff3 Mkt-RF 0.694% / 5.30%, qmj
  0.324% / 2.34%, bab 0.650% / 3.21%); no month gaps in any source.
- RF: subtracted once in `_fit` for invested (French RF or q5 R_F), never for active; factor
  columns are excess or long-short.
- The series compounds to the reported CAGR exactly for all 5 variants; it stops at the first
  unmeasured month (tested; 0 unmeasured on real data).
- OLS: matches numpy (the builder's 20 of 20, and the unit test); singular and n <= p give None.
- Snapshots: built in mkdtemp, renamed whole; same-date path runs `load` (hash, missing file);
  failures remove the temp folder. Parsers fail loudly on layout changes, nan/inf, bad month ends.
- `metrics_sql`/`rank_sql`: `src/screen.py` and `src/db` are not in the diff. The builder's main
  report equals this branch's report with the Factor attribution section removed (5,127 bytes,
  compared again here).
- Tests: `uv run pytest -q` at 5ba5edf -> 418 passed.

## Response (Claude)

Round 1.

### C1: fixed
- The test went in first, `85c2fbf` and `ffc6146`: it fails at 5ba5edf with alpha -1.7e-18.
  - One change to your test: it now compares with numpy OLS of the cost-adjusted response,
    not with the mean cost. In the sample the 11 exit costs correlate with MKT, so part of them
    loads on beta. Your expected value -0.0003056 is the mean cost; the OLS intercept is
    -0.0002973.
  - The test also asserts that the response carries exactly the 11 x -0.001 of exit cost.
- Fix (`6962983`): `attribute` adds a cash month's return (its selling cost, as `pr` is 0) to the invested
  month before it, for the invested regression only. The active regression is unchanged. The
  module docstring and the report's table title say so.
- Real data (scratch copy, 5 variants; `a5_check.py` now books exit costs the same way), invested
  CAPM, matching your numbers:
  - drawdown_50 2.34% to 2.10%/mo (t 0.70).
  - default -0.30% to -0.38%.
  - loose -1.78% to -1.85%.
  - value and insider_fcf unchanged.
- numpy agrees on 20 of 20. Tests: 419 passed.

## Re-check (Codex)

Round 1 (reviewer, standing in for Codex), at exact head `cbc58e03d1c974c2e0f8350b7e22335299242398`.

### C1: agree (fixed)
- The test `test_the_invested_regression_keeps_the_cost_of_each_exit_to_cash` fails with
  `src/attribution.py` from 5ba5edf (`assert -1.73e-18 == -0.000297...`) and passes at cbc58e0.
- The change to my expected value is right: the OLS intercept is mean(y) - beta * mean(MKT), and
  the 11 exit costs are not orthogonal to MKT in the sample, so the intercept (-0.0002973) is not
  the mean cost (-0.0003056). The test now checks the response carries exactly 11 x -0.001 and
  matches numpy lstsq.
- The fix books a cash month's return on the invested month before it, for the invested
  regression only. A cash month after a cash month adds 0; the last invested month adds nothing.
  The active regression is unchanged.
- Real data (my scratch copy, cached series from round 1, rerun with cbc58e0's `attribute`):
  invested CAPM alpha default -0.377%/mo, drawdown_50 2.099% (t 0.70), loose -1.849%, value
  -0.430%, insider_fcf -0.277%. These equal my round-1 recomputation. Active CAPM alpha is
  unchanged (0.012%, 0.826%, -0.654%, 0.000%, 0.152%).
- `uv run pytest -q` at cbc58e0: 419 passed.

No new findings. All findings agreed: resolved at cbc58e03d1c974c2e0f8350b7e22335299242398 (the
code head; this re-check commit changes only this file).

## Outcome (user)

_Only for findings still disputed after two rounds._
