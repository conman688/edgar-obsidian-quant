"""Idempotent, crash-safe Obsidian writer with canonical entity resolution.

- Atomic: render to a temp file in the same directory, fsync, os.replace. A crash
  leaves either the old file or the new one, never a truncated frontmatter.
- Merge: auto keys in frontmatter are overwritten, user keys are kept, `aliases`
  and `tags` are unioned. Only the AUTO block in the body is replaced. Frontmatter is
  round-tripped (ruamel), so the user's comments, key order and formatting survive.
- Identity: people are keyed by SEC owner CIK (from Form 4). One note per CIK;
  name variants become Obsidian `aliases`, which Obsidian resolves natively, so
  [[J. Smith]] and [[John A. Smith]] both open the same note.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

AUTO_START = "<!-- AUTO-GENERATED:START -->"
AUTO_END = "<!-- AUTO-GENERATED:END -->"
_FM = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_UNION_KEYS = ("aliases", "tags")
_BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}
_KEEP_UPPER = {"II", "III", "IV", "V", "LP", "LLC", "LLP"}
_ENTITY = re.compile(r"\b(INC|CORP|CORPORATION|LLC|LLP|LP|L\.P\.|LTD|TRUST|FUND|PARTNERS|CAPITAL|HOLDINGS"
                     r"|GROUP|MANAGEMENT|ADVISORS|COMPANY|FOUNDATION)\b", re.I)


def _case(word: str) -> str:
    if not word.isupper():
        return word                      # keep the filer's own mixed case (McDonald, de la Cruz)
    return word if word.strip(".") in _KEEP_UPPER else word.capitalize()


_YAML = YAML()                  # round-trip mode: keeps comments, order and quoting
_YAML.width = 4096              # never wrap long values across lines


def slug(name: str) -> str:
    """Filename-safe, wikilink-safe title. Keeps case and spaces for readable links."""
    s = unicodedata.normalize("NFKC", name).strip()
    s = _BAD.sub("", s)
    return re.sub(r"\s+", " ", s)[:120] or "unnamed"


def split_note(text: str) -> tuple[CommentedMap, str]:
    m = _FM.match(text)
    if not m:
        return CommentedMap(), text
    fm = _YAML.load(m.group(1))
    if fm is None:
        return CommentedMap(), text[m.end():]
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping; refusing to overwrite")
    return fm, text[m.end():]


def merge_frontmatter(old: CommentedMap, auto: dict) -> CommentedMap:
    """Update `old` in place: assigning an existing key keeps its position and comments."""
    for k, v in auto.items():
        if k in _UNION_KEYS:
            seen = old.get(k) or []
            new = [x for x in (v or []) if x not in seen]
            if k not in old or new:
                old[k] = list(seen) + new
        elif old.get(k, _MISSING) != v:
            old[k] = v
    return old


_MISSING = object()


def _dump(fm: CommentedMap) -> str:
    buf = io.StringIO()
    _YAML.dump(fm, buf)
    return buf.getvalue() if fm else ""


def replace_auto_block(body: str, auto_body: str) -> str:
    block = f"{AUTO_START}\n{auto_body.rstrip()}\n{AUTO_END}"
    starts, ends = body.count(AUTO_START), body.count(AUTO_END)
    if starts == 0 and ends == 0:
        return f"{block}\n\n{body.lstrip()}" if body.strip() else block + "\n"
    if starts != 1 or ends != 1 or body.index(AUTO_START) > body.index(AUTO_END):
        raise ValueError("malformed AUTO markers; fix the note by hand before re-sync")
    a, b = body.index(AUTO_START), body.index(AUTO_END) + len(AUTO_END)
    return body[:a] + block + body[b:]


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def create_note(path: Path, frontmatter: dict, body: str) -> None:
    """Write a new note atomically and never replace one: FileExistsError if `path` exists.
    The temp file is hard-linked into place, which fails rather than overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("---\n" + _dump(CommentedMap(frontmatter)) + "---\n" + body)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def upsert_note(path: Path, auto_frontmatter: dict, auto_body: str) -> bool:
    """Write or update a note. Returns False when the result is byte-identical (no write)."""
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    old_fm, old_body = split_note(old_text)
    fm = merge_frontmatter(old_fm, auto_frontmatter)
    body = replace_auto_block(old_body, auto_body)
    new_text = "---\n" + _dump(fm) + "---\n" + body
    if new_text == old_text:
        return False
    atomic_write(path, new_text)
    return True


class PersonRegistry:
    """Maps any observed name to one canonical note, keyed by SEC CIK when known.

    Backed by the person and person_issuer tables (src/db/schema.py) so resolution
    survives restarts. Names from proxies (no CIK) resolve only against people already
    linked to the same issuer; anything ambiguous goes to Executives/_unresolved/ for
    review instead of minting a new node.
    """

    def __init__(self, con):
        self.con = con

    @staticmethod
    def display_name(sec_name: str) -> str:
        """Form 4 owner names are 'Last First Middle [Suffix]' in any case ('SMITH JOHN A' in
        XML, 'Oliver George' in the bulk data sets). Turn into 'First Middle Last Suffix'.
        Entities (funds, trusts, LPs) keep their order."""
        parts = sec_name.replace(",", " ").split()
        if not _ENTITY.search(sec_name):
            suffix = []
            while len(parts) > 2 and parts[-1].strip(".").upper() in _SUFFIXES:
                suffix.insert(0, parts.pop())
            if len(parts) >= 2:
                parts = parts[1:] + parts[:1]
            parts += suffix
        return " ".join(_case(p) for p in parts)

    def register(self, cik: int, sec_name: str, issuer_cik: int) -> str:
        name = slug(self.display_name(sec_name)) if sec_name.strip() else ""
        row = next(iter(self.con.execute("SELECT canonical, aliases FROM person WHERE cik = ?", [cik]).fetchall()), None)
        if not name:                     # some filings leave the owner name blank
            if row is None:
                self.con.execute("INSERT INTO person VALUES (?, ?, ?)", [cik, f"Owner {cik}", []])
            self.con.execute("INSERT INTO person_issuer VALUES (?, ?) ON CONFLICT DO NOTHING", [cik, issuer_cik])
            return row[0] if row else f"Owner {cik}"
        if row is None:
            canonical = name
            if self.con.execute("SELECT 1 FROM person WHERE canonical = ?", [canonical]).fetchall():
                canonical = f"{name} ({cik})"  # two different people, same name
            self.con.execute("INSERT INTO person VALUES (?, ?, ?)", [cik, canonical, [name, sec_name]])
        else:
            canonical, aliases = row
            new = [a for a in (name, sec_name) if a not in aliases]
            if new:
                self.con.execute("UPDATE person SET aliases = ? WHERE cik = ?", [aliases + new, cik])
        self.con.execute("INSERT INTO person_issuer VALUES (?, ?) ON CONFLICT DO NOTHING", [cik, issuer_cik])
        return canonical

    def resolve(self, observed: str, issuer_cik: int) -> str | None:
        """Match a proxy name to a known insider of the same issuer by surname + first initial."""
        toks = [t.strip(".").lower() for t in observed.split() if t.strip(".")]
        if not toks:
            return None
        last, first_init = toks[-1], toks[0][0]
        rows = self.con.execute(
            """SELECT p.canonical, p.aliases FROM person p JOIN person_issuer pi USING (cik)
               WHERE pi.issuer_cik = ?""", [issuer_cik]).fetchall()
        hits = set()
        for canonical, aliases in rows:
            # Stored names are already first-name-first; raw SEC aliases are not.
            for a in [canonical] + [n for x in aliases for n in (x, self.display_name(x))]:
                at = [t.strip(".").lower() for t in a.split()]
                if at and at[-1] == last and at[0][0] == first_init:
                    hits.add(canonical)
        return hits.pop() if len(hits) == 1 else None

    def aliases(self, canonical: str) -> list[str]:
        row = next(iter(self.con.execute("SELECT aliases FROM person WHERE canonical = ?", [canonical]).fetchall()), None)
        return [a for a in (row[0] if row else []) if a != canonical]
