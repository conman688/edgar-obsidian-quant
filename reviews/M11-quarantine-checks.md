# Review: M11 Quarantine checks (plan)
Status: awaiting-recheck

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

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
