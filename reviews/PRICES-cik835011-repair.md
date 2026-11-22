# Review: PRICES-cik835011-repair Restore CIK 835011 (MGPI -> CASK) price history: script and manifest
Status: awaiting-review

## Request (Claude)

- Branch: `fix/cik835011-repair`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/32
- Diff: `git diff 40790da..<exact head from the hand-off message>` (base: main `40790da`, PR #30 merged). This file is the only change after `738b89d`.
- Commits: `075729e` tests (failing first: `src.repair` did not exist), `738b89d` script, then this
  review file.
- Tests: `uv run pytest` -> 229 passed (217 on main, plus 12 in `tests/test_repair.py`).
- **Nothing is written to live data by this PR or this review.** A write needs this review
  resolved, the reviewed manifest hash, and Conor's separate yes. Merging the PR does not
  approve the write.

### What changed
- `src/repair.py` (new; `uv run python -m src.repair`). Dry run by default.
  - Source: the preserved 11-07 Parquet files (`prices_b07`, `dividends_b07`, `splits_b07` in
    `edgar-obsidian-quant-checks/cik835011-preserve-2026-11-14/`). Each file must match its
    `SHA256SUMS` line and have exactly the live table's columns and types. Every row must be the
    target CIK, one row per key, with no NULL or NaN value except `volume_raw` (`read_source`).
  - Live read: plain `duckdb.connect(read_only=True)`, `memory_limit` 2GB, 2 threads, `fetchall`
    only, under `data/.run.lock` (`ops.run_lock`, non-blocking: exit 3 if held). It never calls
    `src.db.connect`, so it never migrates. It refuses schema versions outside 8..9.
  - Manifest: `{"hash", "plan", "context"}`. `hash` = sha256 of `plan` as canonical JSON
    (`sort_keys`, separators `,` `:`, `allow_nan=False`). Python floats serialise with exact
    round-trip repr.
- `tests/test_repair.py`: 12 fixture tests (temporary DuckDB files and Parquet). No live DB.

### Source/target key identity and values
- Keys: prices `(cik, price_date)`; dividends and splits `(cik, ex_date)`. They are the same as
  the live tables' keys. `prices` has no PRIMARY KEY since migration 3, so the plan also blocks
  on any live duplicate key for the target.
- Values compared: prices `[ticker, close_raw, volume_raw, source]`; dividends
  `[amount_raw, source]`; splits `[ratio, source]`. Exact equality, no tolerance.
- Rows are inserted verbatim from the source, **with their own ticker (`MGPI`)**. They are not
  relabelled to CASK, so PR #30's guards still see two securities:
  - screen `ticker_change` (`src/db/queries.py:106`) quarantines 835011 while MGPI rows are in
    the 365-day window (until about 2027-10-23);
  - backtest `returns_sql` breaks the series between 2026-10-23 (MGPI) and 2026-10-26 (CASK);
  - journal `since()` gives no return across the change.
  This is the conservative, reviewed behaviour for an unproved continuity. No continuity claim
  is made here.

### Conflict/correction policy (`POLICY` constant; it is inside the hashed plan)
- A source key absent live is inserted. This is the only kind of insert.
- A source key present live keeps the live row unchanged (live wins: current rows and source
  corrections). It is listed as `already_present` if identical, else as `keep_live` with both
  values.
- Nothing is deleted. Nothing is updated except `price_status.first_date`, lowered with
  `least()` to the earliest inserted price date (as `prices._store` keeps it). Live is already
  2010-01-04, so this plan sets nothing.
- Blocks (the write refuses): a live split for the target inside the restored date range that
  the source lacks; a duplicate live key; no `price_status` row.
- `price_actions_held` (migration 9) rows for the target are reported in `context`, not
  resolved. Live is at schema 8 today, so this is `null`.

### Idempotence
- `--write --expect-hash H` re-derives the plan under the lock and writes only if the plan's
  hash is H. A plan with nothing to insert and no status change is a no-op with exit 0
  (`test_write_twice_is_a_no_op`). After a write, a fresh plan has 0 inserts and lists the
  restored keys as `already_present`.
- A live change to any hashed item after review refuses the write
  (`test_live_change_after_review_refuses_the_write`).

### Other CIKs untouched
- Writes are only `INSERT ... (cik = target)` and one `UPDATE price_status ... WHERE cik = target`.
- In the same transaction, before commit: for every other CIK, `[count, bit_xor(hash(row))]` per
  table (prices, dividends, splits, price_status, price_actions_held if present) must be
  unchanged. The target's rows must equal its old rows plus the plan's inserts, exactly. A fresh
  plan must have nothing left to write. Otherwise it rolls back.
- Test: `test_write_applies_exactly_the_plan_and_leaves_other_ciks_untouched` (schema 8 and 9)
  compares every row of every table before and after.

### Live manifest (dry run, Codex-reserved sole reader slot, 2026-11-21 17:05:54-17:05:55 MST)
- Ran at head `738b89d7500df430459e7d01fdbe4796da4ca771`:
  `uv run python -m src.repair --db /Users/conor123/edgar-obsidian-quant/data/edgar.duckdb
  --cik 835011 --prices .../cik835011-preserve-2026-11-14/prices_b07.parquet --dividends
  .../dividends_b07.parquet --splits .../splits_b07.parquet --out
  /Users/conor123/edgar-obsidian-quant-checks/cik835011-repair-2026-11-21/manifest.json`
- **Manifest hash (plan sha256): `989ef926d9b93a72c72f57cc1ee7f740dfcfbbc6b633a504fda8f054edd27f33`**
  (recomputed independently from the file: match). File sha256 `87299e11…0653b4`. All evidence
  files are in `cik835011-repair-2026-11-21/SHA256SUMS`.
- DB before and after: size 4,736,167,936, mtime 1795270404.311906346, inode 768221093
  (identical). No `.wal`, 0 `lsof` holders before and after, run lock free after. Exit 0, 0.87 s,
  239 MB max RSS.
- Plan:
  - prices: insert **4,228** (2010-01-04 .. 2026-10-23, all `MGPI`, source `yahoo`),
    keep_live **4**, already_present 0;
  - dividends: insert **48** (2010-09-13 .. 2026-08-14, sum 5.40), keep_live 0;
  - splits: 0; `price_status.set_first_date`: none; blocked: none.
  This matches the 11-14 expectation of 4,228 old-only price keys and 48 dividends.
- keep_live (live CASK row kept on each):
  - 2026-11-02, 11-03, 11-04: the same close, MGPI volume 278,715/188,015/229,045 vs CASK
    278,600/188,000/229,000;
  - 2026-11-05: MGPI 17.540000915527344 / 170,970 vs CASK 17.530000686645508 / 171,000.
- Live target now (context, not hashed): 17 CASK prices 2026-10-26 .. 2026-11-20, 1 dividend
  (2026-11-13), 0 splits. price_status is `CASK ok 2010-01-04 .. 2026-11-20`. Schema version 8.
- Other CIKs (context, for a post-write comparison with the same duckdb 2.0.0): prices 11,334,833
  rows, dividends 63,596, splits 2,889, price_status 4,208, with xor hashes in the manifest.

### How to verify
- `uv run pytest tests/test_repair.py -q` -> 12 passed; `uv run pytest -q` -> 229 passed.
- Hash: `python3 -c 'import json,hashlib;m=json.load(open("manifest.json"));print(hashlib.sha256(json.dumps(m["plan"],sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest())'`
  -> `989ef926…27f33`.
- Optional, with your own reserved slot: rerun the dry-run command above. The same plan hash is
  expected until a run changes CIK 835011's rows on those keys.

### Where bugs are most likely
- The hash covers only the plan, not the whole live target, so new CASK rows (the 11-28 weekly)
  do not change it. The weekly's refresh window starts at about 2026-11-13, after every hashed
  key, so the hash is expected to hold through it. A change on a hashed key refuses the write.
- Float identity: values come from Parquet as DOUBLE and go back as DOUBLE. The write check
  compares the inserted rows exactly.
- `bit_xor(hash(row))` is only a change detector for other CIKs. It is not cryptographic, and
  it depends on the duckdb version. The structural guarantee is that the only statements are
  target-CIK inserts and one target-CIK update.
- Schema 8 vs 9: migration 9 applies at the Mon 11-23 21:00 daily. The plan does not depend on
  it; `held_actions` will then read `[]` unless a weekly holds actions for 835011.

### Known gaps (not findings unless they are worse than stated)
- No backup step is built in. Before any write, the plan is: a fresh `backup` (or the weekly's),
  then the write in a quiet window under the lock, then a read-only post-check against the
  `context` numbers.
- The write cannot check Conor's approval itself; that is procedural.
- Continuity MGPI -> CASK is not asserted, so the screen will quarantine 835011 for about a
  year. Proving a rename is separate work.
- The hash relies on Python's float repr and JSON canonicalisation (Python 3.11.16 here).

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
