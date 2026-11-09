# Review: M11 Quarantine checks (plan)
Status: awaiting-response

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
  - budget of at most 4 requests per filing per run, only for quarantined or `multi_class_risk`
    names: at most 204 cold, 0 when complete;
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

## Outcome (user)

_Only for findings still disputed after two rounds._
