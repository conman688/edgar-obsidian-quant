# Instructions for Claude Code

You **build**; Codex **reviews**. The workflow is in `reviews/PROTOCOL.md`.

- When a milestone's acceptance checks pass and its PR is open: copy `reviews/TEMPLATE.md` to
  `reviews/<milestone>-<slug>.md`, fill in the Request, set `Status: awaiting-review`, commit it to
  the PR branch, and tell the user Codex can start. Do not merge before the review is `resolved`.
- When a review file says `Status: awaiting-response`: answer every finding as fixed, unfounded
  or deferred, following the protocol. Reproduce each one with a failing test first. Unfounded
  needs evidence; deferred needs the user's OK. Then set `Status: awaiting-recheck`.
- Check each session for open reviews: `grep -l "^Status: awaiting-response" reviews/*.md`.

Project rules are in README.md (Invariants) and AGENTS.md (the same list Codex reviews against).
How the user likes to work: one branch per task, PR into main, ask before merging; failing test
before every fix; check against live data and report numbers; `graphify update .` after milestones;
short, plain answers.
