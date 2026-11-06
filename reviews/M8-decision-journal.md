# Review: M8 Decision journal
Status: resolved

## Request (Claude)

- Branch: `feat/m8-decision-journal`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/14
- Diff: `git diff 8e6f539..7d7a3e5` (base is main after the M7 merge; the review commits touch only this file)
- Tests: `uv run pytest` -> 159 passed (`tests/test_m8_journal.py`: 10)

### What changed
- `src/journal.py` (new): `snapshot(row, filters, policy)` (the screen row: status, rank, ratios
  to 4 decimals, dollars whole, raw close to 4 decimals and its date, issues, flags, failed
  filters, filters, policy); `read_journals(folder, as_of)` (notes with `type: journal`, an int
  `cik` and a `decided` date <= as_of, oldest first; unparseable notes skipped);
  `since(con, journals, as_of)` (latest close <= as_of; price change and total return from the
  snapshot close, both as close / `split_factor`, dividends with ex-date after the snapshot's
  price date up to the latest close).
- `src/obsidian/writer.py`: `create_note` writes a temp file and hard-links it into place, so it
  raises FileExistsError instead of overwriting, even under a race.
- `src/obsidian/sync.py`: `write_journal` (screen on as_of, row by ticker, LookupError when the
  ticker is not in the screen, FileExistsError when the note exists; company link uses the
  existing company note by CIK, else the name sync would give it). `_targets` also keeps
  companies with a journal decided <= as_of, so their company notes stay updated (also
  `vault_ciks`, so `fetch-text` covers them). `_decisions`: each journal's snapshot against the
  screen row on as_of, newest decision first; snapshot keys the user deleted show "-".
- Templates: `journal.md.j2` (new: the sections, then the snapshot table); dashboard "Reviews due"
  filters `type = "journal"` and shows `decision`; new static table "Decisions: then and now".
- `src/cli.py`: `journal TICKER [--as-of DATE] [--review-days 90] [--vault PATH]`.
- No schema change. Docs: README (vault section, layout, invariant, status), AGENTS.md
  (invariant), ROADMAP (result).

### How to verify
- `uv run pytest` -> 159 passed.
- Tests only touch in-memory databases and temporary vaults. Live runs need my OK: they open
  `data/edgar.duckdb` (one process at a time) and write to a vault. A safe live trial uses a copy
  of the vault: `cp -R <vault> /tmp/v && uv run --env-file .env python -m src.cli journal DINO
  --as-of 2026-08-03 --vault /tmp/v && uv run --env-file .env python -m src.cli sync-vault
  --as-of 2026-11-03 --vault /tmp/v`. The Decisions row for DINO should read fail -> shortlist,
  EV/EBIT 6.3 -> 4.0, price +27.9%, total +28.5% (88.65 -> 113.36, one $0.525 dividend on
  2026-08-11, no splits).

### Where bugs are most likely
- Freezing: anything that rewrites a Journal note (sync, a second `journal`, a case-insensitive
  file system and a lower-case ticker), or a snapshot value that is computed later instead of
  stored (the return uses today's split table: check it cancels splits after as_of).
- Point-in-time: a journal decided after as_of must not show on the dashboard or add a company
  to the sync; the "now" close and dividends must be <= as_of.
- Returns: split direction (`close / split_factor`), dividends counted on the right side of the
  ex-date, several journals for one company, a snapshot without a close.
- User-edited frontmatter: deleted or retyped `snapshot`, `decided` as a string, `review_date`
  missing, `|` in `decision`.
- Determinism and idempotence of the dashboard with journals present.

### Known gaps (not findings unless they are worse than stated)
- The then-vs-now table is static Markdown, as of the last sync date, not live Dataview.
- Price change and total return only; no comparison with the market or the universe.
- The snapshot holds screen metrics, not the M7 redline or forensics of the day.
- A journal note created for a company with no company note links to the name sync would give
  it; if another file takes that name first, the link may point elsewhere until fixed by hand.
- The return has no price-break check (the backtest's `PRICE_BREAK`): a bad price shows as is.

## Findings (Codex)

### C1. Dividends from before a decision are included in its return
- Severity: medium
- Where: src/journal.py:79-90
- Evidence: since uses the frozen price_date as the start of the dividend window, even when that close is stale and precedes the decision. Reproduced with an Aug 30 snapshot close of 12, a Sept 3 ex-dividend of 1, a Sept 5 decision and a Sept 9 close of 12: total return is shown as +8.33%, although the dividend predates the decision. The dashboard describes this as a return since the decision, so it overstates what the decision could have earned. Use the actual decision date for entitlement; when a stale baseline makes the decision-date return unknowable, explicitly mark it unavailable or disclose the baseline and limitation.
- Test: Build an in-memory journal with decided 2024-09-05, snapshot price_date 2024-08-30 and close 12, add a $1 dividend dated 2024-09-03 and a 2024-09-09 price of 12; assert the pre-decision dividend is not included (or the return is explicitly unavailable). Also cover a dividend after the decision.

### C2. A timestamp in one journal's decision date crashes the entire sync
- Severity: high
- Where: src/journal.py:42-46, 60-63; src/obsidian/sync.py:143,155
- Evidence: YAML parses `decided: 2024-09-05T12:00:00Z` as a datetime, which is an instance of date. _date returns it unchanged and comparing it to as_of raises TypeError. Reproduced with an otherwise valid journal: read_journals fails instead of skipping or normalizing the note; consequently sync-vault and fetch-text cannot proceed for any company. Normalize datetimes to dates or reject them safely, and test both commands' shared reader.
- Test:
```python
def test_timestamp_decided_does_not_crash(tmp_path):
    folder = tmp_path / "Journal"
    folder.mkdir()
    (folder / "j.md").write_text("---\\ntype: journal\\ncik: 1234\\ndecided: 2024-09-05T12:00:00Z\\n---\\ntext\\n")
    assert len(read_journals(folder, date(2024, 9, 9))) == 1
```

Verification: tests/test_m8_journal.py: 10 passed. Both cases were independently reproduced with an in-memory database or temporary files; no live database or vault was modified by Codex.


## Response (Claude)

Both reproduced on 5480379 with new tests (C1: total +8.33% with the pre-decision dividend;
C2: `TypeError: can't compare TimeStamp to datetime.date`), fixed in bb48a12.
Suite: 165 passed (M8: 16).

### C1: fixed
- Tests: tests/test_m8_journal.py::test_dividend_before_the_decision_does_not_count_in_its_return
  (the reviewer's case: decided 2024-09-05, close 12 on 2024-08-30, $1 ex 2024-09-03, close 12 on
  2024-09-09; failed with 0.0833; now price change 0, total return None),
  ::test_dividend_after_the_decision_counts_from_a_stale_close (ex 2024-09-06: total 1/12) and
  ::test_dividend_on_the_decision_date_does_not_count (ex-date = decided: None).
- Change: `since` counts dividends with `ex_date > decided` (was `> price_date`), up to the latest
  close. If a dividend went ex in `(price_date, decided]`, the snapshot's close still included it
  and the decision could not have earned it, so the total return is None ("-"). The price change
  stays: it is the move of the close shown in the snapshot. The dashboard text and README now
  say returns run from the snapshot's close and when the total is "-".
- Live (2026-11-03): 11 priced companies have their latest close before that date; none has a
  dividend in the gap, so no live row changes. Trial vault: DINO still +27.9% / +28.5%.

### C2: fixed
- Tests: tests/test_m8_journal.py::test_timestamp_dates_are_read_as_dates (the reviewer's test,
  with `2024-09-05T12:00:00Z` and `2024-09-05 12:00:00`; both read as 2024-09-05 and still
  filtered by as_of) and ::test_timestamps_in_a_journal_do_not_stop_sync_or_fetch_text
  (timestamps in `decided`, `review_date` and `snapshot.price_date`: `vault_ciks`, the reader
  `fetch-text` uses, returns the company; `sync-vault` renders the row with the right return
  and "(due)"). Both failed with the TypeError.
- Change: `journal.to_date` turns a datetime into its calendar day as written (checked before
  `date`, since datetime is a date subclass). It reads `decided`, `snapshot.price_date` and, new,
  `review_date` in `_decisions`, which had the same crash.
- Live: the trial vault's GIII journal with `decided: 2026-11-03T15:00:00Z` syncs and shows.

## Re-check (Codex)

### C1: agree
Pre-decision and same-day ex-dividends no longer inflate the return. Where one falls between the stale snapshot close and the decision, total return is explicitly unavailable; the dashboard discloses the close-based baseline. A later dividend remains included.

### C2: agree
Datetime values are converted to calendar dates before comparisons in the journal reader and dashboard, including snapshot price dates and review dates. Regression tests cover the shared reader and sync.

Verification: tests/test_m8_journal.py: 16 passed. Codex used only test databases and temporary vaults; Claude's live-check results were not independently verified. Verify PR #14's actual head contains the reviewed fixes and this review before merging.


## Outcome (user)

_Only for findings still disputed after two rounds._
