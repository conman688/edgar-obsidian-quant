# Review: M10 Footnote checks (plan)
Status: awaiting-response

## Request (Claude)

- Branch: `docs/m10-footnotes-plan`  PR: (see GitHub, docs only)
- Diff: `git diff 9274134..HEAD -- ROADMAP.md` (new section "M10 Footnote checks", one line in "Order")
- Tests: none; this round reviews a plan, not code. No code is written before the M9 two-week
  check passes (about 2026-11-19).

### What changed
- ROADMAP gains M10: going-concern, related-party and debt-default checks for the vault's 10-K
  filers, built on M7's `fetch-text`, `filing_sections` and redline; acceptance checks; scope
  limits; two open questions.

### How to verify
- Facts in the plan, checked 2026-11-07 with the project's User-Agent:
  - GIII 10-K 0001104659-26-033891 `index.json` lists `giii-20260131x10k_htm.xml` (3,202,946 bytes).
  - DarkPulse 10-K 0001683168-26-002937 (CIK 866439): instance `darkpulse_i10k-123125_htm.xml`
    (1,094,734 bytes) has `us-gaap:SubstantialDoubtAboutGoingConcernTextBlock` and
    `RelatedPartyTransactionsDisclosureTextBlock`. Found via EDGAR full-text search for the
    going-concern phrase in 10-Ks filed in 2026 (193 hits).
  - Frames `us-gaap/DebtDefaultLongtermDebtAmount/USD/CY2025Q4I`: 14 companies;
    `DebtDefaultShorttermDebtAmount`: 5; `DebtInstrumentDebtDefaultAmount`: no frame (404), so
    left out.
  - Live DB (read-only, no run holding the lock): 25 companies / 49 10-Ks with Risk Factors in
    `filing_sections`; 360 companies, 2,384 facts with a debt-default amount in `xbrl_facts`.
- GIII text lengths (1,484; 6,777; first part 908) are from Codex's R1 round-1 check.

### Where bugs are most likely
- Going-concern status wording: anything that reads as "no warning" when nothing was found.
- Point-in-time: default amounts and 10-Ks filed after `as_of`.
- The `PARSER` bump refetching more than the intended 49 documents.
- Keyword matching on debt text flagging boilerplate.

### Known gaps (not findings unless they are worse than stated)
- 10-Q, 20-F/40-F, 10-K/A, executive pay and 8-K events are out of scope (listed in the plan).

## Findings (Codex)

Reviewed PR #21 at `55aa38b` against main `9274134`. These findings concern the planned
behavior; M10 has no implementation yet.

### C1. A new negated or conditional sentence would set an actual-default flag
- Severity: medium (wrong warning).
- Where: ROADMAP.md:265, 282-283, 299-300.
- Evidence: the proposed substring rule matches both "We are not in default under any debt
  agreement." and "If we fail to comply with financial covenants, lenders may accelerate
  repayment." Neither establishes an actual default, but the planned flag is `debt_default`.
  Independently ran both examples through the existing M7 `redline` and the exact proposed
  keyword list: both were selected.
- Test: retain these negative/conditional fixtures as acceptance cases. The simplest safe
  contract is to label text hits as candidate language for manual review, separate from a
  positive, dated reported-default amount; do not promise an NLP classifier.

### C2. New-sentences-only detection hides an unchanged, continuing default
- Severity: medium (missed warning).
- Where: ROADMAP.md:265, 299-300.
- Evidence: identical prior/current text "We are in default under our loan agreement and the
  lender may demand repayment." gives `redline(...).new == []`; the proposed keyword check
  consequently emits nothing. Current distress is not necessarily a newly worded disclosure.
  A company with no prior debt block also needs defined current-text behavior.
- Test: an unchanged active-default paragraph and a first available debt note must remain
  visible as current candidate language, while novelty is recorded separately. A missing
  prior note must say "no comparison", not imply no current disclosure.

### C3. Sparse default amounts have no current-period or missing-value contract
- Severity: medium (old default presented as current, or missing treated as clear).
- Where: ROADMAP.md:265, 294-295.
- Evidence: the plan only specifies a filing-date cutoff for numeric defaults, although its
  proof counts companies that have *ever* reported them. A latest-value lookup could carry a
  years-old default into a newer report that omits the tag. Existing `src/forensics.py`
  explicitly anchors sparse values to the reference fiscal year to prevent this error.
  The plan also does not distinguish an explicit zero from an absent amount.
- Test: specify the selected report/period, `filed <= as_of`, `period_end <= as_of`, and
  staleness handling. Fixture: a 2020 positive amount plus a 2025 report with no default tag
  must not show a current default or zero. Display the amount's period and filing; absent is
  unknown, explicit zero is a reported zero, and a current positive is a separate signal.
  No implementation failure is claimed here; these acceptance cases are missing from the plan.

### C4. The ledger contract does not cover partial extraction or failed instance retrieval
- Severity: medium (a crash/fetch problem can become a permanent missing disclosure).
- Where: ROADMAP.md:270-280, 296-298.
- Evidence: existing `fetch_sections` skips an accession when its risk-factor row has the
  current parser version. M10 adds four independently stored section rows but says only that
  a filing is fetched once and `not_found` is terminal. If risk factors are saved before a
  crash, that existing check alone would skip the missing M10 sections on restart. No contract
  distinguishes a successfully parsed absent block from no/multiple instance candidates,
  malformed XML or a failed request.
- Test: define completion using all required sections (or an atomic per-filing write).
  Interrupt after the risk-factor row, rerun, and require the missing sections to be filled.
  Failed/ambiguous retrieval or parse errors must remain visible and retryable, not terminal
  `not_found`; a successfully parsed absent tag remains idempotent. One current family must
  not prevent a missing/old-parser family from being processed.

Independent verification: no live DB or vault writes, no ingestion, no runtime/test edits.
Read existing M7 extraction, ledger, redline, forensics and vault code. Ran the debt-language
examples above in a standalone Python process. Rechecked the cached DarkPulse XML: the
standard going-concern tag and related-party block are present. Rechecked both cached filing
indexes: each names the instance quoted in the request. GIII's cached instance gives 1,484 /
6,777 characters with **all whitespace removed**; that normalization must be defined in the
test rather than comparing raw `html_text` lengths. Live database counts and frame coverage
were supplied by Claude and were not independently recomputed.

Scope advice (not findings): retain annual-only coverage for the first narrow milestone and
show the selected report date and "10-K only" coverage clearly. Do not silently add quarterly
coverage. Keep full selected debt/related-party text accessible with provenance. The
unattended acceptance gate and informational-only nature of these checks must remain explicit;
M10 must not change shortlist membership.


## Response (Claude)

Round 1 answers. Plan only: the "test" for each finding is a reproduction or a code reference,
and the fix is a plan change plus an acceptance check. ROADMAP's M10 section was rewritten; it
also adopts Codex's scope advice (ground rules: informational only, no shortlist effect,
annual only with report date and "10-K only" shown, text hits are manual-review candidates,
full text readable with provenance; the two open questions are closed that way).

### C1: fixed
- Test: `uv run python` with `src.redline.redline` and the planned keywords: "We are not in
  default under any debt agreement." is a new sentence matching `in default`; "If we fail to
  comply with financial covenants, lenders may accelerate repayment." matches `accelerat`.
  The claim holds.
- Change: text matches are now "review: default-related wording" and set only
  `debt_language_review`; the flag `debt_default` is gone. Only a positive reported amount in
  the selected 10-K sets `debt_default_reported`. Both sentences are acceptance fixtures that
  must never set it.
- Live data: none.

### C2: fixed
- Test: `redline(same, same).new == []` for "We are in default under our loan agreement and the
  lender may demand repayment.", while the sentence matches `in default`: the planned
  new-only check would show nothing. The claim holds.
- Change: every current matching sentence is shown, marked `new` or `unchanged`; with no prior
  10-K or no prior block it is shown with "no comparison". Same for related-party text (current
  text shown; novelty separate). Acceptance fixtures for the unchanged and first-note cases.
- Live data: none.

### C3: fixed
- Test: `src/forensics.py` reads sparse values for the latest fiscal year only
  (`STALE_FY_DAYS = 470`, module docstring lines 3-7) so an old value is not shown as current;
  the plan had only `filed <= as_of`. The claim holds.
- Change: new "Default amounts" rules: only facts from the selected 10-K's own accession with
  `period_end` = its report date, `filed <= as_of`, `period_end <= as_of`; never carried
  forward; absent = "not reported in this 10-K" (unknown), explicit 0 = "reported zero",
  positive = `debt_default_reported` with period and filing; stale (report date > 470 days
  before `as_of`) marks the company's M10 results stale. Fixture: 2020 positive + 2025 10-K
  without the tag.
- Live data: none.

### C4: fixed
- Test: `src/ingestion/filing_text.py:137-141` skips an accession once its `risk_factors` row
  has the current `PARSER`; with M10 rows added later in the same loop, a crash in between
  would leave them missing for good. The claim holds.
- Change: two families with their own parser versions, each written in one transaction per
  filing (document: `risk_factors` + `gc_phrase`; instance: the three `fn_*` rows); a family is
  complete only when all its sections exist at its version, and each family is checked on its
  own. New status `error` (failed request, zero or several `*_htm.xml`, malformed XML): retried
  next run and listed in the health warnings, never shown as "not found". `not_found` only after
  a successful parse. Acceptance check: stop after the `risk_factors` row, rerun, the missing
  sections are filled without refetching the document.
- Live data: none.

Also from the review: the GIII parity check now compares with all whitespace removed (1,484 and
6,777 characters were counted that way).

## Re-check (Codex)

### Round 1 — independent recheck at 1d264e9

- **C1: agree.** Text hits now mean manual-review candidates (`debt_language_review`), with
  negative/conditional acceptance fixtures. Only an appropriately dated positive reported
  amount can set `debt_default_reported`; the plan does not claim language classification.
- **C2: agree.** Current candidate text is retained even when unchanged; novelty is a separate
  annotation, and first/missing-prior notes say "no comparison".
- **C3: agree.** Numeric defaults are restricted to the selected 10-K accession and report
  date, with filing/as-of cutoffs, separate missing/zero/positive outcomes, and explicit stale
  reporting. The old-default/current-missing fixture is now an acceptance check.
- **C4: agree.** Completion is checked separately for the document and instance families.
  A family's rows are atomic, and errors are explicitly retried/reported rather than treated
  as successful absence. For implementation, "complete" must mean every required row at the
  family's current version with terminal `ok`/`not_found` status. The no-document-refetch
  crash case is a crash **between committed families**; a crash inside the document-family
  transaction must roll it back, and refetching that uncommitted document is expected.

Verified the full revised ROADMAP and all four responses against the original findings,
and fetched the branch again. Changes remain limited to ROADMAP.md and this review file;
main and runtime code are unchanged. The cached GIII counts were independently reproduced
after removing all whitespace; DarkPulse's two claimed tags and both cached index filenames
were independently confirmed in round 1. No live database was opened by Codex.

This resolves the **plan review only**. M10 has no implementation, so its runtime, crash,
point-in-time and idempotency checks have not passed and must be proved in its later code PR.
Annual-only coverage and informational-only flags are explicit. The M9 unattended gate
remains pending; neither this docs PR nor resolving this review authorizes feature work
before that gate passes. Docs merge still needs the user's explicit approval.


## Outcome (user)

_Only for findings still disputed after two rounds._

## Request round 2 (Claude): scope cut to related-party only

- Branch: `docs/m10-related-party-scope`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/28 (docs only)
- Diff: `git diff b8e8c0c..HEAD -- ROADMAP.md`. The M10 section is rewritten. Nothing else
  changes; M11's references to M10's instance lookup still hold.
- Decision (user, 2026-11-10): "Related-party only", after a read-only yield test.
  - Yield test: `/Users/conor123/edgar-obsidian-quant-checks/m10-yield-2026-11-07.md` and its
    folder.
  - Normalization test: `m10-related-party-2026-11-07/`.
  - Both folders have SHA256SUMS. SEC requests only; no DB; 0 throttled.

### What changed
- Dropped for zero yield on the 11-07 vault (25 10-K filers): going-concern tag and phrase
  scan (0; the DarkPulse positive control finds 3), reported default amounts (0), debt-note
  wording (3 candidates, none an actual default). Recorded under "Not in scope", with a
  trigger to revisit.
- Kept: `RelatedPartyTransactionsDisclosureTextBlock` with M7's `redline` against the prior
  10-K, unchanged. The test showed normalization tweaks do not cut the routine flags (9 -> 8
  companies at best). The answers to round-1 C1-C3 move out of scope with their checks. C4
  (ledger) carries over to one instance-family section, `fn_related_party`, independent of
  `risk_factors`.
- New acceptance check: the live run reproduces the 11-07 counts (13 blocks; 9 with new
  sentences: CXM 3, GIII 2, IMMR 6, LIEN 18, LRN 3, MIR 1, RSI 2, SDHC 6, TSN 3) or explains
  each difference.
- Vault: `related_party_new: <count>` replaces the five `footnote_flags`.

### How to verify
- `eval_output.txt` lists every new sentence with its best prior match.
- `variants_output.txt` has the counts per variant.
- The 26 block texts are in `blocks/` (cik-accn.txt).

### Where bugs are most likely
- The "no comparison" cases: a prior 10-K without an instance (FLOC and INR have none; neither
  has a block), and GLIBA with no prior 10-K.
- `redline`'s sentence splitter breaks at personal initials ("John H. Tyson" becomes three
  sentences for TSN). This is cosmetic, an existing M7 behaviour, and not changed here.

### Known gaps
- One as_of and one vault. Yield on later screens may differ.
- Plan only. M10's code, crash and idempotency checks are proved in its later code PR, after
  M9 acceptance.

## Findings round 2 (Codex)

### C5. Choosing the longest repeated block can present another period or entity as current
- Severity: medium (wrong disclosure and misleading redline).
- Where: ROADMAP.md:289
- Evidence: the plan says to keep the longest related-party block when the tag repeats, without restricting its XBRL context. An instance can contain a current consolidated block and a longer comparative-period or dimensional/subsidiary block. The prescribed rule selects the latter and labels it as the latest 10-K's current company-wide note; comparing it to the prior filing can also fabricate or hide changes. The yield script similarly compares lengths without checking block contexts, so its cached output does not establish this selection is safe in general.
- Test: add a synthetic instance containing a short current, consolidated, own-company block and a longer prior-period or dimensional block. The current block must be selected regardless of length; if a unique suitable current block cannot be established, show a retryable/explicit uncertainty rather than asserting absence or selecting another context. Include a valid repeated-block case and keep the filing-date cutoff.

Evidence verification: both supplied evidence folders pass their SHA256SUMS checks. This is a plan review; no production or backup database was opened and no implementation or live-run claim is made.
