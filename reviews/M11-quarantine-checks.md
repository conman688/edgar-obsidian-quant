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
  - zero now needs the same period (`bs_date`, same accession), consolidated scope, and every liability line on a
    non-debt list summing to `Liabilities` within 1%;
  - no-interest-expense and no-borrowings sentences are never proof.
  The 1% tolerance and the non-debt list are the parts to attack.
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


## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
