# Review protocol: Claude Code builds, Codex reviews

Every milestone (and any PR big enough to deserve it) gets one review file in this folder.
Both agents read and write it; the user starts each step and decides merges.

## Roles

| Who | Does | Never |
|---|---|---|
| Claude Code | Builds the milestone, opens the PR, writes the request, answers each finding | Merges without the user; drops a finding silently; fixes without a failing test |
| Codex | Reviews the PR diff against the request, writes findings, re-checks answers; commits and pushes the review file only | Edits code, tests, data or other files; force-pushes; merges |
| User | Starts each step, settles disagreements, approves the merge | |

## File

`reviews/<milestone>-<slug>.md`, copied from `reviews/TEMPLATE.md`, e.g. `reviews/M5-backtest.md`.
One file per milestone; later rounds are appended to the same file.

The second line of the file is its status. Each agent changes it only as shown:

```
awaiting-review     Claude wrote the request              -> Codex reviews
awaiting-response   Codex wrote findings                  -> Claude answers
awaiting-recheck    Claude answered every finding         -> Codex re-checks
resolved            Codex agrees, or the user settled it  -> user merges
```

Find work with `grep -l "^Status: awaiting-review" reviews/*.md` (or `awaiting-recheck` / `awaiting-response`).
`reviews/TEMPLATE.md` has `Status: template`, which never matches.

## Handoff

The agents may work in different checkouts or worktrees, so uncommitted edits are invisible to
the other side. The PR branch on GitHub is the only shared copy:

1. Before reading a review file: `git fetch origin && git switch <branch> && git pull --ff-only`.
2. After writing your turn: commit and push. Codex commits **only** its review file, with the
   message `review(<milestone>): findings round <n>` or `review(<milestone>): re-check round <n>`.
   Claude's commits carry its fixes plus the review file.
3. Never force-push. If a push is rejected, `git pull --rebase` and push again; the status line
   means only one agent should be writing at a time, so a conflict is a sign to stop and ask the user.
4. Tell the user your turn is pushed; they start the other agent.

## Steps

1. **Request (Claude).** When the milestone's checks pass: open the PR, copy the template, fill in
   *Request* (branch, base and head commits, PR link, what changed, how to verify, where bugs are
   most likely, known gaps), set `Status: awaiting-review`, commit the file to the PR branch.
   Tell the user to start Codex.
2. **Review (Codex).** Pull the PR branch. Review `git diff <base>..<head>`. Write findings in
   *Findings* using the format below, set `Status: awaiting-response`, then commit and push the
   review file (see Handoff). Only this file changes.
3. **Response (Claude).** Pull the PR branch. For each finding, in order:
   - write the failing test the finding implies and run it against the head commit;
   - test fails as claimed: fix, keep the test, answer **fixed** with the commit and test name;
   - test passes or the claim does not hold: answer **unfounded** with the evidence
     (test name and output, a query on live data, or the line of code that already handles it);
   - real but out of scope: answer **deferred**, add it to ROADMAP.md, and say why. Needs the user's OK.
   Check every fix against live data where it matters and report the numbers. Commit, push,
   set `Status: awaiting-recheck`.
4. **Re-check (Codex).** Pull the PR branch. For each answer write **agree** or **disagree** with
   a reason in *Re-check*. New problems found in the fixes become new findings (next free ID). All
   agreed and no new findings: `Status: resolved`. Otherwise `Status: awaiting-response` for
   another round. Commit and push the review file.
5. **Close (user).** After two rounds, any finding still disputed goes to the user, who decides
   and writes the outcome under it. Then the user merges.

## Finding format (Codex)

```
### C<n>. <one-line claim>
- Severity: high (wrong shortlist, look-ahead, data loss, crash) | medium (wrong number or wrong quarantine) | low
- Where: path/to/file.py:<line>
- Evidence: what in the code makes it wrong; the input that triggers it and the wrong output
- Test: a minimal failing pytest in the style of the nearest tests/ file, or "unverified: <what would confirm it>"
```

IDs are never reused or renumbered: C1, C2, ... across all rounds.

## Answer format (Claude)

```
### C<n>: fixed | unfounded | deferred
- Test: tests/<file>.py::<name> (failed before, passes after) | the check that shows the claim does not hold
- Change: <commit sha> <what changed> | none
- Live data: <numbers before and after, if it changes results>
```

## Rules for both

- Point at code, not memory: file and line, test names, command output.
- Correctness first. No style, naming or formatting findings unless they hide a bug.
- The live database (`data/edgar.duckdb`) is read-only for Codex, and DuckDB allows one process:
  do not open it while the other agent is running a job. Never run `ingest` (network) during review.
- The project's invariants are in README.md (Invariants) and AGENTS.md / CLAUDE.md. A finding that
  breaks one of them is high severity.
