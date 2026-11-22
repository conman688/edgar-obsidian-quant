"""Year-over-year redline of 10-K Risk Factors, sentence by sentence.

A sentence is new when no sentence of the prior year's section is at least SIMILAR alike
(word-level difflib ratio) after numbers and dates are masked, so "fiscal 2023 ... 31%"
and "fiscal 2024 ... 28%" are the same sentence. Removed sentences are found the same way
in the other direction.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

import duckdb

from src.forensics import STALE_FY_DAYS
from src.ingestion import related_party as rp
from src.ingestion.filing_text import SECTION
from src.tone import tone

SIMILAR = 0.8
MIN_WORDS = 4            # shorter lines are headings' page numbers, "Table of Contents", etc.
CANDIDATES = 12          # prior sentences scored per sentence, picked by shared words

_ABBREV = re.compile(r"\b(?:U\.S|U\.K|E\.U|Inc|Corp|Co|Ltd|No|Nos|Mr|Ms|Mrs|Dr|St|vs|e\.g|i\.e|etc|Jr|Sr)\.", re.I)
_DOT = "․"          # one-dot leader: stands in for abbreviation dots while splitting
_SPLIT = re.compile(r"(?<=[.!?])[\"'”)]*\s+(?=[\"'“(]*[A-Z])")
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december"
           "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_DATE = re.compile(rf"\b(?:{_MONTHS})\.?(?:\s+\d{{1,2}},?)?\s+\d{{4}}\b|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b", re.I)
_NUM = re.compile(r"[$€£]?\d[\d,]*(?:\.\d+)?%?")
_WORD = re.compile(r"[a-z#<>]+")


_PAGE = re.compile(r"^(?:[-\u2013\u2014\s]*(?:page\s+)?\d{1,3}[-\u2013\u2014\s]*|[ivxlc]{1,6}|table of contents)$", re.I)


def _paragraphs(text: str) -> list[str]:
    """Lines without page furniture (page numbers, "Table of Contents" headers), with a line
    that starts in lower case or follows a comma joined to the one before: some filings
    break sentences across lines and pages, and only in some years."""
    out: list[str] = []
    for line in (ln.strip() for ln in text.split("\n")):
        if not line or _PAGE.match(line):
            continue
        if out and (line[0].islower() or out[-1].endswith(",")):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return out


def sentences(text: str) -> list[str]:
    out = []
    for para in _paragraphs(text):
        masked = _ABBREV.sub(lambda m: m.group(0).replace(".", _DOT), para.strip())
        for s in _SPLIT.split(masked):
            s = s.replace(_DOT, ".").strip()
            if len(s.split()) >= MIN_WORDS:
                out.append(s)
    return out


def normalize(s: str) -> str:
    s = _DATE.sub(" <date> ", s.lower().replace("’", "'"))
    s = _NUM.sub(" # ", s)
    return " ".join(_WORD.findall(s))


@dataclass
class Redline:
    new: list[str]
    removed: list[str]
    total: int           # sentences this year
    prior_total: int


def _unmatched(these: list[str], others: list[str]) -> list[str]:
    """Sentences of `these` with no counterpart in `others` at SIMILAR or above."""
    norm = [normalize(s) for s in others]
    exact = set(norm)
    words = [w.split() for w in norm]
    index: dict[str, set[int]] = {}
    for i, ws in enumerate(words):
        for w in set(ws):
            index.setdefault(w, set()).add(i)
    out, seen = [], set()
    for s in these:
        n = normalize(s)
        if n in exact or n in seen:      # a risk summary repeats its sentences in the body
            continue
        seen.add(n)
        ws = n.split()
        shared = Counter(i for w in set(ws) for i in index.get(w, ()))
        sm = SequenceMatcher(None, autojunk=False)
        sm.set_seq2(ws)                  # difflib caches facts about seq2: score many seq1 against it
        if not any(_ratio(sm, words[i]) >= SIMILAR for i, _ in shared.most_common(CANDIDATES)):
            out.append(s)
    return out


def _ratio(sm: SequenceMatcher, words: list[str]) -> float:
    sm.set_seq1(words)
    return sm.ratio()


def redline(prior: str, current: str) -> Redline:
    old, new = sentences(prior), sentences(current)
    return Redline(new=_unmatched(new, old), removed=_unmatched(old, new), total=len(new), prior_total=len(old))


def _latest_two(con: duckdb.DuckDBPyConnection, section: str, ciks: list[int], as_of: date) -> dict[int, list[tuple]]:
    """Per company, its latest two rows of `section` filed on or before as_of, newest first:
    (cik, accn, filed, report_date, status, text, url)."""
    rows = con.execute("""
        SELECT cik, accn, filed, report_date, status, text, url FROM filing_sections
        WHERE section = $section AND filed <= $as_of AND list_contains($ciks, cik)
        QUALIFY row_number() OVER (PARTITION BY cik ORDER BY filed DESC, accn DESC) <= 2
        ORDER BY cik, filed DESC, accn DESC
    """, {"section": section, "as_of": as_of, "ciks": sorted(ciks)}).fetchall()
    by_cik: dict[int, list[tuple]] = {}
    for r in rows:
        by_cik.setdefault(r[0], []).append(r)
    return by_cik


def risk_factor_redline(con: duckdb.DuckDBPyConnection, ciks: list[int], as_of: date,
                        lists: dict[str, set[str]] | None = None) -> dict[int, dict]:
    """Per company: the latest 10-K known on as_of against the one before it.

    status: ok | no_prior (one 10-K known) | no_section (latest has no Item 1A found) |
    no_prior_section. Companies with no 10-K in the table are left out. With word `lists`
    (src/tone.py), an ok redline also carries `tone`: {list: (this year, prior year)}."""
    out = {}
    for cik, rs in _latest_two(con, SECTION, ciks, as_of).items():
        cur, prior = rs[0], rs[1] if len(rs) > 1 else None
        r = {"accn": cur[1], "filed": cur[2], "report_date": cur[3], "prior_accn": prior and prior[1],
             "prior_report_date": prior and prior[3], "new": [], "removed": [], "total": None, "prior_total": None}
        if cur[4] != "ok":
            r["status"] = "no_section"
        elif prior is None:
            r["status"] = "no_prior"
        elif prior[4] != "ok":
            r["status"] = "no_prior_section"
        else:
            d = redline(prior[5], cur[5])
            r.update(status="ok", new=d.new, removed=d.removed, total=d.total, prior_total=d.prior_total)
            if lists is not None:
                now, before = tone(cur[5], lists), tone(prior[5], lists)
                r["tone"] = {k: (now[k], before[k]) for k in lists}
        out[cik] = r
    return out


# M10: what the vault says for each related-party result that is not a current note (review C6).
RP_MESSAGES = {
    "not_found": "no related-party note tagged in this 10-K",
    "elsewhere": "related-party note found only for another period or entity; not compared",
    "conflict": ("related-party note could not be uniquely identified: two different current company-wide notes; "
                 "not compared"),
    "error": "the 10-K's XBRL instance could not be read; retried on the next fetch-text",
}
RP_NO_COMPARISON = {None: "no prior 10-K known", "not_found": "the prior 10-K has no related-party note tagged",
                    "ambiguous": "the prior 10-K's related-party note could not be uniquely identified",
                    "error": "the prior 10-K's XBRL instance could not be read"}


def related_party_redline(con: duckdb.DuckDBPyConnection, ciks: list[int], as_of: date) -> dict[int, dict]:
    """Per company: the related-party note of the latest 10-K filed by as_of, against the 10-K
    before it with `redline` as is. Companies with no row are left out.

    status is the latest row's (ok | not_found | ambiguous | error); `reason` is an ambiguous
    row's elsewhere | conflict, or an error row's error, and `message` what the vault shows for
    anything but ok. A comparison needs both notes ok; otherwise `compared` is False and
    `no_comparison` says why. `stale`: the report date is more than STALE_FY_DAYS before as_of
    (or unknown). Informational only: nothing here feeds the screen."""
    out = {}
    for cik, rs in _latest_two(con, rp.SECTION, ciks, as_of).items():
        cur, prior = rs[0], rs[1] if len(rs) > 1 else None
        status, report_date = cur[4], cur[3]
        r = {"accn": cur[1], "filed": cur[2], "report_date": report_date, "url": cur[6], "status": status,
             "reason": None if status in ("ok", "not_found") else cur[5],
             "text": cur[5] if status == "ok" else None,
             "message": RP_MESSAGES[cur[5] if status == "ambiguous" else status] if status != "ok" else None,
             "stale": report_date is None or (as_of - report_date).days > STALE_FY_DAYS,
             "prior_accn": prior and prior[1], "prior_report_date": prior and prior[3],
             "prior_status": prior and prior[4], "compared": False, "no_comparison": None,
             "new": [], "removed": [], "total": None, "prior_total": None}
        if status == "ok" and prior is not None and prior[4] == "ok":
            d = redline(prior[5], cur[5])
            r.update(compared=True, new=d.new, removed=d.removed, total=d.total, prior_total=d.prior_total)
        elif status == "ok":
            r["no_comparison"] = RP_NO_COMPARISON[prior and prior[4]]
        out[cik] = r
    return out
