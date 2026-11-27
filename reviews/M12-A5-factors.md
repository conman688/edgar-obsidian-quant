# Review: M12-A5 factor attribution of the backtest
Status: awaiting-review

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

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
