# Review: M6 Obsidian vault sync
Status: awaiting-response


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

### C1. A collision at the fallback filename overwrites another company's metadata
- Severity: high
- Where: src/obsidian/sync.py:85-92
- Evidence: _note_path checks whether TITLE.md exists but does not check whether TITLE (CIK).md also exists or belongs to another note. Reproduced with Companies/ACME.md (cik 42) and Companies/ACME (1234).md (cik 99); syncing ACME cik 1234 changes the second note's cik to 1234 and overwrites its generated fields and any existing auto block. Find an actually unoccupied path or fail safely before writing.
- Test:
```python
def test_company_fallback_collision_preserves_existing_note(con, tmp_path):
    folder = tmp_path / "Companies"
    folder.mkdir()
    (folder / "ACME.md").write_text("---\ncik: 42\n---\nfirst\n")
    other = folder / "ACME (1234).md"
    original = "---\ncik: 99\nstatus: personal\n---\nuser content\n"
    other.write_text(original)
    sync_vault(con, tmp_path, AS_OF)
    assert other.read_text() == original
```

### C2. Renaming an executive creates a duplicate purchase event
- Severity: medium
- Where: src/obsidian/sync.py:141-143
- Evidence: The event filename includes the current executive note filename, but existing events are not indexed by (accession, owner). Reproduced: sync a company with one purchase, rename the executive note to Renamed.md, and sync again; Events contains both the old and new event for the same filing/owner. Both appear as insider buys in the Dataview dashboard, and user edits remain stranded on the old event. Locate events by a stable identity and update links without creating a second record.
- Test:
```python
def test_renamed_executive_does_not_duplicate_event(con, tmp_path):
    sync_vault(con, tmp_path, AS_OF)
    person = next((tmp_path / "Executives").glob("*.md"))
    person.rename(person.with_name("Renamed.md"))
    sync_vault(con, tmp_path, AS_OF)
    assert len(list((tmp_path / "Events").glob("*.md"))) == 1
```

### C3. Long insider names truncate away event accession numbers
- Severity: high
- Where: src/obsidian/sync.py:141-147; src/obsidian/writer.py:45-49
- Evidence: slug truncates the whole title at 120 characters after the accession is appended. Two filings by the same insider on the same date and ticker with a long owner name consequently get identical event paths. Reproduced with owner name LAST plus 110 As and two distinct accessions: two separate buys produce only one event note, overwritten by the latter filing. Reserve space for a stable unique accession/owner suffix or use a separate identity-based filename.
- Test:
```python
def test_long_name_does_not_merge_distinct_filing_events(con, tmp_path):
    name = "LAST " + "A" * 110
    xml = form4_xml(77, name, "CEO",
                    [("2024-08-15", "P", 10000, 12, "A", 50000, "D", "", False)], doc_flag=0)
    for accn in ("0001234-24-000101", "0001234-24-000102"):
        store(con, parse_form4(xml, accn, date(2024, 8, 16)))
    sync_vault(con, tmp_path, AS_OF)
    events = [p for p in (tmp_path / "Events").glob("*.md") if fm(p).get("owner_cik") == 77]
    assert len(events) == 2
```

Verification: tests/test_m6_vault.py: 13 passed. All three findings were independently reproduced using in-memory databases and temporary vaults; the live vault and database were not modified.


## Response (Claude)

_Claude answers each finding here: fixed, unfounded or deferred._

## Re-check (Codex)

_Codex writes agree or disagree per answer here._

## Outcome (user)

_Only for findings still disputed after two rounds._
