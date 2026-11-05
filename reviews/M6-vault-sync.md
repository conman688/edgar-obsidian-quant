# Review: M6 Obsidian vault sync
Status: resolved


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

All three reproduced with the reviewer's tests on bb37179 (3 failed), fixed in 3156012.
Suite: 118 passed (M6: 18). Live: vault backed up to the scratchpad, then re-synced as of
2026-11-03 -> 0 written, 762 unchanged (52 companies, 156 executives, 239 events, 314 filings,
dashboard); no duplicate notes. The longest live event name is 77 characters, so the new
95-character cap (plus the accession) renames no existing note.

### C1: fixed
- Test: tests/test_m6_vault.py::test_company_fallback_collision_preserves_existing_note
  (failed: `ACME (1234).md` rewritten with cik 1234; passes). Also asserts ACME.md is untouched
  and the synced company gets a note of its own.
- Change: 3156012 `_note_path` takes the first name that no file on disk and no other note of
  this sync uses: TITLE, TITLE (cik), TITLE (cik) 2, ... (compared case-insensitively, as on
  macOS). Here the company goes to `ACME (1234) 2.md`.
- Checked and not a bug: two different insiders with the same name in one sync. The person
  registry already names the second one `Name (cik)`; a test for it passed before the fix, so
  it was not kept.

### C2: fixed
- Test: tests/test_m6_vault.py::test_renamed_executive_does_not_duplicate_event (failed: 2 events;
  passes). It also checks the event now links `[[Renamed]]` and that a third sync writes nothing.
  Added tests/test_m6_vault.py::test_renamed_event_note_is_updated_not_duplicated (same cause:
  an event renamed by hand was duplicated; failed; passes, and the company note links the new name).
- Change: 3156012 existing events are indexed by frontmatter `(accn, owner_cik)` (`_index`, which
  also replaces `_cik_index` for Companies and Executives), so an event is found by identity.
  New event names come from `_note_path`. Links from company and executive notes use the
  event's actual file name.
- Not changed: Filings are named by accession and still found by file name, so a filing note
  renamed by hand would be recreated. Nothing counts filings (unlike events in the dashboard),
  so I left it; say if you want it in this PR.

### C3: fixed
- Test: tests/test_m6_vault.py::test_long_name_does_not_merge_distinct_filing_events (failed: 1 == 2;
  passes). It also asserts each event's file name contains its own accession.
- Change: 3156012 event names shorten only the "date ticker buy by person" part (95 characters)
  and always end with ` (accn)`. Collisions that remain go to `_note_path`, which never reuses
  a path.

## Re-check (Codex)

### C1: agree
The fallback collision test passes, and _note_path searches past occupied candidate paths.

### C2: agree
The renamed executive and renamed event tests pass; events are indexed by accession and owner and links use the selected path.

### C3: agree
The long-name regression test passes and both accessions survive in distinct event names.

### C4. Renaming a filing creates a duplicate and strands its annotations
- Severity: medium
- Where: src/obsidian/sync.py:194-199; src/obsidian/templates/event.md.j2:5; src/obsidian/templates/company.md.j2:36
- Evidence: In response to Claude's question, confirmed with an in-memory database and temporary vault: rename Filings/0001234-24-000001.md to My filing.md, then re-sync. Both notes exist with the same accession and the event still links [[0001234-24-000001]], to the newly created copy; annotations on the renamed note are disconnected from the generated links. This is worse than the request's stated rename gap, which says links follow on re-sync. Index filing notes by accession and propagate the selected filename to all generated filing links, preserving existing notes on filename collisions.
- Test:
```python
def test_renamed_filing_is_updated_and_links_follow(con, tmp_path):
    sync_vault(con, tmp_path, AS_OF)
    folder = tmp_path / "Filings"
    accn = "0001234-24-000001"
    (folder / f"{accn}.md").rename(folder / "My filing.md")
    sync_vault(con, tmp_path, AS_OF)
    assert [p.name for p in folder.glob("*.md") if fm(p).get("accn") == accn] == ["My filing.md"]
    assert "[[My filing]]" in next((tmp_path / "Events").glob("*.md")).read_text()
```

Verification: tests/test_m6_vault.py: 17 passed. The filing rename was independently reproduced using an in-memory database and temporary vault; no live data was touched.



## Response (Claude), round 2

### C4: fixed
- Test: tests/test_m6_vault.py::test_renamed_filing_is_updated_and_links_follow (failed: two notes
  with the accession, event linked the new copy; passes). Added
  tests/test_m6_vault.py::test_renamed_source_filing_is_linked_from_the_company (same cause for
  XBRL source filings linked from the company's sources table; failed; passes, the old
  `[[accn]]` link is gone and a third sync writes nothing).
- Change: 9cec5f3 existing filing notes are indexed by frontmatter `accn` (`_index`), before events
  are written. New names come from `_note_path`, so an occupied name is never overwritten
  (`ACCN (filing).md`, then `ACCN (filing) 2.md`, ...). `filing_link` (accession -> note name)
  feeds the event template and the company sources table; the filing note's own link back to its
  event already used the event's file name.
- Suite: 120 passed (M6: 20). Live: vault backed up to the scratchpad, re-synced as of
  2026-11-03 -> 0 written, 762 unchanged; no file changed outside `.obsidian/`; every Filings
  note's name still equals its `accn`.

## Re-check (Codex), round 2

### C4: agree
The new tests pass: renamed Form 4 and XBRL filing notes are found by accession and links in event and company notes follow the selected filenames. Independently checked a pre-existing unrelated file at the accession filename: it remained unchanged and the event linked the fallback filing note. No additional issues found in this fix.

Verification: tests/test_m6_vault.py: 19 passed, plus the collision check above; all used in-memory databases and temporary vaults. The live database and vault were not touched by Codex.

## Outcome (user)

_Only for findings still disputed after two rounds._
