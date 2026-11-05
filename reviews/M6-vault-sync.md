# Review: M6 Obsidian vault sync
Status: awaiting-review


## Request (Claude)

- Branch: `feat/m6-vault-sync`  PR: https://github.com/conman688/edgar-obsidian-quant/pull/12
- Diff: `git diff 9ab4fab..0f3a27c` (code; the next commit adds only this file)
- Tests: `uv run pytest` -> 114 passed

### What changed
- `src/obsidian/sync.py` (new): `sync_vault(con, vault, as_of, filters, policy)`. Picks companies
  with status shortlist/quarantine or an existing note (found by frontmatter `cik`); purchases from
  `conviction_buys_sql(365)`; registers insiders in `PersonRegistry`; writes Events (one per
  accn + owner), Executives, Filings (sources + Form 4 accns, form/filed looked up in xbrl_facts),
  Companies, Dashboard.md; creates Journal/. Returns written/unchanged counts per folder.
- `src/obsidian/templates/*.md.j2` (new): company, executive, filing, event, dashboard.
- `src/obsidian/writer.py`: frontmatter round-tripped with ruamel.yaml (comments, order kept);
  `merge_frontmatter` assigns only changed keys; `display_name` always reorders Form 4 "Last First"
  names, keeps suffixes last, leaves entities; blank names register as "Owner <cik>"; `resolve`
  matches stored names as they are and SEC aliases reordered.
- `src/db/queries.py`: `conviction_buys_sql` (one row per purchase) split out of `cluster_buys_sql`.
- `src/cli.py`: `sync-vault --as-of DATE [--vault PATH]`.
- `pyproject.toml` / `uv.lock`: jinja2, ruamel.yaml.

### How to verify
- `uv run pytest` -> 114 passed (`tests/test_m6_vault.py`: 14).
- Live, only with my OK (it writes to the user's vault and the person table):
  `uv run --env-file .env python -m src.cli sync-vault --as-of 2026-11-03` -> 0 written, 762 unchanged.
  Reading the vault is fine: /Users/conor123/edgar-obsidian-quant-vault.

### Where bugs are most likely
- Idempotence: anything that changes between two syncs of the same date (dict/set ordering,
  float formatting, ruamel round-trip of values it did not write, list order from DuckDB).
- User data loss: frontmatter merge (union keys `aliases`/`tags`), notes whose frontmatter does not
  parse (skipped by the index: is a duplicate then created?), name collisions in `_note_path`.
- CIK index: two notes claiming the same cik; a renamed executive note; links to renamed notes.
- `display_name` on unusual names (single token, all suffixes, "Mc"/"de la" casing, entities).
- Point-in-time: the vault shows `as_last_known` as of the date; purchases from
  `conviction_buys_sql` with `p_since` = as_of - 365 days.
- The sync writes to the database (person tables) as a side effect.

### Known gaps (not findings unless they are worse than stated)
- Companies that leave the priced, non-financial universe are no longer updated (no screen row).
- Links inside notes use the target note's current file name; a rename by hand needs a re-sync
  for links to follow.
- The dashboard needs the Dataview plugin; "new insider buys" is relative to today, not as_of.
- Events and filings of companies that drop out are kept, never deleted.

## Findings (Codex)

_Codex writes C1, C2, ... here._

## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
