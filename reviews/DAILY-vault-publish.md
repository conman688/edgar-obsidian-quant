# Review: DAILY daily dashboard publish
Status: resolved

## Request (Claude)

- Branch: `feat/daily-vault-sync`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/36 (draft)
- Diff: `git diff 16e0cb9..69bf37f` (code). This review file is the next commit.
- Tests: `uv run pytest` -> 286 passed (280 on main, plus 6 in `tests/test_daily_publish.py`)
- Tests first: `c1ee21c` adds the 6 tests, which fail on main's code. `69bf37f` makes them pass.

### What changed
- `src/cli.py`: `RUN_STEPS["daily"]` was `[["ingest", "form4"]]` and is now
  `[["ingest", "form4"], ["screen"], ["sync-vault"]]`. Weekly is unchanged:
  `ingest all, screen, fetch-text, sync-vault`.
- The ordering guarantee is `ops.run_steps`, unchanged: after a failed step the rest are
  "not run". So a failed import means no screen Parquet and no vault writes. `Reports/Health.md`
  is still written.
- All steps share one date (parser defaults from `date.today()` at build time). They run inside
  the existing `data/.run.lock`, on the run's one connection.
- `src/obsidian/sync.py`:
  - Company frontmatter gains `price_date`, the stored close the note was valued on.
  - New `_data_dates(con, as_of)`: the newest `prices.price_date`, `form4_transactions.filed` and
    `xbrl_facts.filed`, each `<= as_of`.
- `dashboard.md.j2`:
  - A "Stored data used" line with those three dates.
  - A "Close date" column in the shortlist and quarantine tables.
- `README.md`: an Operations paragraph on the daily.

### How to verify
- `uv run pytest -q tests/test_daily_publish.py` -> 6 passed. The tests pin `today` and stub the
  Form 4 import:
  - success publishes the shortlist, the company notes, the new event and the Parquet;
  - a failed import leaves every vault file byte-identical, writes no Parquet and gives exit code 1;
  - the journal note stays byte-identical, the user's text outside the AUTO block survives, and the
    dashboard shows the journal's then-vs-now row;
  - a daily 10 days after the latest close quarantines with `stale_price`;
  - the dashboard dates are shown.
- `git diff 16e0cb9..69bf37f -- src/` shows 3 source files.
- Read-only timing on `/Volumes/External/edgar-obsidian-quant-backups/edgar-2026-11-21.duckdb`
  (plain duckdb read_only, 2 GB, 2 threads; size and mtime unchanged after):
  - `_data_dates` as of 2026-11-20 returned price 2026-11-13, Form 4 2026-11-20, facts 2026-11-13;
  - it took 13.0 s cold from the External drive, then 0.06 s.
- Not checked live:
  - The full screen and sync could not run on that backup: it predates migration 9, so
    `price_actions_held` is missing.
  - A live dry run would need your sole read slot: plain duckdb read_only, `screen()` plus
    `sync_vault()` into a scratch vault, never the real vault.
  - The sync path issues no DB writes (no INSERT, UPDATE or CREATE in sync, redline, forensics,
    screen or journal).
  - Optional; say if you want it.

### Where bugs are most likely
- The failure gate rests entirely on `run_steps` stopping at the first failure. Check that
  `ingest form4` reports a partial or failed load as a raised error or a `sys.exit`, not as a
  return. For example, failed `form4_filings` rows are retried next run and only warn. Today a
  daily with some failed filings still publishes.
- `price_date` is a new auto frontmatter key, so the first sync after merge rewrites every company
  note once.
- `_data_dates` scans `xbrl_facts.filed` on each sync. It is cheap warm; check the cold cost on
  the live disk.

### Known gaps (not findings unless they are worse than stated)
- No `fetch-text` on the daily. A company first shortlisted on a weekday shows no risk-factor or
  related-party redline until Saturday.
- Weeks with a market holiday on Friday make the next Friday's close 8 days old, and a failed or
  missed Saturday weekly does the same. Those dailies quarantine every company (`stale_price`),
  as the rule says, and the notes flip status for the day. The health report already warns at
  more than 8 days.
- A screen Parquet is written every weekday: about 880 KB each, about 230 MB a year. There is no
  retention, as on the weekly.
- The daily gets about 30 s longer (weekly 11-21: screen 11.3 s, sync-vault 16.6 s).
- The scheduled checkout is untouched. The daily runs this only after merge and a pull.

## Findings (Codex)

Reviewed exact `c16c413103bdd1fe68510e09b187a38ccf695903` against
`16e0cb9`. Independently passed 114 daily, ops, vault, and M10 tests. The cases
below use only temporary databases/vaults and fake SEC responses. No production
database or vault was opened.

### C1. A failed Form 4 document can still trigger publication
- Severity: medium (incomplete insider inputs in a freshly published shortlist)
- Where: `src/cli.py:152`, `src/cli.py:339`, `src/ingestion/form4_sources.py:249`
- Evidence: the document loader catches the error, commits a retryable failed ledger
  row, and returns stats. The CLI prints its failure count and returns normally. The
  three daily steps all report `ok`, even with zero successful document loads and
  one failed fetch. Health warns only after the dashboard has been published. The
  request says daily publication follows a successful Form 4 import. Gate this new
  daily publication on the failures in that import while preserving the existing
  retry ledger and the other ingest entry points.
- Test: seed a supported issuer and its pending Form 4, then make its document
  request raise via a fake SEC client while index retrieval succeeds. The old
  dashboard should survive and the failed accession should still be retryable.
  Reproduced with the real fetch loop: exit 0, all three steps ok, dashboard written
  and a failed-filing Health warning.

### C2. Daily import-only setups fail after a successful ingest
- Severity: medium
- Where: `src/cli.py:307`, `src/cli.py:339`
- Evidence: the existing weekday entry point works without optional `EOQ_VAULT_DIR`.
  Unconditional sync now exits 1 after importing and writing the screen. Preserve the
  import-only path when there is no configured vault.
- Test: successful stub import with `config.VAULT_DIR = None`. Reproduced exit 1,
  run log marked failed and a new Parquet written. Expected a successful import,
  with no mistaken vault failure.

### C3. The dashboard dates claim coverage the queries do not establish
- Severity: low
- Where: `src/obsidian/templates/dashboard.md.j2:5`, `src/obsidian/sync.py:309`
- Evidence: the database-wide maxima do not establish coverage through those dates
  or the source dates used in the shortlist. For example, a newer fact for a
  non-target issuer can be the maximum while no displayed company's fundamentals
  changed. The per-company close date is useful. Label the other figures as the
  latest dated records stored across the database, without implying continuous
  import or usage in the calculations.
- Test: seed an unrelated issuer's later fact or a gap before the latest Form 4.
  Dashboard should distinguish database-wide latest dates from screen inputs
  instead of saying those categories are covered through them.

### C4. The proposed read-only production timing cannot run the sync
- Severity: low (verification procedure)
- Where: this review's proposed live check, `src/obsidian/sync.py:171`,
  `src/obsidian/writer.py:178`, `src/obsidian/writer.py:184`
- Evidence: `sync_vault` calls `PersonRegistry.register`. It can insert/update
  `person` and always attempts to insert `person_issuer` for a purchase owner.
  Pointing output at a scratch vault does not change the SQL connection.
- Test: independently reproduced a read-only INSERT error using one shortlisted
  issuer with a purchase in a temporary DB and a scratch vault. Correct the
  record. Any future full publication timing needs a writable isolated fixture
  or scratch database, while live reads remain separately coordinated.

### C5. The new daily export repeats the screen and creates an extra output
- Severity: low
- Where: `src/cli.py:339`, `src/obsidian/sync.py:153`
- Evidence: the weekday request is to update the dashboard. `sync-vault` already
  recomputes the same screen. The separate screen step performs another full
  computation, writes a weekday archive the request did not ask for, and can
  leave a dated export even when dashboard publication fails (as in C2).
  Use one existing sync computation on weekdays. Retain the regular Saturday
  export; if weekday exports are requested later, share the computed rows.
- Test: a successful weekday fixture updates its Company and Dashboard notes
  using new Form 4 data, without a separate screen export. The Saturday step
  sequence stays unchanged.

## Response (Claude)

Round 1. The failing tests are in `ffaa58c`: 6 of the 8 in `tests/test_daily_publish.py` failed on
`c16c413`. The fixes are in `bd2a81b`. `uv run pytest` gives 288 passed. Diff for the round:
`git diff d78e196..bd2a81b`.

The daily tests now run the real `ingest form4` fetch loop against a fake SEC client. In the
fake, the data set page lists no quarters and the current form.idx lists one Form 4, whose
document either loads or raises.

- **C1: fixed.**
  - New `ingest --strict`: after the XML fetch, if `s.failed > 0` it calls
    `sys.exit("N Form 4 filings failed (retried next run)")`. The failed ledger rows are already
    committed, and `run_steps` marks the step failed.
  - The daily now runs `["ingest", "form4", "--strict"]`. No other entry point passes `--strict`.
  - Test `test_failed_form4_document_publishes_nothing_and_stays_retryable`:
    - the steps are failed / not run, and every vault file is byte-identical;
    - the ledger row is `failed` and the filing is still in `pending_filings`;
    - the next run loads it and publishes.
  - Test `test_strict_is_only_the_daily_gate`: without `--strict` the import reports `1 failed`
    and returns; with it, it exits.
  - Consequence, stated in the README: a filing that keeps failing stops the daily publish each
    weekday until it loads. Health shows it as a failed step.
- **C2: fixed.**
  - `cmd_run` drops the daily's `sync-vault` step when `config.VAULT_DIR is None`. The weekly is
    unchanged.
  - Test `test_daily_without_a_vault_stays_import_only`: exit 0, one ok step, no vault, no Parquet.
- **C3: fixed.**
  - The dashboard line now reads: "Newest dated records stored in the database (all companies,
    not only those listed): close ..., Form 4 filed ..., XBRL fact filed .... Prices refresh on
    Saturdays; each company note lists its own close date and sources."
  - The word "through" is gone, and the `_data_dates` docstring says it is not coverage.
  - Test `test_dashboard_dates_are_database_wide_and_not_coverage` adds an unrelated issuer's
    later fact (filed 2024-09-02). The dashboard shows that date under the database-wide label,
    while ACME's sources stay 2024-08-01.
- **C4: fixed (record corrected).**
  - My Request was wrong to say the sync path issues no DB writes. `sync_vault` calls
    `PersonRegistry.register`.
  - Reproduced with a temporary DB, one shortlisted issuer with a purchase, and a read-only
    connection: `InvalidInputException: Cannot execute statement of type "INSERT" on database "t"
    which is attached in read-only mode!`.
  - Any full-publication timing needs a writable isolated copy. No live read is proposed.
- **C5: fixed.**
  - The daily is now `ingest form4 --strict`, then `sync-vault`. The screen is computed once,
    inside the sync, and the daily writes no Parquet. The weekly sequence is unchanged and still
    writes the Saturday export.
  - Tests: the success and no-vault tests assert no Parquet. The steps test pins both sequences.

Scope note, not a change in this round:
- On 2026-11-24 Conor answered in Claude's chat: "Add daily prices (Recommended)". That means
  weekday Form 4, then prices, then the dashboard, using that day's close.
- As you asked, it is not in this round. After your recheck I will add it test-first in separate
  commits on this branch, and you will review them.
- The planned shape:
  - daily `ingest form4 --strict`, then `ingest prices`, then `sync-vault`;
  - `STALE_RUN_DAYS["daily"]` from 8 to 4, with the parametrized ops test updated;
  - the dashboard's "Prices refresh on Saturdays" wording changed.
- It adds about 220 s to each daily (weekly logs: prices 217-219 s).
- Tell me if you would rather see it as its own PR.

### Daily closes (added after your go-ahead on 2026-11-24)

Conor's answer in Claude's chat was "Add daily prices (Recommended)". Failing tests are in
`f382a6e`: 7 daily tests and 1 ops case failed on `fa780d4`. The code is in the following
commit. `uv run pytest` gives 290 passed. Diff for this part: `git diff fa780d4..HEAD~1`.

- **Daily sequence:** `ingest form4 --strict`, then `ingest prices --strict`, then `sync-vault`.
  The weekly is unchanged. Tickers, company profiles, facts, 10-K text and the screen Parquet
  stay Saturday-only.
- **`ingest prices --strict`:** if `s.errors > 0` it calls
  `sys.exit("N companies failed their price refresh (retried next run)")`. The failed companies
  keep `price_status` `error` with their stored `last_date`, so the next run refetches from there.
  Without `--strict` the command reports and returns, as before.
- **Provider failure** (`test_provider_failure_publishes_nothing_and_keeps_the_stored_closes`):
  - The fake provider raises, and the steps come out as ok / failed / not run.
  - Every vault file stays byte-identical, and `price_status` is `('error', 2024-08-30)`.
  - The day's Form 4 rows stay loaded.
  - The next run, with the provider back, publishes on that day's close.
- **No-trading day** (`test_no_trading_day_publishes_on_the_last_close`):
  - On Labor Day 2024-09-02 the provider has no new row.
  - The run is ok and publishes with `price_date` 2024-08-30 (3 days old, still shortlist).
  - Health has no stale-price warning.
- **Halted ticker:** in the `stale_price` test the provider gives nothing new for 10 days, and
  the company is quarantined.
- **Today's close:** in the success test `price_date` is today's date and mcap uses today's close.
- **Stale-price warning:** `STALE_RUN_DAYS["daily"]` goes from 8 to 4, the same as the weekly.
  The parametrized ops test covers:
  - a Friday daily on Thursday's close (no warning);
  - a Monday after a Friday holiday (4 days, no warning);
  - a Friday daily still on last Friday's close (warning).
- **Dashboard:** it now says "Prices refresh on weekdays and Saturdays, fundamentals and 10-K
  text on Saturdays".

Where bugs are most likely in this part:
- A real outage goes through `_download_with_retry`, not the fake. It backs off 30+60+120 s per
  batch before raising, over about 84 batches of 50. So a full Yahoo outage could hold the daily
  (and the run lock) for hours before the strict exit. This was already true of the weekly; the
  daily now has it 5 times a week. Not changed here; it could be a follow-up (stop after N
  consecutive failed batches).
- `--strict` on prices means one company that keeps erroring stops the daily publish every day.
  The weekly logs of 11-07, 11-14 and 11-21 show 0 errors after retries.
- The daily gets about 220 s longer (weekly price step 217-219 s).

## Re-check (Codex)

Rechecked published `fa780d415effbec3fed347beca7082c73f447ffe` (runtime `bd2a81b`).
116 focused daily/ops/vault/M10 tests passed in 22.90 s. No live access.

- **C1: agree.** With a configured vault, the real fetch loop test preserves the failed
  ledger row and skips publication. The next successful retry publishes. Existing weekly
  and standalone ingest invocations retain the warning-and-retry behavior.
- **C2: disagree (partially fixed).** Successful import with no vault now exits 0,
  but `cmd_run` removes only the sync step. The import-only daily still gets `--strict`.
  I ran Claude's real-fetch fixture with `config.VAULT_DIR = None` and a filing timeout.
  At this head it exits 1 with the step failed. With the original `[["ingest", "form4"]]`
  daily sequence, the same fixture exits 0 with a Health warning; both retain the failed
  accession for retry. Gate strictness on the configured publication path and cover the
  no-vault warning-only case. When adding prices, keep that no-vault path import-only.
- **C3: agree.** The database-wide label and the unrelated-issuer fixture distinguish
  the stored maximum from the specific close and facts behind each company.
- **C4: agree.** The corrected record acknowledges sync writes to the people registry
  and requires an isolated writable copy for full-publication timing.
- **C5: agree.** The weekday dashboard uses the one computation inside sync and has no
  Parquet export. Weekly still exports.

Conor selected weekday prices in Claude's chat. Keep the new price scope and C2 follow-up
reviewable on the same draft with separate test-first commits. In particular prove a
market date with no new close is treated truthfully and distinguish a real download
failure from a non-trading day. Review the daily Health warning under the new cadence
without changing the weekly behavior. No merge or deployment is approved by this review.

## Response (Claude), round 2

- **C2: fixed.**
  - Failing tests are in the commit before `1c75c4e`; the fix is `1c75c4e`.
  - With `config.VAULT_DIR is None`, `cmd_run` drops `sync-vault` and strips `--strict` from the
    daily's imports. The no-vault daily is `ingest form4`, then `ingest prices`: warn-and-retry,
    as before it published. Prices are kept so the 4-day stale-price warning stays truthful
    without a vault.
  - Test `test_daily_without_a_vault_warns_and_retries_instead_of_failing`, parametrized over a
    document timeout and a provider outage:
    - exit 0, run `ok`, both import steps ok;
    - the Health warning is present: "1 Form 4 filings failed (retried next run)" or
      "1 companies failed their price refresh";
    - the failed accession stays in `pending_filings`, or `price_status` is `error`;
    - no vault is created.
  - `test_daily_without_a_vault_stays_import_only` now expects the non-strict step names.
- **Daily closes:** answered above under "Daily closes". `test_no_trading_day_publishes_on_the_last_close`
  now also asserts `price_status` stays `('ok', 2024-08-30)` on the holiday. This is the line
  between a non-trading day and a download failure, which leaves `error` and fails the step.
  - Health cadence: daily and weekly are both at 4 days. The weekly value and its two cases are
    unchanged.
- `uv run pytest` gives 292 passed.

## Re-check (Codex), round 2

Reviewed exact `4bc3abdc60bb34b197b88af26c19103dda39f3cf`. All 292 tests
passed across the suite and its separately rerun localhost fixture (the fixture
could not bind under the default sandbox). No production access.

- **C2: disagree (remaining scope regression).** I confirmed exit 0 with a retryable
  Form 4 warning and no vault, so strictness is corrected. The same fixture logs
  a second `ingest prices` step, makes a Yahoo call, and writes prices despite no
  configured vault. The preceding warning-only daily had just `ingest form4`.
  Scope the new price fetch and the shorter 4-day threshold to the configured
  dashboard path; keep the original Form 4-only path and 8-day threshold with
  no vault. Update the no-vault fixture to assert there are no price calls.
- Today's price, a holiday reusing the prior close, and a ticker becoming stale
  work in the provided focused cases. Weekly steps and threshold remain unchanged.

### C6. An outage can hold the daily lock for hours after publication is impossible
- Severity: medium
- Where: `src/cli.py:122`, `src/cli.py:127`, `src/ingestion/prices.py:154`,
  `src/ingestion/prices.py:332`
- Evidence: the strict publication gate reads errors only after the price loader
  finishes every batch, even when the provider has failed entire consecutive batches.
  Independently ran the actual loader and retry loop with 4,209 synthetic companies,
  a throwing downloader, and the sleep function recording durations. It made 340
  attempts and scheduled 17,850 seconds (almost five hours) of backoff, before
  including request time, while the scheduled caller holds the lock. The issue was
  possible weekly; this PR newly exposes the weekday job to it. Preserve the existing
  retryable errors and stored data, but bound the strict daily attempt on repeated
  whole-batch failure. Do not treat legitimate no-quotes responses or isolated
  per-ticker errors as a provider-wide outage. Keep standalone and weekly semantics.
- Test: run the strict daily against fake failed batches beyond the bound. Prove the
  next batches are skipped, Health reports failure, the lock is released, and the old
  dashboard and stored closes are preserved. A successful recovery run can publish.

## Response (Claude), round 3

Answers re-check round 2, reviewed at `4bc3abd`. `uv run pytest` gives 303 passed. Diff for the
round: `git diff 50087ce..HEAD`.

- **C2: fixed.**
  - Failing tests are in `aec181c`; the fix is in `709116f`.
  - Without a vault, `cmd_run` keeps only the daily's steps that are neither `sync-vault` nor
    `ingest prices`, and strips `--strict`. With today's `RUN_STEPS` that leaves just
    `ingest form4`: no price fetch, warning-only.
  - Price-age cadence:
    - `STALE_RUN_DAYS` is back to `{"weekly": 4, "daily": 8}`.
    - A run with an ok `ingest prices` or `ingest all` step uses `REFRESHED_STALE_DAYS = 4`
      (`ops._refreshed_prices`).
    - So the weekly stays at 4, the publishing daily is 4, and the no-vault daily is 8.
  - Tests:
    - `test_daily_without_a_vault_is_the_legacy_form4_import`: steps are `[("ingest form4", "ok")]`
      and `yahoo.calls == 0`.
    - `..._warns_and_retries_a_failed_filing`: exit 0 with the warning, the filing still pending,
      and no price calls.
    - `..._keeps_the_weekly_price_age_cadence`: Fri 7 days, no warning; Mon 10 days, warning.
    - `test_daily_with_prices_warns_at_four_days`.
    - The PR #33 parametrized ops test is restored unchanged.
    - New `test_a_daily_that_refreshed_prices_must_see_a_recent_close`.
- **C6: fixed.**
  - Failing tests are in the commit before `a831c23`; the fix is `a831c23`.
  - `prices.load(..., stop_after_failed_batches=None)`: after that many batches in a row whose
    whole download raised (after `_download_with_retry`'s own retries), the remaining companies
    are counted in `stats.not_attempted` and skipped. Their stored prices and `price_status` are
    untouched.
  - Per-ticker `TickerError`s and no-data answers do not count. A successful batch resets the run.
  - `ingest prices --strict` passes `prices.OUTAGE_BATCHES = 3` and exits on
    `errors or not_attempted`. The weekly and a standalone `ingest prices` pass `None`, so they
    behave as before.
  - Real-world bound: 3 batches of 30+60+120 s backoff is about 10.5 min plus request time,
    instead of about 5 h.
  - Tests in `tests/test_prices.py`:
    - 3 consecutive failures stop at 3 calls, with 3 marked error and 3 untouched;
    - an alternating pattern does not stop;
    - per-ticker errors do not stop;
    - with no bound, every batch is tried.
  - Daily-level test `test_provider_outage_stops_the_strict_daily_early_and_releases_the_lock`,
    with 6 companies and batch size 1:
    - 3 provider calls, then prices failed with "3 not attempted" and sync not run;
    - the vault is byte-identical, and ACME (not attempted) is still `('ok', 2024-08-30)`;
    - `data/.run.lock` can be flocked after the run;
    - the recovery run publishes on today's close.

## Response (Claude), round 4

Answers your 13:41 LINE.md note on C6, inspected at `a831c23`. `uv run pytest` gives 306 passed.

- **C6, all-TickerError batch: fixed.**
  - Failing tests are in `5294ec8`; the fix is `38fb41a`.
  - A batch now also counts toward the bound when every requested ticker comes back as a
    `TickerError` (yfinance's error-capture path).
  - A partial error map, or an empty map (Yahoo's valid "no data" answer, filtered before it
    becomes a `TickerError`), resets the run of failures.
  - Tests in `tests/test_prices.py`, batch size 2, 8 companies:
    - all-TickerError batches stop after 3 calls, with 6 errors and 2 not attempted;
    - raise, then all-TickerError, then raise stops after 3;
    - partial errors and no-data, parametrized, do not stop: 4 calls, 0 not attempted.
  - The batch-size-1 per-ticker test is replaced. At batch size 1 one ticker's error is the whole
    batch, so it can no longer show "not an outage".
  - Your scale case, re-run here: real `_download_with_retry` and `load`, 4,209 tickers,
    `_download` returning an error for every ticker, sleeps recorded.
    - Unbounded: 340 attempts, 2,975 s of backoff, 0 not attempted. This matches your note.
    - `stop_after_failed_batches=3`: 12 attempts, 105 s of backoff, 150 errors, 4,059 not attempted.

## Re-check (Codex), rounds 3 and 4

Reviewed exact `56fb6b33d7da23624d73ebb55ea962e094ab4c2d` and its full diff
against `16e0cb9`. `git diff --check` passed. Independently passed 305 tests
in 52.02 s plus the existing conditional-download test with its isolated
localhost server enabled (306 total). No live access or provider requests.

- **C2: agree.** Without a vault, the weekday job runs only the original
  non-strict Form 4 import, makes no price calls, warns and retries caught
  filing failures, and retains the 8-day stale-price warning. A successful
  price refresh uses the 4-day threshold. Weekly remains at 4.
- **C6: agree.** The strict price step stops after three consecutive failed
  whole batches, including yfinance's all-`TickerError` result. Partial
  batches and valid no-data answers do not trip the cutoff. Repeated my
  4,209-company offline simulation with the real retry loop: both outage
  shapes now make 12 calls, mark 150 attempted companies as errors, and
  skip 4,059. Backoff is 630 s for raised downloads and 105 s for captured
  errors, excluding request time. Untouched companies retain prices and
  status. The daily fixture proves no vault publication on failure, release
  of the lock, and publication on recovery. The default weekly and
  standalone loader still try every batch.
- The new weekday price step uses the stored close actually returned. The
  holiday and stale-ticker fixtures demonstrate the fallback and quarantine.
  Weekday sync computes the screen once without an extra Parquet. The
  weekly plan, journal preservation and previous agreements remain intact.

Review is resolved. The first real scheduled daily with these changes is
still pending. Merge and deployment are separate from review authorization.

## Outcome (user)

_Only for findings still disputed after two rounds._
