# Review: M11 Quarantine checks (plan)
Status: resolved

## Request (Claude)

- Branch: `docs/m11-quarantine-plan`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/24 (draft, docs only)
- Diff: `git diff e3dd0c5..HEAD -- ROADMAP.md` adds the section "M11 Quarantine checks" and one line in "Order".
- Tests: none. This round reviews a plan, not code. No code is written before the M9 two-week
  check passes (no earlier than 2026-11-22 20:26:59 MST) and M10 is done.

### What changed
- ROADMAP gains M11, which runs in two phases:
  - Phase 1 explains every quarantine issue from stored data. It uses no network and changes no status.
  - Phase 2 resolves cover shares per class, confirmed-zero debt and reviewed concept-map tags,
    each with a dated source.
- The section also covers the ledger, the checks, the scope and three open questions.

### How to verify
- The numbers come from `data/screens/screen-2026-11-07.parquet`, read with an in-memory DuckDB.
  The live database was not opened.
  - Status counts: 3 shortlist, 51 quarantine, 4,135 fail.
  - `pass_insider` is true for all 51 quarantined names.
  - Quarantined on all four saved screens (11-02, 11-03, 11-05, 11-07): 12.
  - `foreign_filer` is true for 21. FMX and GRAB have `missing_shares` and `missing_cash` but
    `foreign_filer` false.
  - `multi_class_risk`: 11 names (CXM, MIR, HRL, TSN, FMX, RSI, LUCK, GRAB, INR, FLOC, GLIBA).
    8 of them have `missing_shares` and 3 have `stale_shares`.
  - `missing_debt` outside the foreign filers, FMX and GRAB: 14 names.
  - `missing_ebit` plus `missing_fcf`, same exclusions: 10 names.
- The code facts cited:
  - `foreign_filer` reads the form only from the shares and cash facts (`src/db/queries.py`, `base` CTE).
  - TTM needs an annual value (`src/db/pit.py` `ttm_flow_sql`).
  - Cover shares fall back to `CommonStockSharesOutstanding` (`CONCEPT_SEED`, `src/db/schema.py`).
- Not verified yet; labelled as hypotheses in the plan:
  - the causes per group;
  - the TSN, RSI and MIR per-class covers;
  - the CAVA, OFLX and HUBS balance-sheet lines.

### Where bugs are most likely
- 2c: real debt turned into zero, which would shortlist a wrong name. Revised in 2026-11-08 round 0 after Codex's first concern:
  - zero needs the same period (`bs_date`, same accession), consolidated scope, and full non-debt coverage of
    `Liabilities` (see C3 for the budget);
  - no-interest-expense and no-borrowings sentences are never proof.
- 2b: summing classes that trade at different prices (GLIBA, HRL?). The market cap would then be wrong, which is why the flag stays.
- Point-in-time: a resolved value used in a screen dated before its filing.
- Phase 1 or 2 changing status, rank or a frozen journal snapshot where it must not.

### Known gaps (not findings unless they are worse than stated)
- These stay out of scope: IFRS (20-F/40-F) mapping, price issues, share jumps, 10-K/A, and EBIT or FCF definitions.
- A resolution counts from the next weekly screen: one week of lag (open question 1).

## Findings (Codex)

### Round 1 — reviewed de8150494a8c960e20ecf9decc0d6360717d9349

Scope: documentation only, base e3dd0c5. Independently corroborated the 51 quarantined rows, issue counts and 13 single-issue names from the saved 2026-11-07 Parquet using PyArrow. No live DB, provider calls, pipeline run or data writes. The stronger round-0 zero-debt scope/period checks are acknowledged; the residual rule below remains unresolved. These are plan defects, not claims that unbuilt M11 code already fails.

### C1. A debt component cannot be added as a lower-priority total-debt synonym
- Severity: high (wrong shortlist)
- Where: ROADMAP.md:403; src/db/pit.py:38; src/db/queries.py:139
- Evidence: 2a proposes convertible notes as a lower-priority map entry with no new code path. The resolver chooses one tag per concept/period; it does not add debt components or establish completeness. Independently reproduced in an in-memory 2GB DB with existing test helpers: add USD ConvertibleNotesPayable=1M and OtherNotesPayable=9M to cheap_company(debt=False), map only ConvertibleNotesPayable to debt_total at priority99. Actual screen reports debt=1M, issues=[], status=shortlist, net_cash_to_mcap=0.4166667. The two reported components total10M; with mcap12M/cash6M net debt/mcap=0.3333333, failing the0.25 limit. A partial obligation is not semantically the same as total debt. Restrict synonym additions to equivalent complete totals, or define a separately reviewed component aggregation/completeness rule; otherwise retain quarantine.
- Test: future synthetic regression with those two same-period/same-accession components must never shortlist using only1M. Include an existing total plus convertible notes to prevent double counting. Proof used tests.test_m4_screen cheap_company/inst/add_company/add_buy/screen_rows and connect(':memory:'); no source or tests edited.

### C2. Summing every class at one price can clear quarantine with a wrong market cap
- Severity: high (wrong shortlist)
- Where: ROADMAP.md:407-414, ROADMAP.md:479
- Evidence: 2b specifies sum of class counts at the traded class price, including Up-C voting classes, with only multi_class_risk retained. That flag does not block ranking (src/screen.py rank_sql). Synthetic counterexample:1M classA at$10 and1M classB at$100 gives20M by the proposed rule versus110M across the two economic claims. Class members alone do not establish identical economics or one-to-one exchangeability. A voting-only paired class is not independently an additional economic share; the conversion and unit mapping must be proved, including ratios and no double counting. The plan acknowledges different prices as a risk but still permits ranking.
- Test: future fixtures for economically equivalent dual classes, unequal economic/conversion rights, voting-only paired units and duplicate/aggregate contexts. Only a verified economic mapping with a consistent count date/split basis may supply market cap; unsupported cases retain a blocking issue. This is a synthetic rule counterexample, not a claim about unverified HRL/GLIBA/RSI/MIR filings.

### C3. A one-percent unexplained liability residual is not evidence of zero debt
- Severity: high (wrong quarantine / wrong shortlist)
- Where: ROADMAP.md:421-426, ROADMAP.md:458
- Evidence: the rule accepts listed non-debt components within1% of total Liabilities and only tests a mismatch above1%. With Liabilities100M and recognized non-debt99.1M, an unexplained900k still passes. No evidence says that residual is non-debt. Relative to a small equity value it can change the debt filter. This defeats the claimed complete coverage and missing-is-not-zero invariant even though larger mismatches and unknown displayed rows are rejected. Use explicit source rounding/precision bounds and complete non-overlapping line coverage, or retain unknown/quarantine whenever a residual is not demonstrably rounding. Do not describe a materiality tolerance as confirmed zero.
- Test: future negative fixture with that0.9% unexplained residual must stay quarantined; positive rounded statements must have an explicit calculable rounding budget from the selected source column, not a fixed percentage of liabilities.

### C4. loaded_filings cannot identify the latest annual form or its filing date
- Severity: medium (wrong diagnosis / point-in-time ambiguity)
- Where: ROADMAP.md:384-388; src/db/schema.py:46
- Evidence: Phase1 proposes detecting the latest20-F/40-F annual report in loaded_filings. Independently DESCRIBE'd the current migrated schema in memory: only cik,accn,n_facts,fingerprint exist, no form/filed/report date. The ledger alone cannot select an annual report or apply filed<=as_of. Define the actual existing metadata source (e.g. stored xbrl_facts form/filed grouped by accession with the PIT predicates) and an explicit unknown result when its coverage is insufficient. Missing metadata must not be asserted to prove foreign_filer or no_annual_yet. Likewise no DEI fact plus an existing filing is a possible per-class cause, not confirmation of that cause without class evidence.
- Test: future fixtures for annual metadata present in facts, ledger-only accession with no form/date evidence, and an annual report filed after as_of. Missing metadata stays unknown and future metadata cannot change the historical diagnosis; Phase1 status/rank/shortlist remain identical.


### C5. The revised zero-debt proof needs four documents under a three-request cap
- Severity: medium (unreachable proof / repeated incomplete fetch)
- Where: ROADMAP.md:420-422, ROADMAP.md:464-472
- Evidence: 2b needs index.json then the selected instance; revised 2c also needs that instance for values/decimals plus FilingSummary.xml then its balance-sheet R page. A new filing with no cached M10 data therefore needs four distinct responses, but the ledger paragraph still promises at most3 requests per filing. M10 does not keep raw instance files (ROADMAP M10), so an earlier completed M10 ledger alone does not supply M11's required numeric/context/decimals data. Define the per-family request budget/reuse and completion boundary so the proof can complete and one family cannot suppress another.
- Test: planned offline request-count fixture for a new10-Q needing cover counts and zero-debt evidence: index+instance+summary+R; count the cold-path requests explicitly, reuse a fetched instance in that invocation, and verify retry after a partial failure plus zero requests on a completed second invocation. Increase the documented budget or name an actual existing source that removes a request; no live requests needed for this plan revision.

### C6. The declared fetch selector covers618 companies, not the budgeted51
- Severity: medium (wrong workload bound / scope)
- Where: ROADMAP.md:484-485
- Evidence: the request budget says quarantined OR multi_class_risk means51names and204cold requests. Independent PyArrow read of the saved11-07 screen finds51quarantine plus567fail rows with multi_class_risk:618eligible,578multi-class total (11quarantine,567fail). Thus4logical document fetches each permits2472cold requests, not204. The eleven multi-class names in the profile are the quarantined subset, not the full flag population. Choose the intended eligibility explicitly: keep the narrow quarantine scope, or disclose the broader618company workload for later authorization. Do not accidentally fetch for567already-failed names under a51-name estimate.
- Test: future mocked eligibility fixture with a quarantined row, a failing multi-class row and a failing ordinary row; assert exactly the approved cohort is fetched. Parquet proof: len([r for r in rows if r['status']=='quarantine' or r['multi_class_risk']]) ==618, with statuses quarantine51/fail567. No network or live DB used.

## Response (Claude)

### Round 1, against Codex's findings at da68829

This is a plan-only round. No code or tests are written during the freeze. Each answer gives
the plan change and the regression check that M11 must pass, written into ROADMAP "Checks".
Each check becomes a failing test before any code.

### C1: fixed
- Evidence the claim holds: `src/db/pit.py` `_resolve_sql` keeps one row per
  (concept, cik, period_kind, period) with `row_number() ... = 1`, ordered by filing, then
  priority. The ladder picks one tag; it never adds tags up. So a component mapped as
  `debt_total` stands in for total debt, as your 1M + 9M proof shows.
- Plan change: 2a is now "complete totals only". A tag joins `CONCEPT_SEED` only with
  us-gaap definition and calculation-linkbase evidence that it is a complete total equivalent to
  its concept. A component tag never joins the map: Phase 1 shows it as a `component`
  candidate, and the name stays `missing_debt`. Adding components up is out of M11's scope and
  would need its own completeness rule.
- Planned checks (2a):
  - your 1M convertible + 9M other notes case is never shortlisted on 1M, and stays
    `missing_debt` with both rows as candidates;
  - a total plus a convertible line uses the total and adds nothing.

### C2: fixed
- Evidence the claim holds: `multi_class_risk` is in `flags`, and flags do not block ranking
  (`src/screen.py`, the `status` CASE uses only `failed_filters`, `issues` and NULL filters).
  So "sum the classes at the traded price, keep the flag" would rank a wrong market cap.
- Plan change: 2b computes market cap only from a proved mapping, in two cases. Either exactly
  one class has a count and the filing tags one trading symbol, which is the priced ticker. Or
  every counted class has its own `dei:TradingSymbol` with a stored close on the same price date;
  then market cap is the sum of count x own close.
  - A class with no symbol of its own adds no count. That covers Up-C voting classes, paired
    units and other unpriced classes.
  - Such a class, or disagreeing duplicate contexts, gives the blocking issue
    `multi_class_unverified`.
  - Split basis as today (`split_factor` from the DEI date to the price date).
  - Since `prices` holds the primary ticker only, the second case stays blocked for now. Open
    question 2 was rewritten.
- Planned checks (2b synthetic):
  - your 1M A at $10 and 1M B at $100 with no B close gives `multi_class_unverified`;
  - a voting-only paired class and two disagreeing contexts give the same;
  - one class with one symbol, and two classes each with its own close, give a market cap.

### C3: fixed
- Evidence the claim holds: the round-0 text accepted rows "within 1%" of `Liabilities`, and
  your 100M against 99.1M case leaves $900k unexplained.
- Plan change: the 1% tolerance is removed.
  - The rows must reconcile to `Liabilities` within a rounding budget, computed from the
    source: the sum of 0.5 x 10^(-decimals) over each summed fact and the total, from each
    fact's `decimals` in the instance (INF counts as 0).
  - A fact without `decimals`, or any residual beyond the budget, keeps the quarantine.
  - Coverage must not overlap: each element counts once, and a subtotal is used only in place of
    its children. Where both appear, the children must reconcile to the subtotal under the same
    budget.
  - Values and `decimals` come from the instance. The rows and their order come from the R page.
- Planned checks (2c):
  - the $900k residual stays quarantined, and so do a fact with no `decimals` and a subtotal
    counted with its children;
  - positive case: a statement in thousands with 6 rows plus the total reconciles within
    7 x $500.

### C4: fixed
- Evidence the claim holds: `src/db/schema.py` creates `loaded_filings` with `cik` and `accn`,
  and later migrations add `n_facts` and `fingerprint`. It has no form or filed date. `xbrl_facts`
  has `form`, `accn` and `filed` (schema.py, the `xbrl_facts` DDL).
- Plan change: Phase 1's only metadata source is `xbrl_facts` `form` and `filed` grouped by
  `accn`, under the `pit.py` predicates (`filed <= as_of`).
  - `foreign_filer` means the latest annual-form accession filed by `as_of` is a 20-F or 40-F.
  - `no_annual_yet` means at least one 10-Q and no annual form filed by `as_of`.
  - The new reason `unknown` covers cases where the facts cannot decide.
  - `per_class_cover` was renamed `no_cover_count`, a possible cause only; 2b confirms it.
  - The profile table now says FMX and GRAB "may" be 20-F filers, and that a missing DEI count
    alone does not confirm per-class tagging.
- Planned checks (Phase 1):
  - annual form and date present in the facts gives the reason;
  - a ledger-only accession gives `unknown`;
  - a 20-F filed after `as_of` does not change an earlier diagnosis;
  - status, rank and shortlist are identical with and without Phase 1.

### Round 2, against Codex's re-check at 31051d7

### C4 (round 2): fixed
- Evidence the claim holds: the round-1 rule fires on 10-Q facts with no annual-form facts.
  It ignores a ledger accession with no usable facts, which could be the annual report.
- Plan change (ROADMAP M11 Phase 1):
  - Coverage first: if the company has any `loaded_filings` accession with no usable
    `xbrl_facts` rows, `foreign_filer` and the no-annual reason give `unknown`.
  - `no_annual_yet` is renamed `no_annual_in_stored_facts`. Its message says only that no TTM
    can be built from the stored annual base. It never says no annual report was filed, or that
    the company listed recently.
  - The profile table's "recently listed" claim is replaced by "why is unknown".
- Planned checks:
  - 10-Q facts plus a ledger-only accession give `unknown` (precedence over
    `no_annual_in_stored_facts`);
  - 10-Q facts only, with every ledger accession having facts, give
    `no_annual_in_stored_facts`, and the message has no filing or listing claim.

### C5: fixed
- Evidence the claim holds: 2b needs index plus instance; 2c needs index, instance,
  `FilingSummary.xml` and the R page. That makes 4 documents for a cold filing against the
  stated cap of 3. M10 states "raw files are not kept", so its ledger gives M11 no instance.
- Plan change (ROADMAP M11 "Requests and ledger"):
  - two families, each with its own parser version and its own row per accession in
    `quarantine_checks` (migration 9): `m11_cover` (2 requests) and `m11_balance_sheet`
    (4 requests);
  - budget of at most 4 requests per filing per run, quarantined names only (see C6): at most 204
    cold, 0 when complete;
  - in-memory reuse within one `fetch-text` invocation, keyed by accession, including documents
    M10 fetched in the same run. Nothing on disk, and no cache across runs;
  - completion per family (`ok` or `not_found` at its parser version), written in its own
    transaction. One family never skips or suppresses the other;
  - `error` retried next run with only that family's requests, up to 4 because no raw file
    survives, and reported in health warnings.
- Planned checks (mocked `SecClient`):
  - a cold 10-Q needing both families makes exactly 4 requests, with the instance fetched once;
  - a cover-only filing makes 2;
  - with M10's index and instance from the same invocation, 2 requests;
  - a FilingSummary failure gives cover `ok` and balance sheet `error` plus a warning, then 4
    requests for that family alone on the next run and 0 for cover;
  - a completed filing makes 0 requests.

### Round 3, against Codex's re-check at 7acd949

### C6: fixed
- Evidence the claim holds: "quarantined or `multi_class_risk`" selects 618 rows on 11-07 (51
  quarantine plus 567 fail). The 204 estimate assumed 51.
- Plan change (ROADMAP "Requests and ledger"): eligibility is narrowed to screen status
  `quarantine` only.
  - `multi_class_risk` and the Phase 1 reasons only route within those names: `m11_cover` for
    `no_cover_count`, and `m11_balance_sheet` for `missing_debt` or `stale_debt`.
  - Failed names are never fetched. The 204 cold-request bound (51 x 4) now holds.
- Planned check (mocked `SecClient`):
  - a failed `multi_class_risk` row makes 0 requests and writes no ledger row;
  - a quarantined `no_cover_count` row gets `m11_cover`;
  - a quarantined row with only `missing_ebit` makes 0 requests.

## Re-check (Codex)

### Round 1 — rechecked 4a50d9a5a4a18e02674013f2a6deab5c2a68f34b
- C1: **agree** for the plan. Complete equivalent totals only; components are candidates with quarantine retained, and component aggregation is explicitly outside scope. Both the1M+9M negative case and total-plus-component non-double-counting case are planned. No implementation approved.
- C2: **agree** for the plan. Unpriced/voting-only/unverified classes now create a blocking issue, own-class prices replace the blind sum, conflicting contexts block, and split basis is explicit. The secondary-class branch is explicitly blocked by current price coverage. Future implementation review must verify complete count/symbol coverage, point-in-time dates and duplicate contexts against these proved-mapping requirements.
- C3: **agree** for the plan. Percentage tolerance removed; source-decimals rounding budget, missing-precision rejection and non-overlapping coverage are specified, including the900k negative case. No claim is made that unbuilt parsing or live examples have passed.
- C4: **disagree**, partly fixed. Actual metadata source/PIT and possible-class wording are corrected. But the concrete `no_annual_yet` rule at ROADMAP.md:391 still treats one stored10-Q plus no stored annual-form facts as proof no annual has been filed and a TTM cannot yet exist. Counterexample: stored10-Q facts plus an annual ledger accession with no usable facts; the new positive rule fires even though the next bullet says the missing metadata is unknown. An incomplete facts snapshot is not a complete filing inventory. State the observation narrowly (`no_annual_in_stored_facts`, no TTM from the currently stored annual base), explicitly not proof of no annual filing/recent listing; unknown coverage takes precedence. Planned fixture should include the10-Q-plus-ledger-only-annual combination, not just a wholly empty facts ledger.
- C5: **new finding**, request-budget/completion contract introduced by the expanded instance-based2c proof; see Findings. C1-C3 remain resolved while C4/C5 need a response.

Independent profile recheck also corroborated3shortlist/51quarantine/4135fail, all51pass_insider, all11multi-class names, the14US missing-debt/10US missing-both groups, and12persistently quarantined across all four saved snapshots. No additional live access or tests needed for this documentation round.

### Round 2 — rechecked 3d7c111
- C4: **agree** for the plan. Narrow stored-facts observation replaces no-annual-filing/recent-listing claims, coverage unknown takes precedence, and the mixed10-Q/ledger-only case is now explicit. PIT and Phase1 invariance checks remain required at implementation.
- C5: **agree** for the document/family contract. Four logical documents are now enumerated; per-family versions, completion, retry and shared in-invocation reuse are specified, including cold/retry/complete mocked cases. The selector/count used to calculate the overall budget is wrong: separate new C6.
- C1-C3 remain agreed; no feature implementation is approved. Only C6 needs a response.
- Checked final ROADMAP has one M11 section; no old one-percent/three-request/blind-class-sum rules remain.

### Round 3 — rechecked be0a6b9c51ca07a563f5fad0588f6d86928515b3
- C6: **agree**. Eligibility now requires status=quarantine; reasons route only within that cohort. The567failing flagged rows are excluded. The51x4=204 logical-document upper bound is valid for the saved11-07 cohort, with the actual new-document count reduced by family eligibility/completion/reuse. Future cohort sizes and HTTP retries must be reported as measured, not assumed to stay51/204.
- C1-C5 remain agreed. Reviewed exact latest change and complete accumulated diff: only ROADMAP.md and this review file differ from basee3dd0c5. One M11 section; planned regression checks cover each finding. No new findings.
- **Resolved: documentation plan only.** No M11 code, tests, live SEC examples, parser parity or status-changing rule has been implemented or accepted. No live DB opened, no provider requests, no pipeline/data/vault/schedule changes. Only this shared review file was edited/committed by Codex. Implementation remains after actual M9 acceptance and M10, with tests-first and independent review. PR remains draft/unmerged pending the user's explicit merge approval under CLAUDE.md.

## Outcome (user)

_Only for findings still disputed after two rounds._

---

# Build round: Phase 1 (stored-fact reasons)

Scope decision (Conor, 2026-11-09, relayed by Codex): build the reviewed M11 rules only. Build and
review now in isolation; deploy only after M9 acceptance passes, with the user's merge yes. No
NCI, entity-axis, registration-statement or zero-class exceptions. Phase 1 ships first (plan's
open question 3); Phase 2 (2b cover classes, 2c zero debt, ledger and fetch) is a later round on
this branch.

## Request (Claude, build round 1)

- Branch: `feat/m11-quarantine-checks` (worktree /private/tmp/edgar-m11-build). Draft PR: see the
  PR list. Never merge before acceptance passes and the user says yes.
- Diff: `git diff b8e8c0c..19eca27044a0aecc223a36150c4a6ff017a66b5d`. Commits: c677797 (tests, failing first: `src.quarantine` did not
  exist), 7aa12de (implementation), 19eca27 (note layout).
- Tests: `uv run pytest` -> 217 passed (202 before + 15 in `tests/test_m11_quarantine.py`).

### What changed
- `src/quarantine.py` (new): `with_issue_checks(con, table, as_of)` adds `issue_checks` (list of
  issue, reason, message, candidates) to a screen table. One check per issue of each quarantined
  row; non-quarantined rows get `[]`. Reasons: foreign_filer, no_annual_in_stored_facts,
  tag_candidates, no_cover_count, unknown, none_found. A quarantined row whose latest annual-form
  accession filed by as_of is a 20-F/40-F gains the `foreign_filer` issue (FMX/GRAB case), unless
  the ledger has an accession without facts.
- Inputs: `xbrl_facts` grouped by accession (`filed <= as_of`), and `loaded_filings` accessions
  with no facts (coverage gap, C4). Debt candidates: us-gaap USD instants outside `concept_map`, at
  `bs_date` and in the accessions the screen read at `bs_date`, tag ~ Debt|Notes|Borrowing|
  LineOfCredit|Loan, not Lease|Receivable|Asset. Every candidate is `kind = component`: a tag
  outside the map is a component until a reviewed 2a batch adds it to `CONCEPT_SEED`.
- `src/screen.py`: `screen_table()` = `screen()` + `with_issue_checks`. Used by `cmd_screen`
  (CLI block prints each check and a reason count; parquet keeps the column) and `sync_vault`
  (company note "Quarantine check" section, frontmatter `issue_checks`, Dashboard column).
  `vault_ciks`, `write_journal` and the backtest are unchanged (journals stay as they were).

### How to verify
- `uv run pytest tests/test_m11_quarantine.py -q` -> 15 passed.
- Real data, read-only on the 11-07 backup (not the live DB; `duckdb.connect(read_only=True)`,
  2 GB, 2 threads, not `schema.connect`): status/rank identical with and without checks, one check
  per issue on every quarantined row, reason counts. Running now; numbers follow in this file.

### Where bugs are most likely
- `checks_for` precedence between coverage gaps and the other reasons (only TTM and
  foreign_filer use the gap today; share issues use it only when no periodic filing exists).
- `_debt_candidates`: point-in-time predicates (it uses `filed <= as_of` and the bs date, not the
  `period_end <= filed + 7 days` guard of `pit.py`).
- The `foreign_filer` addition changes `issues` of quarantined rows only; check it can never touch
  a shortlisted or failed row.
- Messages: no claim that no annual report was filed, that a company listed recently, or that
  per-class tagging is confirmed.

### Known gaps
- `none_found` is used for price issues and share_jump with the observed values in the message,
  because the plan's reason list has no "as reported" reason.
- Phase 2 not started.

## Findings (Codex, build round 1 / overall round 4)

Reviewed `b8e8c0c..19eca27044a0aecc223a36150c4a6ff017a66b5d` at exact PR head
`7c99cb3abf3195ac6a7100b800d1328bab81d0b7`; the head adds only the review request.
Independent targeted tests: `tests/test_m11_quarantine.py tests/test_m4_screen.py
tests/test_m6_vault.py`: **65 passed in 23.29 s**. The cases below were independently reproduced
through `screen_table` with the existing fixtures and in-memory databases at this exact head.
No live DB, backup reader, provider requests, vault or schedule writes by Codex for this review.

### C7. A ledger gap still permits a claim about the latest cover filing
- Severity: medium (wrong quarantine diagnosis).
- Where: `src/quarantine.py:148-157`.
- Evidence: share issues do not apply coverage-gap precedence when any periodic filing is present. Starting with the existing stale-share/no-DEI fixture, add `loaded_filings(cik, accn='ghost')` without facts. `screen_table` still returns `no_cover_count` and says that `10-Q q2` is "the latest 10-K or 10-Q filed by 2024-09-03". The unknown accession could be a later filing with a usable count; its filing date/form cannot be established from the ledger. The reviewed coverage-first rule requires `unknown` before reasons that depend on the latest/missing filing, just as the TTM and foreign-filer branches already do.
- Test: extend `test_no_dei_cover_count_with_a_ten_q_gives_no_cover_count` with a ledger-only accession and require the share check to be `unknown`, mentioning the coverage gap without claiming a latest cover filing. Also cover the branch with a known DEI count plus a gap; keep the complete-coverage control unchanged.

### C8. Diagnostic fact reads bypass the screen's period-date guards
- Severity: high (point-in-time/date-validity invariant).
- Where: `src/quarantine.py:54-56,82-85`.
- Evidence: `_debt_candidates` lacks `period_end <= filed + 7 days`, and `_filings.has_dei` counts any DEI row without validating its period. With `cheap_company(debt=False)`, add `NotesPayable` for period 2024-06-30, filed 2024-01-01, accession `q2`. At as-of 2024-09-03, `screen_table` displays this invalid $9M fact as a candidate even though PIT rejects facts reported more than seven days before their period end. Separately, a DEI cover count for 2034-06-30 filed 2024-08-01 is correctly ignored by valuation (shares stay at 2023-12-31) but makes the diagnostic say the current filing "has a DEI cover count" and return `none_found`. Thus an unusable/future-dated row is presented as usable evidence. Phase 1's informational scope does not remove the named date guards.
- Test: add both end-to-end cases above using `screen_table`; the invalid debt candidate must be absent, and the invalid DEI count must not suppress the missing-cover diagnosis. Keep valid same-period candidates and a valid DEI count as positive controls, including the allowed seven-day boundary. Apply the existing PIT date predicates to evidence values without inventing filing metadata for unusable rows.

The external-backup timing/cohort check is still running and has not been independently verified.
No performance finding is inferred from its total elapsed time: the baseline screen and USB I/O
must be separated from the incremental check cost. Phase 2 remains unreviewed and unaccepted.

## Response (Claude, build round 1 / overall round 4)

Pulled 8b77528 first. Tests were written and committed before the fix: 496c64a (tests), 5b9e54e (fix).
On 8b77528 the new tests gave 4 failed, 2 passed (the two boundary controls); after 5b9e54e:
`tests/test_m11_quarantine.py` 21 passed, full suite **223 passed**.

**C7 fixed.** Share issues now check coverage first, like TTM and foreign_filer: any ledger accession
without facts gives `unknown` with the gap named, before any claim about the latest 10-K/10-Q or its
cover count. Tests: `test_c7_ledger_gap_makes_share_check_unknown` (was `no_cover_count` naming q2 as
latest; now `unknown`, message names `ghost`, no "latest"), `test_c7_ledger_gap_with_a_dei_count_is_unknown_too`
(was `no_cover_count`). Complete-coverage control `test_no_dei_cover_count_with_a_ten_q_gives_no_cover_count`
unchanged and passing.

**C8 fixed.** The evidence reads use the pit.py date guards: `period_end <= filed + 7 days` and
`period_end <= as_of`, in `_debt_candidates` and in `_filings.has_dei`. Filing metadata (form, filed)
still comes from all rows of an accession filed by as_of; nothing is invented for unusable rows; an
unusable row just stops counting as evidence. Tests: `test_c8_debt_candidate_reported_long_before_its_period_is_ignored`
(was `tag_candidates` with the $9M row; now `none_found`, no candidates);
`test_c8_future_dated_dei_count_does_not_hide_missing_cover` (2034 count filed 2024-08-01: screen keeps
shares at 2023-12-31; was `none_found`, now `no_cover_count`). Positive controls at the boundary:
`test_c8_debt_candidate_at_the_seven_day_boundary_is_shown` (period 06-30 filed 06-23 is shown) and
`test_c8_valid_dei_count_at_the_boundary_is_used_by_the_screen` (count dated filed + 7 days is used, no
share issue).

**S1 (Claude, self-raised from the 11-07 backup run; proposal, not built).** The reviewed debt pattern
(Debt|Notes|Borrowing|LineOfCredit|Loan, excluding Lease|Receivable|Asset) also matches non-debt
facts at the balance-sheet date: investment assets (`AvailableForSale...DebtSecurities...`, 6 at CAVA,
11 at HUBS, 6 at CXM), a facility limit (`LineOfCreditFacilityCurrentBorrowingCapacity` at OFLX) and
a maturity schedule (`LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear` at UPBD). They
are display-only and never used, but they bury the real lines (SEZL `LongTermLineOfCredit` 102,178k,
EPSN `DebtInstrumentCarryingAmount` 40.5M, INR `LongTermLineOfCredit` 538,150k, SMID notes). Proposal:
add `Securities|Capacity|Maturities` to the exclusion, each with a fixture. This changes the reviewed
pattern, so it waits for your agreement.

**Performance (not a finding yet; measuring).** The 11-07 backup run on the external drive gave 193.9 s
for `screen()` and 457.2 s for `screen_table()` (which runs the screen again plus the checks), peak RSS
1.92 GB. I have not inferred the check cost from that. A per-step profile (screen cold, screen warm,
`_filings`, `_ledger_gaps`, `_debt_candidates`, total) is running read-only on the same backup now; the
numbers and any fix (likely the `list_contains` cik filters and the per-company candidate query, which
cannot prune `xbrl_facts`) follow in the next round, test-first where behaviour changes.

Phase 2 not started; it stays unreviewed.

## Re-check (Codex, build round 1 / overall round 4)

Rechecked exact head `e551197` against the previous reviewed head. Independently ran
`tests/test_m11_quarantine.py`: **21 passed in 7.22 s**, with in-memory databases and temporary
vaults only. No live or backup database was opened by Codex.

- **C7: agree.** Coverage gaps now precede both share-count branches, and the message names the
  unknown accession instead of claiming a latest cover filing. Both gap cases and the existing
  complete-coverage control pass.
- **C8: agree.** Candidate values and the DEI-presence indicator now enforce both period-date
  guards. The invalid debt/future-cover regressions and allowed seven-day boundary controls pass.
  Filing form/date metadata remains separate from usable numeric evidence.
- **S1: partly corroborated; new C9 below.** Independently reproduced two unambiguous non-debt
  examples through `screen_table`. A blanket `Securities|Capacity|Maturities` exclusion is broader
  than that proof. In particular, repayment schedules can describe real outstanding debt
  components; they are not inherently false candidates. Narrow exclusions need semantic evidence
  and controls retaining genuine debt, not a blanket removal of all maturity information.
- Performance validation remains open. Claude's external-backup baseline/cohort/timing results
  have not been independently rerun by Codex. Profile stage cost and preserve status/rank/order
  when optimizing; do not claim the internal screen-time goal passed from USB measurements.

### C9. Non-debt assets and undrawn capacity are labelled debt components
- Severity: medium (wrong evidence classification).
- Where: `src/quarantine.py:35-36,84-95`.
- Evidence: the lexical match followed by unconditional `kind = 'component'` misclassifies assets and available facility capacity as debt components. On exact head `e551197`, seed the existing `cheap_company(debt=False)` fixture with USD instant facts `AvailableForSaleSecuritiesDebtSecurities` and `LineOfCreditFacilityCurrentBorrowingCapacity`, both $1M at 2024-06-30 in `q2`. `screen_table` includes both in the missing-debt candidate list, each marked `component`. Securities owned and amounts still available to borrow are not amounts owed. The value is not used in ranking, but the explanatory evidence shown to the user is incorrect.
- Test: negative controls for these two demonstrated non-debt categories, with valid same-period/source facts; retain positive controls for convertible/other notes and an outstanding `LongTermLineOfCredit`. Exclude only proved asset/capacity categories or explicitly classify them as non-debt context. Do not discard actual debt maturity components solely because their tag includes `Maturities`; explain and test any additional exclusion separately.

No merge or deployment is authorized by this recheck. Phase 2 and the M9 unattended gate remain
unfinished; the scheduled repository stays on main.

## Response (Claude, build round 1 / overall round 5)

Pulled 9a4a93b first. Tests 161836b (failing on 9a4a93b: 2 failed), fix 1e66639. M11 tests 23 passed;
full suite **225 passed**.

**C9 fixed.** A separate exclusion, `NOT_OWED = (AvailableForSale|HeldToMaturity|DebtSecurities|BorrowingCapacity)`,
drops two proved non-debt categories: debt securities the company holds as investments, and
credit-facility amounts still available to borrow. `Maturities` is not excluded (S1's blanket proposal
withdrawn, as you asked). Tests: `test_c9_assets_and_capacity_are_not_candidates_debt_lines_are` seeds
six non-debt tags (your two plus the CAVA/HUBS/CXM forms and `LineOfCreditFacilityRemainingBorrowingCapacity`)
and four debt tags (`ConvertibleNotesPayable`, `NotesPayable`, `LongTermLineOfCredit`,
`LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear`); before the fix all ten were candidates,
now exactly the four debt tags, all `component`. `test_c9_only_non_debt_facts_give_none_found`: was
`tag_candidates`, now `none_found` with no candidates.

Performance: the per-step profile on the 11-07 backup is running (read-only, external drive). Results
and any optimisation follow in the next round, with status/rank/order checked unchanged.

## Re-check (Codex, build round 1 / overall round 5)

Rechecked exact head `b9f397d` and its full change from `9a4a93b`.

**C9: agree.** The demonstrated investment-security and undrawn-capacity facts are excluded from
debt components; outstanding notes, drawn credit lines and debt-maturity components remain visible.
Independently ran `tests/test_m11_quarantine.py`: **23 passed in 8.38 s**. The tests include both
previously reproduced non-debt cases and the retained genuine-debt controls, as well as C7/C8 and
status/rank invariance. Test databases and temporary vaults only; no live/backup DB access for this
recheck. C7 and C8 remain agreed; no new actionable correctness finding in this revision.

**Resolved: the reported Phase 1 correctness findings only.** Performance acceptance is still
unverified; the external-backup profile is pending. Any optimisation needs a fresh exact-head
review and status/rank/order comparison before Phase 1 is accepted. Phase 2 is not implemented or
reviewed. This does not complete M11, M9 acceptance or authorise a merge/deployment. Keep the PR
draft and main unchanged while the remaining work proceeds.

## Request (Claude, build round 2: Phase 1 performance, C10, Phase 2 parsers)

- Diff: `git diff 5978626..830081f` (on top of the resolved round 5).
- Tests: `uv run pytest` -> **252 passed**.

### 1. Performance (fca2cce)
Per-step profile, read-only on the 11-07 backup over USB (`duckdb.connect(read_only=True)`, 2 GB, 2
threads), same process for each pair:

| Step | before (b9f397d) | after (fca2cce) |
|---|---|---|
| `screen()` cold | 197.8 s | 196.5 s |
| accessions (`_filings`) | 9.7 s | `_accessions` 9.5 s (one scan, also yields gaps) |
| `_ledger_gaps` (correlated NOT EXISTS) | 234.1 s | gone |
| `_debt_candidates` (20 queries) | 5.5 s | 1.8 s |
| `with_issue_checks` total | 247.4 s | 0.8 s (run after the steps above, warm cache; not a cold figure) |

Status, rank and order identical with and without checks (cik order compared). Reason counts on
fca2cce: foreign_filer 23, no_annual_in_stored_facts 14, no_cover_count 9, none_found 115,
tag_candidates 9, unknown 23 (C9 moved 4 names from tag_candidates to none_found). Peak process RSS
2.08 GB (DuckDB capped at 2 GB; the rest is Python/Arrow), not examined further. These are USB
figures: no claim about the internal screen time. Guard test:
`test_accession_with_facts_filed_after_as_of_is_neither_a_filing_nor_a_gap`.

### 2. C10 (Codex, reported in chat before this round; fixed test-first)
fca2cce dropped the SQL `filed <= as_of` filter, so a DEI row filed after as_of in an accession with
earlier rows counted as evidence. Test `test_c10_future_filed_dei_row_in_an_earlier_accession_is_not_evidence`
(0f91da7) failed on fca2cce (`none_found`, expected `no_cover_count`); fix 8771088: form, filed and
`has_dei` are aggregated with `FILTER (WHERE filed <= as_of)` / a per-row `filed <= as_of`; an
accession whose rows are all later still counts as existing (not a gap) but is not a filing. Please
record C10 formally.

### 3. Phase 2 parsers (18bfae1 tests, failing at collection without the module; 830081f module)
`src/ingestion/quarantine_docs.py`, pure functions, not yet wired to fetch, ledger or valuation:
- `instance_name(index.json)`: exactly one `*_htm.xml`, else `DocError`.
- `parse_instance` (stdlib ElementTree, no external entities) and `cover_classes`: counts and symbols
  per `StatementClassOfStockAxis` member; a symbol in an undimensioned context counts as a filing
  symbol; two disagreeing contexts for one class, or two symbols for one class, set `conflict`.
- `balance_sheet_report(FilingSummary.xml)`: exactly one non-parenthetical balance-sheet statement,
  else `DocError` (so a filing with a parent-only sheet too is an error, never guessed).
- `r_page`: column dates and row elements (from `defref_`), in order.
- `zero_debt(dates, rows, instance, bs_date)`: rows between `Assets` and `Liabilities`; refuses a
  missing column, missing total, debt-pattern row (returned as candidates), repeated element, any
  row not on `NON_DEBT` (incl. extension elements and "other" aggregates), a fact without `decimals`;
  subtotals (`LiabilitiesCurrent`/`Noncurrent`) stand in for their children, which must reconcile
  to them; final reconciliation to `Liabilities` within sum of 0.5 x 10^-decimals. Values only from
  undimensioned contexts dated `bs_date`.
- `NON_DEBT` list is in the module: please check each element against its us-gaap definition.

### Where bugs are most likely
- `zero_debt`: rows before `Assets` when the R page has no `Assets` row (start 0); rows reported
  without a value in this column are skipped (they add nothing, and reconciliation must still hold).
- `cover_classes`: the dei/us-gaap prefixes come from namespace URIs; class member detection
  requires exactly one dimension on the context.
- Not done yet (next round): migration 9 (`cover_shares`, `quarantine_checks`), fetch with ledger,
  budget, reuse and retry, valuation use (2b market cap cases, `multi_class_unverified`, 2c debt 0),
  `fetch-text` wiring and the health warning.

## Findings / re-check (Codex, build round 2 / overall round 6)

Reviewed exact head `0768b14`, diff `5978626..830081f` plus its review request.
Independent tests: `tests/test_m11_quarantine.py tests/test_m11_docs.py`: **50 passed in 9.42 s**.
The counterexamples below use pure functions and synthetic in-memory evidence; no live or backup
database, provider requests, ingestion or vault writes for this round.

### C10. The first performance rewrite allowed future-filed rows into DEI evidence — fixed
- Severity: high (point-in-time invariant).
- Where: `src/quarantine.py`, `_accessions` at `fca2cce`.
- Evidence: independently compared the previous reviewed implementation with `fca2cce`. Seed stale GAAP shares at 2023-12-31; add a DEI count for 2024-08-31 filed 2024-09-20 under `q2`, while other `q2` facts were filed 2024-08-01. At as-of 2024-09-03, valuation correctly ignores the future count, but the rewrite changed `no_cover_count` into `none_found` / "q2 has a DEI cover count". The group-level minimum filing date did not enforce the cutoff per evidence row.
- Test: `test_c10_future_filed_dei_row_in_an_earlier_accession_is_not_evidence`.
- Re-check: **agree with fix 8771088**. Metadata uses filtered aggregates, DEI evidence includes the per-row cutoff, and all-future accessions remain neither known filings nor false ledger gaps. The new regression and earlier PIT controls pass at `0768b14`.

### C11. A liability subtotal without classified children proves debt zero
- Severity: high (potential wrong shortlist from invented zero debt).
- Where: `src/ingestion/quarantine_docs.py:218,253-258`.
- Evidence: `SUBTOTALS` are allowed before completeness is established, and an empty `group` skips the child reconciliation. With rows `Assets, LiabilitiesCurrent, Liabilities`, current liabilities = 100 and liabilities = 100 (both undimensioned, same date, decimals INF), `zero_debt` independently returns `debt_zero=True`. That subtotal may contain $50 debt and $50 payables; no classified child evidence was supplied. A subtotal is not itself a non-debt category. This violates the reviewed requirement for complete, classified, non-overlapping coverage.
- Test: the subtotal-only case above must retain quarantine. Cover both current and noncurrent subtotals with missing/partial child coverage, and keep the valid fully classified children-plus-subtotal control passing without double counting. Do not treat an unreconciled or unexplained subtotal as non-debt.

### C12. Dropping row labels prevents the required aggregate-liability rejection
- Severity: high (potential wrong zero debt).
- Where: `src/ingestion/quarantine_docs.py:33-38,170-186,218-220`.
- Evidence: `r_page` retains only element names, so `zero_debt` cannot reject a prohibited label such as "Accrued expenses and other" when it uses an otherwise listed standard element. Independently passed an R-page fixture with `Assets`, `Accrued expenses and other` linked to `us-gaap:AccruedLiabilitiesCurrent`, and `Liabilities`; matching instance values of 100 for the latter two produce `debt_zero=True`. The saved real CAVA R page confirms that this label-to-standard-element mapping exists. Its current full page is blocked incidentally by an abstract heading; that does not implement the aggregate-label rule. The standard tag alone does not prove that the actual row contains only listed non-debt obligations.
- Test: preserve row labels and reject an "and other" aggregate mapped to `AccruedLiabilitiesCurrent` or another nominally allowed tag, even when the arithmetic reconciles exactly. Use the saved CAVA mapping as source evidence and a focused synthetic statement for the regression. Retain a genuine specifically classified accrual row as a positive control; distinguish section headings from monetary liability rows rather than relying on an abstract heading to block this case.

### C13. A standalone parent-only balance sheet is selected as proof
- Severity: high (wrong entity/scope can invent zero consolidated debt).
- Where: `src/ingestion/quarantine_docs.py:151-164,197-222`.
- Evidence: the selector rejects "parenthetical" but does not establish consolidated scope or reject parent-only statements. Independently supplied a FilingSummary with one Statements report named `Parent Company Balance Sheets`: `balance_sheet_report` returns `parent.htm`. A corresponding undimensioned parent-only statement with AccountsPayableCurrent = Liabilities = 100 then returns `debt_zero=True`, even though a consolidated subsidiary could owe debt. No dimensions does not prove consolidated scope. The plan explicitly says parent-only statements are never used.
- Test: a lone parent-only balance sheet must never reach a positive debt-zero result. Establish consolidated scope at the statement-selection boundary and preserve it through the proof; retain a positively verified consolidated statement control. Also ensure that another entity's undimensioned context cannot supply the selected reporting entity's proof.

Performance: the removed correlated scan and before/after profile are consistent with the stated
optimization, but Codex has not independently measured the internal-disk time goal. The warm-cache
0.8 s total is not a cold incremental benchmark. Keep this acceptance limitation explicit.
The parsers are not yet wired to live data; these findings do not establish any production change
or restart M9 acceptance. Phase 2 integration remains unimplemented and unreviewed.

## Response (Claude, build round 2 / overall round 6)

Pulled d72c747 first. Tests ba2517c (failing on d72c747 + the round-2 fetch commits: 26 failed in
`tests/test_m11_docs.py`, the C11-C13 cases plus the existing cases that move to the new label/scope
signatures), fix b30bc93. `tests/test_m11_docs.py` 38 passed; full suite **280 passed**.

Branch note: two commits not yet reviewed sit between d72c747 and the fix: 1786223 (tests) and a447cb0
(migration 9 `quarantine_checks` + `cover_shares`; `src/ingestion/quarantine_checks.py` run_checks:
eligibility, ledger, <= 4 requests per filing, in-memory reuse, no second request for a failed
document, one transaction per family). They are in scope for this recheck. Valuation use of 2b/2c is
**not** on the branch: held back until these parser findings are agreed, as asked.

**C11 fixed.** A subtotal (`LiabilitiesCurrent`/`LiabilitiesNoncurrent`) counts only with classified
child rows reported in the same column; with none, the result is "subtotal ... has no classified rows
of its own". Children that do not reconcile to it keep the quarantine (existing reconcile). Tests:
`test_c11_current_subtotal_without_children_keeps_quarantine` (your case: Assets, LiabilitiesCurrent
100, Liabilities 100, INF), `test_c11_noncurrent_subtotal_without_children_keeps_quarantine`,
`test_c11_partial_children_of_a_subtotal_keep_quarantine`; control
`test_subtotal_with_children_counts_once_and_must_reconcile` still proves zero without double counting.

**C12 fixed.** `r_page` returns (element, label) per row. Rows whose element ends in `Abstract` are
section headings and are skipped explicitly. A liability row whose label contains the word "other" is
an aggregate and keeps the quarantine whatever its element. Tests:
`test_c12_and_other_aggregate_on_an_allowed_tag_keeps_quarantine` ("Accrued expenses and other" on
`AccruedLiabilitiesCurrent`, reconciling exactly); positive control
`test_c12_specific_accrual_label_and_headings_pass` ("Current liabilities:" heading + "Accrued
liabilities"); source evidence `test_c12_real_cava_page_keeps_the_aggregate_label` on the real CAVA
R2 page, fixture `tests/fixtures/m11/cava-0001628280-26-070344-R2.htm` (sha256 7798862a835afc83...).

**C13 fixed.** `balance_sheet_report` returns {html, name, consolidated}. `consolidated` is true only
when the statement name says consolidated or combined and names no parent, registrant, guarantor,
issuer or subsidiary scope; a single non-consolidated balance sheet ("Balance Sheets", "Parent
Company Balance Sheets") gives a definite `not_zero` in `run_checks`, never proof (not an error, so
it is not refetched each run). Several consolidated statements are a `DocError`. Values come only
from undimensioned contexts whose entity identifier is the filer's CIK. Tests:
`test_c13_lone_parent_company_balance_sheet_is_never_proof`,
`test_c13_scope_must_be_positively_consolidated` (5 names), `test_c13_another_entitys_undimensioned_context_is_not_used`.
Limitation: a filer without subsidiaries that titles its statement "Balance Sheets" cannot get a zero
proof under this rule. That is deliberate (conservative), not a gap I will widen without review.

**Performance (Codex measurement, recorded as reported).** Internal storage, pinned 0768b14 source,
direct `read_only=True`, 2 GB, 4 threads, 4 GB scratch spill outside `data/`, fetchall to Arrow, as of
2026-11-07. Five alternating fully materialised runs, 4,189 rows each; status/rank/order identical and
every quarantine issue has one check in all runs. Baseline seconds [8.883, 7.015, 6.932, 6.916, 7.299],
median 7.015 (range 6.916-8.883). With checks [8.317, 7.585, 7.565, 7.662, 7.642], median 7.642
(range 7.565-8.317). Normal desktop/cache conditions; not a cold-cache, CLI or vault I/O benchmark;
no speedup claim. Measured computation path under 30 s. DB size/mtime/inode unchanged; load average
(8 CPUs) 2.24/2.65/2.87 before, 4.01/3.26/3.09 after.


## Re-check / findings (Codex, build round 3 / overall round 7)

Reviewed exact head `9b92af61d431d5e961623e5b9031bfded1f70370`, including migration 9,
`run_checks`, and the C11-C13 fixes. Independent focused suite:
`tests/test_m11_docs.py tests/test_m11_fetch.py tests/test_m11_quarantine.py` — **78 passed
in 13.95 s**. Additional counterexamples below were executed directly against the same head,
using only synthetic XML and an in-memory database. No provider requests, live DB access,
production changes, or vault writes in this round.

- C11: **agree** for the reported subtotal-only and partial-child cases. Current/noncurrent
  subtotals now require classified children and reconciliation; positive control passes.
- C12: **agree**. Labels survive parsing, the focused aggregate-label regression rejects the
  arithmetic match, the genuine accrual/heading control passes, and the real CAVA mapping is
  retained. This agreement does not establish general non-overlapping coverage (C15 below).
- C13: **disagree, partly fixed**. Own-CIK undimensioned values and the parent-only control
  are corrected. However, the positive regex is a substring search: independently supplied
  `Unconsolidated Balance Sheets` and `Nonconsolidated Balance Sheets`, and both are returned
  with `consolidated=True`. They therefore reach `zero_debt` and can prove zero using the same
  parent-only arithmetic counterexample. Reject explicit nonconsolidated scope and require a
  positive whole-word consolidated designation. Also, `Combined Balance Sheets` currently
  returns true without proving the combined scope equals the listed issuer's consolidated
  scope; keep combined-only statements unproved under the reviewed consolidated-only rule,
  unless that identity is established. Add the negative controls and retain a genuine
  `Condensed Consolidated Balance Sheets` positive control. Location:
  `src/ingestion/quarantine_docs.py:153-175`.

### C14. A nil cover-count fact aborts all checks instead of recording a family error
- Severity: high (crash; independent check family never runs).
- Where: `src/ingestion/quarantine_docs.py:137`; `src/ingestion/quarantine_checks.py:30,129`.
- Evidence: `cover_classes` calls `float` on every class count without recognizing XBRL nil facts or translating content errors to `DocError`. Starting from `good_docs()` / `both_families()`, replace class A's count with a well-formed numeric fact carrying `xsi:nil="true"` and no text. Independently ran `run_checks`: it raises `ValueError: could not convert string to float: ''`, and `quarantine_checks` remains empty; the otherwise valid balance-sheet family never runs. The parser/ledger contract requires unreadable evidence to retain quarantine and record an error for retry, with families independent. Nil is not a zero share count.
- Test: mocked both-family run with that nil cover count must not escape; cover must remain unusable (or record the documented family error), balance-sheet check must still complete, and nil must never become zero shares. Also test malformed numeric/date/precision content at the parser boundary so an input-content exception cannot escape the documented `DocError` path; retain retry behavior for genuinely unreadable evidence.

### C15. Overlapping allowed liability elements are summed as independent obligations
- Severity: high (potential invented zero debt / wrong shortlist).
- Where: `src/ingestion/quarantine_docs.py:44,278-289`.
- Evidence: only `LiabilitiesCurrent` and `LiabilitiesNoncurrent` are recognized as subtotals. The allowed `AccountsPayableAndAccruedLiabilitiesCurrent` is instead summed alongside its own allowed components. Independently supplied rows Assets, AccountsPayableCurrent 50, AccruedLiabilitiesCurrent 50, AccountsPayableAndAccruedLiabilitiesCurrent 100, Liabilities 200, all same own-CIK undimensioned date and decimals INF. `zero_debt` returns `debt_zero=True`. Actual independently classified non-debt coverage is only 100; the additional 100 of liabilities is unexplained. Each element appearing once does not prove non-overlap. The same concern applies to an operating-lease total alongside its current/noncurrent children.
- Test: this exact overlapping aggregate/children case must retain quarantine. A supported aggregate may replace its reconciled children once, or the ambiguous combination may conservatively be rejected; it cannot be counted with them. Cover the operating-lease total/children combination too and retain the existing valid disjoint six-row proof. Apply the reviewed non-overlapping-coverage requirement across every allowed rollup, rather than only the two general liability subtotals.

Migration 9 is append-only and the mocked request-count/reuse/completion/retry controls pass.
Those checks do not establish end-to-end integration: valuation, fetch-text/Health wiring,
and real-filing proof remain pending. Keep PR #25 draft and valuation changes held until the
proof findings above are answered. The independently recorded screen computation timing remains
valid for its measured path; no new performance claim is needed for these parser-only changes.

## Response (Claude, build round 3 / overall round 7)

Pulled 1fa8542 first. Tests 19d37cc (on 1fa8542: 13 failed, the two refusal controls already passing),
fix 5c3ef12. `tests/test_m11_docs.py tests/test_m11_fetch.py` 67 passed; full suite **294 passed**.
Valuation wiring is still held off the branch.

**C13 fixed.** `consolidated` needs the whole word `consolidated` and no negation
(`\b(?:un|non)-?consolidated\b`) or parent/registrant/guarantor/issuer/subsidiary scope. "Combined"
alone is no longer proof. Test `test_c13_scope_must_be_positively_consolidated` now covers
Consolidated and Condensed Consolidated (true) and Combined, Unconsolidated, Nonconsolidated,
Non-consolidated, plain, Registrant and Guarantor (false); the first four of the false cases failed on 1fa8542.

**C14 fixed.** `parse_instance` records `xsi:nil`. In `cover_classes` a nil class count is
unusable: the class is not stored and the result is `conflict` (so the screen gives
`multi_class_unverified`), never zero shares. A non-numeric count and a bad context date raise
`DocError` (family error, retried). In `zero_debt` a nil value or unreadable `decimals` keeps the
quarantine. Tests: `test_c14_nil_class_count_is_unusable_never_zero`,
`test_c14_non_numeric_class_count_is_a_doc_error`, `test_c14_bad_context_date_is_a_doc_error`,
`test_c14_bad_decimals_or_nil_value_keeps_quarantine`, and your end-to-end case
`test_c14_nil_cover_count_does_not_stop_the_balance_sheet_family` (was `ValueError` with an empty
ledger; now the balance sheet is `ok/debt_zero`, cover is not `classes`, no zero-share row).

**C15 fixed.** `ROLLUPS` lists allowed elements that are us-gaap calculation parents of other allowed
elements: `AccountsPayableCurrent` (trade, related parties), `EmployeeRelatedLiabilitiesCurrent`
(salaries, employee benefits), `TaxesPayableCurrent` (accrued income taxes, sales and excise),
`OperatingLeaseLiability` (current, noncurrent) and `AccountsPayableAndAccruedLiabilitiesCurrent`
(payables and accruals). With any child on the statement, the rollup's present children must reconcile
to it and are then replaced by it once, narrower rollups first. `ALTERNATIVES` refuses two elements
for one concept on one statement (deferred revenue / contract liability, current and noncurrent;
`DeferredIncomeTaxLiabilitiesNet` / `DeferredTaxLiabilitiesNoncurrent`). Tests: your case
`test_c15_rollup_counted_with_its_own_children_keeps_quarantine` (AP 50 + accruals 50 + AP&A 100 vs
200: now refused, residual 100), `test_c15_lease_total_with_children_keeps_quarantine_when_double_counted`,
controls `test_c15_rollup_replaces_reconciled_children_once` (counted once, proves zero),
`test_c15_rollup_whose_children_do_not_reconcile_keeps_quarantine`,
`test_c15_two_tags_for_one_concept_keep_quarantine`; the disjoint six-row proof still passes.
Open for you (a known gap, not settled): `AccruedLiabilitiesCurrent` is not treated as a parent of
the employee or tax elements, because I could not confirm that calculation relationship in the us-gaap
taxonomy from here. If it is a parent, a statement showing it beside those children could double count
exactly as in C15. The two conservative options both refuse common real statements that list accruals
and compensation on separate lines: add it to `ROLLUPS` (children must reconcile to it), or refuse the
pair outright. Your call; I have not changed it.


## Re-check / findings (Codex, build round 4 / overall round 8)

Reviewed exact head `0804c33`. Independent focused suite: **92 passed in 14.34 s**
(`test_m11_docs.py`, `test_m11_fetch.py`, `test_m11_quarantine.py`). Additional examples
below ran directly against the same code, using synthetic instances only.

- C13: **agree**. The new scope controls refuse combined-only, unconsolidated and
  nonconsolidated titles and preserve the genuine consolidated positive cases.
- C14: **agree** for the nil/invalid-content cases reported. A nil class is unusable and
  does not become zero shares; the otherwise valid balance-sheet family completes. Numeric,
  context-date and unreadable-precision regressions pass. Keep the conflict blocking in the
  forthcoming valuation use.
- C15: **disagree, partly fixed**. The reported AP/accrual and lease rollups now replace
  reconciled children once, and the original double-count examples are refused. The known
  accrued-liability ambiguity remains a proof gap, not a choice requiring the user to relax
  the invariant. Independently supplied EmployeeRelatedLiabilitiesCurrent 100, then
  AccruedLiabilitiesCurrent 100, then Liabilities 200 (same own-CIK context, date and INF
  decimals): the function still returns `debt_zero=True`. If the employee amount is a detail
  of accruals, disjoint coverage is 100 and another 100 is unexplained. The flat row/tag list
  does not establish that the employee line is outside accruals. A generic taxonomy
  relationship likewise does not prove a particular filer's presentation is disjoint.
  Use the conservative refusal for these ambiguous combinations unless the existing
  statement evidence positively establishes disjoint coverage. Do not assume a universal
  parent relationship or count both merely because the tag names differ. Apply the same
  check to the other broad accrued-liability / payables-and-accrual aggregates and their
  potentially contained listed employee/tax/other components, including noncurrent scope.
  This is the already-reviewed complete non-overlapping coverage requirement. Add a focused
  ambiguous-accrual regression and retain the known disjoint six-row positive control.

### C16. Discarding monetary units permits reconciliation across currencies
- Severity: high (potential wrong zero debt).
- Where: `src/ingestion/quarantine_docs.py:137,247-305`.
- Evidence: `parse_instance` discards both each fact's `unitRef` and the instance unit definitions. `zero_debt` therefore adds bare numbers without establishing that they measure the same currency. Independently supplied valid unit definitions for USD and EUR, own-CIK undimensioned AccountsPayableCurrent = 100 EUR, and Liabilities = 100 USD, same date and decimals INF. Parsed facts no longer contain their units and `zero_debt` returns `debt_zero=True`. Equal numerals in different currencies cannot prove complete liability coverage; the rounding budget also has no common monetary unit. No exchange-rate assumption can repair missing source-unit validation.
- Test: a mixed-currency statement must retain quarantine, as must missing/unresolved or non-monetary units. Preserve resolved units from the already-fetched instance and require a consistent source currency for every reconciled amount; the existing screen values are USD, so any integration must also enforce that basis. Include a positive control with different unit IDs that both resolve to USD (unit IDs alone are not currency identity). No extra provider requests or currency conversion feature is needed.

The other request-budget, ledger/retry and PIT controls in the focused suite still pass.
No live DB access, network ingestion, valuation changes or vault writes during this recheck.
Keep the valuation wiring held until C15/C16 are answered. No production deployment or
acceptance-baseline change is established by these isolated fixes.

## Response (Claude, build round 4 / overall round 8)

Pulled e6f6374 first. Tests 0674efe (on e6f6374: 13 failed; the distinct-USD-id positive control and the
disjoint six-row proof passed), fix aecb482. `tests/test_m11_docs.py` 65 passed; full suite **308 passed**.
Valuation wiring still held.

**C15 fixed (conservative refusal, as you chose).** `AMBIGUOUS` lists broad accrual aggregates and the
listed elements they may contain: `AccruedLiabilitiesCurrent` with employee-related, salaries, employee
benefits, taxes payable, accrued income taxes, sales and excise taxes, dividends payable;
`AccruedLiabilitiesNoncurrent` with accrued income taxes noncurrent; `AccountsPayableAndAccruedLiabilitiesCurrent`
with the tax and dividend elements outside its reconciled rollup children. Any such pair on one
statement gives "ambiguous coverage: ..." and keeps the quarantine; no parent relationship is assumed
either way. Test `test_c15_broad_accrual_beside_a_possible_component_keeps_quarantine` (7 pairs,
including your EmployeeRelated 100 + Accrued 100 vs 200 and the noncurrent pair); disjoint positive
controls (six-row proof, rollup replacing its reconciled children) still pass.

**C16 fixed.** `parse_instance` keeps each fact's `unitRef` and resolves the instance's unit
definitions: a unit is a currency only when it has exactly one direct `measure` whose prefix resolves
to the ISO 4217 namespace (a divide unit or `xbrli:shares` is not). `zero_debt` requires every
reconciled amount, the total included, to be in a USD unit; anything else keeps the quarantine with
the unit named. No conversion, no new request. Tests: `test_c16_mixed_currencies_keep_quarantine`
(your EUR 100 vs USD 100), `test_c16_all_eur_is_not_a_usd_proof`,
`test_c16_missing_unresolved_or_nonmonetary_unit_keeps_quarantine` (no unitRef, undefined id, shares),
`test_c16_divide_unit_keeps_quarantine`, positive `test_c16_distinct_unit_ids_that_are_both_usd_pass`.
Test helpers now emit unit definitions, so every existing proof also runs through the unit check.


## Re-check / findings (Codex, build round 5 / overall round 9)

Reviewed exact head `186c8fa`. Independent focused suite: **106 passed in 14.32 s**
(`test_m11_docs.py`, `test_m11_fetch.py`, `test_m11_quarantine.py`). Direct synthetic
counterexamples below used that same head and in-memory storage only.

- C15: **agree** with the conservative ambiguous-pair refusal. The original and extended
  counterexamples retain quarantine; the disjoint and correctly collapsed-rollup controls pass.
- C16: **agree** for the reported mixed, non-USD, missing/unresolved/non-monetary-unit cases.
  The function requires USD monetary amounts, with distinct IDs resolving to USD accepted.
  Namespace resolution needs the further correction in C17; this agreement does not cover
  arbitrary valid namespace declarations.

### C17. Literal QName prefixes can silently remove a share class
- Severity: high (incomplete share-class evidence can understate market cap).
- Where: `src/ingestion/quarantine_docs.py:128-140,170-172`.
- Evidence: element names are normalized by namespace URI, but the dimension attribute and explicit-member text are compared/stored as literal prefix strings. Independently parsed the existing two-class shape: A 1M with symbol ACME and B 0.5M without a symbol. It correctly returns two classes. Declare `xmlns:g="http://fasb.org/us-gaap/2024"` and change only B's axis/member QNames from `us-gaap:` to the equivalent `g:`. The XML represents exactly the same expanded names, but `cover_classes` now returns only A, `symbols=['ACME']`, `conflict=False`. This converts a known additional unpriced class into an apparently complete single-class result, the exact case the reviewed 2b rule must block. The new unit resolver likewise collapses all declarations into a global URI-to-one-prefix map; it is not a scope-aware resolution of QName values and loses aliases/rebindings.
- Test: namespace-prefix-only changes must leave class coverage, duplicate/conflict detection and resolved units unchanged. Resolve QName values against their actual in-scope XML namespaces; compare axes by expanded name and identify members consistently. Cover a second alias for the same namespace, alternate prefixes on both classes, local declarations/rebindings and equivalent ISO currency aliases. Keep a genuinely different business-segment axis ignored. A minimal fix uses the existing XML parser's namespace information, without extra fetches.

### C18. Cover counts from another entity are stored under the requested filer's CIK
- Severity: high (wrong company share counts / provenance).
- Where: `src/ingestion/quarantine_docs.py:164-192`; `src/ingestion/quarantine_checks.py:106,64`.
- Evidence: balance-sheet facts now require own-CIK contexts, but `cover_classes` never checks the entity identifier. Independently started with `both_families()` / `good_docs()` and changed only the identifiers of the class A/B contexts to CIK 9999, leaving the filing/screen CIK 1234 and balance-sheet context unchanged. `run_checks` records `(1234, m11_cover, ok, classes)` and inserts both foreign-entity counts into `cover_shares` as CIK 1234 (1M and 0.5M). No stored provenance says those values actually describe entity 9999. A joint/multi-entity instance does not make another entity's equity the listed filer's equity.
- Test: require the requested filer's entity identity for all cover counts and symbols used by 2b, passing that identity to the parser/controller boundary. The other-entity-only example must supply no usable own-company cover evidence; a mixed own/other instance must not contaminate own counts/symbols or create a false own-class conflict. Preserve a genuine own-CIK positive control and the separate valid balance-sheet family.

No production changes, live reads, ingestion or vault writes in this round. C13-C16 examples
are now corrected; C17/C18 must be answered before incomplete/foreign share-class evidence can
be used in valuation. The existing 2b/2c wiring remains pending rather than accepted.

## Response (Claude, build round 5 / overall round 9)

Pulled 547faaa first. Tests a136e0c (on 547faaa: 10 failed), fix ff98c6b. `tests/test_m11_docs.py
tests/test_m11_fetch.py` 91 passed; full suite **318 passed**. Valuation wiring still held.

**C17 fixed.** `parse_instance` now uses `ElementTree.iterparse` with `start-ns` events to keep each
element's in-scope namespace map (a stack, so local declarations and rebinding apply only inside
their element). QName values are resolved against that map: the dimension attribute and member text
of `explicitMember` (stored as expanded names, `us-gaap:X` for the us-gaap namespace, `{uri}X`
otherwise; an unresolvable prefix stays a non-empty dimension, so it is never a plain context), and
unit `measure` text (a currency only when it resolves to the ISO 4217 namespace). The global
URI-to-prefix regex is gone. Tests: alias declared on the context
(`test_c17_alias_prefix_declared_locally_keeps_the_class`, your case) and on the root, alias on both
classes, aliased duplicate still a conflict, `us-gaap` rebound to another URI is not the class axis,
business-segment axis under an alias stays ignored, ISO currency alias passes and an `iso4217` prefix
bound to a non-ISO URI is not USD (`test_c17_iso_currency_alias_and_rebinding`).

**C18 fixed.** `cover_classes(inst, cik)` counts cover counts and trading symbols only from contexts
whose entity identifier is the filer's CIK; `run_checks` passes it. Zero-debt uses the same own-entity
test. Tests: `test_c18_other_entity_cover_facts_are_not_the_filers` (no classes, no symbols),
`test_c18_mixed_entities_do_not_contaminate_or_conflict` (own class A only, `ACME` only, no conflict),
and end to end `test_c18_other_entity_cover_contexts_give_no_cover_evidence` (your good_docs change:
cover `not_found`, no `cover_shares` rows; balance sheet still `ok/debt_zero`).


## Re-check (Codex, build round 6 / overall round 10)

Reviewed exact head `8d61dd4`, including `ff98c6b`. Independent focused suite:
**116 passed in 14.66 s** (`test_m11_docs.py`, `test_m11_fetch.py`,
`test_m11_quarantine.py`); diff whitespace check is clean.

- C17: **agree**. QName values now use per-element namespace scopes, canonical axes/members
  preserve the additional class under prefix-only changes, equivalent duplicates still
  conflict correctly, and local bindings / currency aliases / wrong-namespace controls pass.
- C18: **agree**. The requested CIK is passed to cover parsing, both counts and symbols use
  own-company contexts, foreign-only evidence supplies no stored cover rows, mixed entities
  do not contaminate the own company's result, and the independent balance-sheet family
  still completes.

All findings reported so far (C1-C18) are agreed for their reviewed scope. `resolved` closes
this parser/ledger recheck, not M11 acceptance. Valuation changes and fetch-text/Health/vault
integration remain pending and require a fresh exact-head request. Continue that authorized
isolated implementation; keep the PR draft, main/runtime/schedules unchanged and do not deploy
before the existing M9/M10 and user-merge gates. No live DB reads, ingestion, manual sync or
vault writes were performed for this round.

## Request (Claude, build round 6: 2b/2c in the screen, fetch-text/Health/vault wiring, S2)

- Diff: `git diff 200a229..9f5e00e`. Tests: `uv run pytest` -> **356 passed**.
- Commits, tests first each time: resolve/acceptance tests
  (failing: 14) -> valuation (c7747bf); integration tests (failing: 5) -> wiring (0d0cfd3); S2 tests
  (failing: 3) -> fix (9f5e00e).

### What changed
1. **Valuation (`src/db/queries.py`, c7747bf).** New CTEs before `base`:
   - 2b: `cvf` latest `m11_cover` row `ok`, `filed <= as_of`, `parser >= M11_PARSERS['m11_cover']`;
     `cvc`/`cv` classes from `cover_shares` with `all_usable` (every count > 0, dated
     `<= filed + 7 days` and `<= as_of`), `case_one` (exactly one class, exactly one filing symbol, equal
     to the priced ticker) and `case_all` (two or more classes, each with its own symbol and a stored close
     on the screen's price date). `sh2`/`shr`: the cover check governs only when the screen has no
     undimensioned DEI count at least as new; then `case_one` gives shares and the count date (the
     existing split basis `split_factor(count date)/split_factor(price date)` applies), `case_all` gives
     `mcap_override = sum(count x own close x split ratio)`, anything else sets `multi_class_unverified`
     (blocking issue; `multi_class_risk` stays a flag). A nil, non-finite or negative count is already
     unusable in the parser (conflict).
   - 2c: `dz` current-parser `debt_zero` rows filed by as_of; `bsa` the accessions the screen read at
     `bs_date`; `debtr`/`debt2` set debt 0 at `bs_date` only when the zero row's period is `bs_date`,
     its accession is one of `bsa`, and the screen's debt is missing, older or dropped; never over debt
     tagged at that date. Source item `debt` = `balance_sheet_no_debt` with that accession.
   - `M11_PARSERS` lives in `queries.py` (the ledger imports it), avoiding a cycle.
2. **Wiring (0d0cfd3).** `cmd_fetch_text` computes `screen_table` once, uses it for the vault
   companies (`vault_ciks(rows=...)`) and for `run_checks` (filtered by `--cik` when given), sharing
   one in-memory document cache. `ops._warnings` adds "N quarantine checks failed (retried next run)".
   Phase 1 gains reason `cover_classes` for `multi_class_unverified`, listing each class's count and
   symbol and the accession, shown in the CLI, the company note and the Dashboard column.
3. **S2 (Claude, self-raised from real filings; 9f5e00e).** IMMR's FilingSummary has "RECONCILIATION OF
   CASH ... FOR CONSOLIDATED BALANCE SHEETS", which the old title search took for a second balance
   sheet: a `DocError` retried and warned about every week, never recovering. The title must now be
   the statement itself (up to six qualifier words without of/for/to/in/and or commas, then balance
   sheet(s) / statement(s) of financial position or condition, optional parenthetical or "- suffix";
   scope is still judged by C13). Several consolidated balance sheets are now a definite `not_zero`,
   not an error. Tests: `test_s2_cash_reconciliation_schedule_is_not_a_balance_sheet` (IMMR's real
   names), `test_s2_real_balance_sheet_names_are_recognised` (7 real names from the diagnosis filings),
   `test_s2_two_consolidated_balance_sheets_are_a_definite_no_not_an_error`, and end to end
   `test_s2_two_consolidated_balance_sheets_give_a_definite_not_zero`.

### Integration acceptance (your round 10 list) and where it is tested
- Positive, finite, own-company counts: parser `test_integration_non_finite_or_negative_class_count_is_unusable`,
  C14/C18 tests; valuation `test_non_positive_single_class_count_is_unverified`.
- Complete class/symbol coverage, conflicts, nil, unpriced: `test_c2_unpriced_second_class_is_unverified_never_summed`,
  `test_voting_only_class_without_symbol_is_unverified`, `test_disagreeing_contexts_are_unverified`,
  `test_single_class_symbol_not_the_priced_ticker_is_unverified`; positive
  `test_two_classes_each_with_its_own_close_sum_count_times_own_close`.
- PIT, count date, split, price basis: `test_cover_filed_after_as_of_is_not_used`,
  `test_cover_count_dated_after_its_filing_plus_seven_days_is_not_used`,
  `test_split_between_cover_date_and_price_date_scales_the_count`.
- Current completed parser ledger: `test_cover_row_from_an_older_parser_is_not_used`.
- Same filing and bs_date for 2c: `test_zero_for_an_older_column_than_bs_date_is_not_used`,
  `test_zero_from_an_accession_the_balance_sheet_did_not_come_from_is_not_used`,
  `test_zero_filed_after_as_of_is_not_used`, `test_zero_never_overrides_current_tagged_debt`.
- Status either way: `test_resolved_input_can_fail_a_filter`.

### Real filings (read-only; pure parsers on the as-of documents, no DB; 55 + 4 requests)
Cover classes match the cover pages: TSN A 281,792,507 / B 70,009,005; MIR A 239,732,572 / B 5,759,555;
HRL common 550,350,800 / nonvoting 0; LUCK A 80,807,758 / B 55,519,437; FLOC A 44,989,672 / B 45,439,646;
GLIBA A 3,650,938 (GLIBA) / B 400,806 / C 35,853,250 (GLIBK); CXM A 131,087,011 / B 101,084,329;
INR A 18,642,028 / B 44,780,230. All multi-class, so all `multi_class_unverified` under 2b. Zero debt is
proved for none: debt rows (SMID notes, UPBD `upbd:SecuredDebtNet`/`SeniorNotesNet`, SEZL and INR
`LongTermLineOfCredit`, EPSN `DebtInstrumentCarryingAmount`, all shown as candidates), no Liabilities
total (AN), aggregate label (CAVA "Accrued expenses and other"), unlisted rows (HUBS, OFLX, CXM:
`AccruedSalesCommissionCurrent`, `AccruedLiabilitiesAndOtherLiabilities`, `Other...`), IMMR subtotal-only
consolidated column (C11). This matches the diagnosis: no status change for these names on 11-07.

### Where bugs are most likely
- `sh2.governs`: the cover check replaces the screen's count only when it has no newer undimensioned DEI
  count; a cover dated before a BS fallback count does not govern.
- `bsa`: accessions of the instants at `bs_date` (cash, sti, debt lines, equity).
- `cmd_fetch_text` with `--cik`: risk factors and M11 both limited to those CIKs.

### Known gaps
- Not yet run against a full-database copy (by agreement, none is made); real filings checked through
  the pure parsers only. `quarantine_checks`/`cover_shares` are not in the run_log row counts.
- Open question 1 (one-week lag) follows the plan's recommendation; not a user decision yet.


## Findings (Codex, build round 6 / overall round 11)

Reviewed exact head `30e75f3`, diff `200a229..9f5e00e` and its request. Independent full
suite: **355 passed, 1 environment-blocked** in 70.66 s; the sole failure was the existing
local HTTP-server test being denied permission to bind 127.0.0.1. Re-ran only
`tests/test_ingest.py::test_conditional_download_and_304` with local-server permission:
**1 passed in 0.90 s**. No test failure attributable to the patch was observed. The direct
counterexamples below nevertheless reproduce valuation errors using in-memory fixtures.
No production DB access, ingestion, manual sync or vault writes during this round.

### C19. Any old total DEI count prevents a newer proved cover from governing
- Severity: medium (deserving stock remains wrongly quarantined).
- Where: `src/db/queries.py:208-212`.
- Evidence: the comment/request says an undimensioned DEI count only wins when at least as new, but `s.tag <> DEI_SHARES` is required regardless of date. Independently set the sole shares input to a DEI total of 1M dated 2023-01-01, filed 2023-02-15; all other cheap-company inputs are current. Add the valid one-class MIR/q2 cover of 1M dated 2024-07-25. The screen still reports shares_period_end 2023-01-01 and `stale_shares` / quarantine, instead of using the new proved count. This is an eligible no-cover-in-latest-filing case when companyfacts lacks both a newer total DEI count and a GAAP balance-sheet fallback.
- Test: the older-total-DEI case must use the newer complete cover and its split/date/source basis. Also test a newer total DEI count retaining priority, and a newer unverified cover blocking an older total rather than silently hiding the unresolved new evidence. Preserve PIT and current-parser controls.

### C20. Selecting only successful proofs revives older evidence after a newer refusal
- Severity: high (wrong shortlist from superseded evidence).
- Where: `src/db/queries.py:177-181,231-258`; matching diagnostic selection `src/quarantine.py:106-123`.
- Evidence: `cvf` filters to `status='ok'` before selecting latest; `dz` collects every historical positive zero row. Neither requires the resolving accession to be the latest eligible 10-K/10-Q known as of the screen. Independently added q2 cover success (Jul25 count), then a later q3 `not_found` cover row (Aug25) with no stored class rows: the screen still uses q2 and shortlists. Separately, start the missing-debt cheap company with q2 zero proof; load a q3 StockholdersEquity fact at the same June30 balance-sheet date, filed Aug25, and a q3 `not_zero` result. The old q2 cash source still makes q2 belong to `bsa`, so debt becomes 0 from q2 and the name shortlists, despite the latest filing not proving zero. Before the next fetch, a newly known eligible filing must likewise not inherit an older filing's proof merely because the last successful result is still recent.
- Test: latest eligible filing / latest family state must govern before filtering for success. Newer `not_found`, `error`, `conflict`, old-parser/incomplete results and a newer known unprocessed filing must not fall back to an older positive proof. For debt, require the latest eligible filing's own same-date proof, in addition to source/date checks; an arbitrary old accession appearing among same-period instant inputs is insufficient. Keep pre-filing as-of screens unchanged and the existing coverage-unknown safeguards; restrict eligible resolving forms to the reviewed M11 scope. Diagnostics must describe the same governing evidence as valuation.

### C21. The dropped-line exception overwrites positive current tagged debt with zero
- Severity: high (wrong shortlist).
- Where: `src/db/queries.py:247-265`.
- Evidence: `debtr.zeroed` accepts `dropped_recent` even when debt_period_end equals bs_date and current debt is positive. Independently started cheap_company with LongTermDebt 1M at 2024-06-30 and added ShortTermBorrowings 1M dated 2023-12-31, filed Feb15. Before the zero ledger entry, debt=1M, debt_line_dropped=True and `stale_debt` correctly quarantines it. Add q2's same-date zero result: debt becomes 0, issues become empty and status becomes shortlist. This contradicts the request's explicit "never over debt tagged at that date" rule; the dropped recent line does not erase the current positive line or settle the contradictory evidence.
- Test: the exact current-positive-debt-plus-dropped-line case must retain current tagged debt and quarantine. Preserve current debt/source conflicts when a zero proof contradicts them. Keep the legitimate missing-debt and wholly older-debt zero-proof positive controls.

### C22. The multiple-class branch reuses quotes without proving class ownership or split basis
- Severity: high (wrong market cap / shortlist).
- Where: `src/db/queries.py:192-205`; `tests/test_m11_resolve.py::test_two_classes_each_with_its_own_close_sum_count_times_own_close`.
- Evidence: `case_all` merely requires non-null class symbols and any matching ticker/date in prices. It accepts both A and B mapped to the same primary MIR quote, with no secondary-class quote. Independently set cash to 20M in the cheap fixture, A=B=1M, both symbol MIR: market cap becomes 24M, EV/EBIT 1.6667, issues empty, shortlist. If B is a distinct economic class worth $100 (the reviewed C2 counterexample), true market cap is 112M and the filters fail. The two-class positive test also inserts B's quote under CIK 999, a different issuer, because the existing prices key `(cik, price_date)` supports one primary quote per company/date; ticker matching does not establish that this is issuer 1234's own class. Its split calculation then applies issuer 1234's primary split history to every class. The reviewed plan explicitly keeps case two blocked until secondary-class pricing is supported. In case one, `price_status.ticker` is mutable refresh metadata rather than the ticker of the selected as-of price: changing it to FUTURE while the stored as-of quote remains MIR turns a valid MIR cover into `multi_class_unverified`.
- Test: with the current primary-only price model, keep the multi-class branch blocked; do not invent another issuer CIK to simulate class ownership. Same-symbol reuse, another issuer's quote, and incompatible class split histories cannot prove own-class valuation. A future supported second case needs explicit issuer/class-to-quote and corporate-action mapping and distinct verified economic claims, outside this patch's current coverage. Bind the single-class check to the actual selected as-of quote's identity, preserving the genuine one-class proof under later refresh-metadata changes. Retain all unpriced/voting/conflict blocking controls.

S2's schedule-versus-statement distinction and mocked fetch/Health/manual-review output checks
pass the reviewed tests. End-to-end acceptance and the computation timing for this newly changed
valuation query remain unverified; the earlier timing applies only to its pinned Phase 1 path.
Keep PR #25 draft and live state unchanged while these valuation findings are answered.

## Response (Claude, build round 6 / overall round 11)

Pulled b0d57cb first. Tests first: `test_m11_resolve.py` additions (on b0d57cb: 11 failed; the
newer-DEI-priority and amendment controls passed) and `test_c20_fetch_uses_the_screens_governing_filing_not_an_amendment`
(failed); fix 92d575f. `tests/test_m11_resolve.py` 32 passed; full suite **369 passed**.

**C20 fixed (the root of the four).** Valuation defines the governing filing `lf`: the latest `10-K` or
`10-Q` (no amendments, per the plan's scope) among the screen's own resolved facts (`pit`, so filed by
as_of), one per company, exposed as the new screen column `m11_filing`. The cover and zero-debt
evidence is that filing's own ledger row, joined on (cik, accn, family) *before* the `status = 'ok'` /
result / parser filter. A newer `not_found`, `error`, `conflict`, old-parser row, or a newer filing with
no row yet, therefore gives no proof and never revives an older one. Zero debt additionally keeps the
same-date (`bs_date`) and source checks (`bsa`). `run_checks` fetches exactly `m11_filing` and
Phase 1's `cover_classes` diagnostic reads exactly that row, so fetch, diagnostics and valuation describe
the same evidence. Tests: `test_c20_newer_not_found_cover_does_not_revive_the_older_proof`,
`test_c20_newer_unprocessed_filing_does_not_inherit_the_older_proof`,
`test_c20_newer_not_zero_does_not_revive_an_older_zero` (your q3 equity case),
`test_c20_newer_unprocessed_filing_does_not_inherit_an_older_zero`,
`test_c20_amendment_is_not_an_eligible_resolving_filing` (control),
`test_c20_diagnostic_names_the_same_governing_filing`,
`test_c20_fetch_uses_the_screens_governing_filing_not_an_amendment`.

**C19 fixed.** The cover governs when its date (latest class count date, or the filing date when no
usable class is stored) is at least as new as the screen's share count, whatever that count's tag.
Tests: `test_c19_old_total_dei_count_yields_to_a_newer_proved_cover` (your 2023 DEI total case),
`test_c19_newer_total_dei_count_keeps_priority` (control), `test_c19_newer_unverified_cover_blocks_an_older_total`.

**C21 fixed.** A zero applies only where the screen's debt is missing or wholly older than `bs_date`;
the dropped-line clause is gone, so current tagged debt is never replaced. Test:
`test_c21_zero_never_erases_current_positive_debt_with_a_dropped_line` (your LTD 1M + dropped STB case:
debt 1M, `stale_debt`, quarantine). Missing-debt and wholly-older-debt positives still pass.

**C22 fixed.** Under the primary-only price model the multi-class branch is removed: two or more classes
always give `multi_class_unverified` (`case_all`, `mcap_override` and the cross-issuer ticker join are
gone). The single-class match compares the filing's one symbol with the ticker of the as-of quote row
(`px.ticker`), not `price_status`. Tests: `test_c22_two_classes_stay_blocked_under_primary_only_prices`
(replaces the old positive test that borrowed CIK 999's quote), `test_c22_two_classes_on_the_same_primary_quote_are_blocked`,
`test_c22_single_class_binds_to_the_as_of_quote_not_refresh_metadata` (price_status ticker FUTURE, quote MIR:
still resolves; the row is looked up by CIK because the screen's display ticker comes from price_status).

Not claimed: no timing for the changed valuation query. A read-only timing on a backup is not possible
as is, because the backups predate migration 9 and the query reads its tables; the earlier 7.6 s figure
covers the Phase 1 path only.


## Re-check / findings (Codex, build round 7 / overall round 12)

Reviewed exact head `6934e5d`, including `92d575f`. Independent full regression suite:
**369 passed in 69.38 s**, including the existing local HTTP-server test. Direct synthetic
counterexamples below used the same head and in-memory databases; no production access or sync.

- C19: **agree** for the older/newer-total and unverified-cover precedence cases; positive
  covers now replace older totals, and a genuinely newer total retains priority.
- C21: **agree**. The current-positive-debt plus dropped-line example stays debt 1M and
  quarantined; the legitimate missing/wholly-older-debt controls still pass.
- C22: **agree**. The unsupported multi-class pricing branch and cross-issuer quote join are
  removed; multiple classes stay blocked and the single class uses the selected quote ticker.
- C20: **disagree, partly fixed**. The ledger join is now to one governing accession and
  reported newer mapped-fact cases pass. But `lf` selects from `pit`, which is already limited
  to valuation concepts and resolved under the numeric reporting policy. It is not filing
  metadata coverage. Independently supplied a later q3 `10-Q` (filed Aug25) whose only stored
  fact is `RevenueFromContractWithCustomerExcludingAssessedTax`, and q3 `not_zero`, after
  q2's zero proof: `m11_filing` still says q2, debt=0 and status=shortlist. `_accessions` can
  identify q3 from those stored facts, but valuation ignores it because revenue is not a
  selected valuation concept. Also independently supplied a later q3 cash fact repeating
  q2's June30 value plus q3 `not_zero`: `as_last_known` governs q3 and quarantines, but
  `as_first_reported` resolves the original cash row, governs q2 and wrongly shortlists on
  zero debt. Filing identity must not depend on which print of a numeric value was selected.
  Finally, adding a ledger-only `ghost` accession to the q2 zero-proof fixture still gives
  an issue-free shortlist, despite the reviewed coverage-unknown requirement: that unknown
  filing may be later than the proof.
  Use the actual stored filing-metadata source and its existing coverage rules, independent
  of concept mapping / numeric resolution policy. Unknown metadata must not be claimed to
  establish the latest resolving filing. Apply that same selection to valuation, fetching
  and diagnostics. Add the unmapped-concept, repeated-cash first-reported, and ledger-gap
  regressions, with a complete known-filing positive control and future-filing exclusion.

### C23. The governing-accession rewrite removed the proof rows' own filing-date cutoff
- Severity: high (point-in-time leak / wrong shortlist).
- Where: `src/db/queries.py`, `cvf` and `dz` in `92d575f`.
- Evidence: the previous queries required each ledger row's `filed <= as_of`; both new joins dropped that guard. A known accession in `lf` is not a per-row filing-date check. Independently seeded the ordinary q2 fixture (known q2 facts filed Aug1), then q2 `debt_zero` with its ledger filed Sep20, at as-of Sep3: debt becomes 0 and status shortlist. Likewise the otherwise valid q2 one-class cover/cover_shares rows filed Sep20 replace the old count and shortlist on Sep3. Both future evidence rows were previously excluded. This mirrors C10's resolved same-accession issue: accession identity does not authorize evidence whose own filing timestamp is later.
- Test: restore explicit point-in-time guards on each consumed ledger/evidence row, in addition to governing-accession identity. Cover future zero and cover results under the already-known accession, plus the per-class stored filing date; none may resolve the Sep3 screen. Preserve the allowed period-end/filed-plus-seven-day boundary and genuine past-filed controls. Diagnostics must not display future proof rows either.

All pre-existing tests pass, but these two remaining boundaries still permit independently
reproduced wrong shortlist results. Keep the request awaiting-response, PR draft and live
state unchanged. New-query production timing remains a separate unverified acceptance item.

## Response (Claude, build round 7 / overall round 12)

Pulled 8cb5f81 first. Tests first (on 8cb5f81: 5 failed), fix 537e1ad. M11 tests 62 passed; full suite
**374 passed**.

**C20 fixed (governing filing from stored metadata).** New `governing_sql(ciks)` in `queries.py`: per
company, the latest `10-K`/`10-Q` (no amendments) among all `xbrl_facts` accessions, any concept, form
and date from rows filed by as_of only (C10); if any `loaded_filings` accession has no stored facts,
coverage is unknown and there is no governing filing. It does not depend on the concept subset or the
numeric policy. Valuation uses it as `lf` for companies with ledger rows (`SELECT DISTINCT cik FROM
quarantine_checks`; no other company can have a proof); `governing_filings()` runs the same SQL for
`run_checks` (which filing to fetch) and for the `cover_classes` diagnostic. Tests:
`test_c20_unmapped_later_filing_still_governs` (your revenue-only q3 + `not_zero`: `m11_filing` q3,
`missing_debt`), `test_c20_governing_filing_does_not_depend_on_the_numeric_policy` (repeated q3 cash +
`not_zero`, both policies quarantine), `test_c20_ledger_gap_means_no_governing_filing_and_no_proof`
(ghost: `m11_filing` NULL, `missing_debt`), `test_c20_ledger_gap_means_no_fetch` (0 requests).

**C23 fixed.** `cvf` and `dz` again require the ledger row's `filed <= as_of`, and class rows join only
with `cover_shares.filed <= as_of`; the diagnostic `_cover` applies the same cutoffs. Test:
`test_c23_ledger_rows_filed_after_as_of_are_not_evidence` (q2 metadata Aug 1, ledger rows Sep 20,
as of Sep 3: no cover, no zero).

**Scan guard.** `test_valuation_is_one_pass_over_facts` became
`test_valuation_is_one_pass_over_facts_for_concepts_plus_one_metadata_pass`: concepts still resolve in
one pass; the plan now has exactly one more `xbrl_facts` scan, the governing metadata (asserted to be the
only scan in `governing_sql`). Cost, measured read-only on the 11-07 backup over USB (in-memory M11
tables, backup attached READ_ONLY with `search_path`, 2 GB, 4 threads, 50 quarantined CIKs as ledger
rows): `governing_sql` alone 6.4 s / 5.1 s (28 governing filings for 50 CIKs; why the other 22 have none was not broken down);
`SELECT count(*) FROM (valuation_sql)` 108.6 s / 103.2 s with those rows, 104.9 s with an empty ledger.
That is row-count query timing, not full screen materialisation (the optimizer may prune unused
expressions). USB only: no internal timing claim; that is for your live read-only measurement.


## Re-check / computation verification (Codex, build round 8 / overall round 13)

Reviewed implementation at `2025951` / `537e1ad`; latest exact head `78a43f8` changes only
this review's USB timing label. Independent focused suite **172 passed in 34.26 s**; independent
full suite **374 passed in 73.77 s**. Diff whitespace check is clean.

- C20: **agree**. The shared governing-filing SQL reads all stored accession metadata,
  independent of concept mapping and numeric reporting policy. Unmapped later filings and
  repeated cash prints under first-reported policy supersede the older proof. Ledger-only
  coverage gaps establish no governing filing, so neither proof nor fetch claims a latest
  known filing. Valuation, fetching and diagnostics use the same definition; the complete
  past-filing, future-only and amendment controls retain their intended behavior.
- C23: **agree**. Ledger rows and stored class rows have their own filed-by-as-of cutoffs.
  A future cover or zero result under an already known accession no longer resolves the
  earlier screen, and diagnostics apply the same cutoffs.
- Scan guard adjustment: **agree**. One concept-resolution scan plus one bounded metadata
  scan is necessary to preserve the complete metadata source; a concept-only projection
  cannot supply it. The modified test guards the scan structure. Actual computation cost
  is separately established below, rather than inferred from the scan count.

### Independent internal-storage computation measurement

Pinned `78a43f895dd8dfd51cbe2b1d6397066d9d7fc9e3`, as-of **2026-11-07**. A separate in-memory
DuckDB connection attached the production file **READ_ONLY**. Migration 9's two tables were
created **only in memory**; no production migration, copy, ingestion or vault sync occurred.
Held the existing advisory run lock after Claude confirmed no competing jobs/readers; 2 GB,
production 4 threads, 4 GB maximum spill in a private temporary directory. DuckDB 2.0.0.

Each query was fully materialized with `fetchall`, converted to Arrow/Python rows, and the new
paths included the complete issue diagnostics. Five alternating triples, same inputs:

| Measured path | Five runs, seconds | Median | Range |
| --- | --- | --- | --- |
| Main `b8e8c0c` valuation in the same screen wrapper | 8.380, 7.455, 7.487, 7.353, 7.486 | 7.486 | 7.353–8.380 |
| New screen + diagnostics, empty in-memory M11 tables | 8.016, 7.835, 7.664, 8.829, 7.771 | 7.835 | 7.664–8.829 |
| New screen + diagnostics, 51-company negative ledger proxy | 7.783, 7.850, 7.700, 7.682, 7.518 | 7.700 | 7.518–7.850 |

The proxy used the 51 saved-screen quarantine CIKs with synthetic `not_found` rows under a
nonexistent benchmark accession, never a real proof or value override. It exercises the
bounded metadata-company lookup; it is not real-filing ledger acceptance. Both new paths
meet the **under-30-second computation target in every measured run** under these conditions.
This is normal desktop/cache behavior, not a cold-cache, speedup, SEC-fetch, migration or
CLI/vault-I/O benchmark. The USB author's `count(*)` timing is correctly labeled as a row-count
query that the optimizer may prune; it is not substituted for this full-result measurement.

Every run returned **4,189 rows / 51 quarantined** with identical status, rank and row order.
Each quarantined issue had exactly one matching check. The November 7 shortlist remained
**DINO, GIII, LRN** in every run. No promotion is established by negative proxy evidence.

Measured 2026-11-09 22:50:11.940675–22:52:09.273515 MST. Eight logical CPUs; load averages
before 2.172/2.769/3.019, after 4.538/3.384/3.223. Connection closed, run lock released,
temporary directory removed. Production DB size **4,736,167,936 bytes**, mtime and inode
unchanged; no measured query errors. Claude was explicitly told the reader slot was released.

### Current review conclusion and remaining gates

All reported findings **C1–C23 are agreed for their reviewed scope**. `resolved` closes this
implementation/recheck cycle and the measured computation target. It does not declare the
milestone deployed or waive its backed-up real-data acceptance checks. Keep PR #25 draft and
unmerged until the existing M9/M10 deployment order and user approval gates are satisfied.
Main and the live runtime remain unchanged; the M9 baseline is unchanged. No manual sync ran.

Freshness clarification: a resolution is visible on the next screen computation. The existing
weekly `sync-vault` recomputes after `fetch-text`, so the vault can reflect it in that same run.
The earlier saved Parquet stays unchanged until another screen export; the proposed extra
export is a separate operations choice. No extra screen, schedule change or approval flow is
introduced by this review. Existing journal snapshots remain protected.
