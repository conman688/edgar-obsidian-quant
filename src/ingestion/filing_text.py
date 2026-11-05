"""10-K text: fetch the primary document, turn it into plain text, cut out Item 1A.

Only the companies the vault shows are fetched (one submissions index per company plus one
document per new 10-K), so request volume stays small. Raw HTML is not kept: only the cut
section goes into `filing_sections`, whose rows are also the ledger, so a filing is
downloaded at most once.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Protocol

import duckdb

FORMS = ("10-K", "10-KT")       # amendments (10-K/A) usually carry only Part III, not risk factors
SECTION = "risk_factors"
PER_COMPANY = 2                 # the latest 10-K and the one before it: enough for a redline

_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "hr"}
_SKIP = {"script", "style", "head", "title", "ix:header"}
_HIDDEN = re.compile(r"display\s*:\s*none", re.I)


class Sec(Protocol):
    def get(self, url: str) -> bytes: ...


class _Text(HTMLParser):
    """Visible text, one line per block element. Inline XBRL puts its hidden facts in a
    display:none <ix:header>; both that and scripts are skipped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip: list[str] = []        # open tags whose content is skipped

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip[0]:
                self.skip.append(tag)    # same tag nested inside the skipped one
            return
        style = dict(attrs).get("style") or ""
        if tag in _SKIP or _HIDDEN.search(style):
            self.skip.append(tag)
        elif tag in _BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_startendtag(self, tag, attrs):
        if not self.skip and tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[0]:
                self.skip.pop()
            return
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))   # source line breaks are only whitespace


def html_text(raw: bytes) -> str:
    p = _Text()
    p.feed(raw.decode("utf-8", errors="replace"))
    p.close()
    text = "".join(p.out).replace("\xa0", " ").replace("\u200b", "")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


_START = re.compile(r"^item\s*1a\b\.?", re.I | re.M)
_END = re.compile(r"^item\s*(1b|1c|2)\b", re.I | re.M)
_TITLE = re.compile(r"^[\s.:\-–—]*risk\s+factors[\s.:]*", re.I)


def risk_factors(text: str) -> str | None:
    """Item 1A body: from an 'Item 1A' heading line to the next Item 1B/1C/2 heading.

    The table of contents and cross-references also start lines with 'Item 1A', so the
    longest such span is the section itself."""
    best = ""
    for m in _START.finditer(text):
        end = _END.search(text, m.end())
        body = text[m.end():end.start() if end else len(text)]
        body = _TITLE.sub("", body, count=1).strip()
        if len(body) > len(best):
            best = body
    return best or None


def _submissions_url(cik: int) -> str:
    return f"https://data.sec.gov/submissions/CIK{cik:010d}.json"


def _doc_url(cik: int, accn: str, doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}"


def annual_reports(payload: bytes, as_of: date, n: int = PER_COMPANY) -> list[dict]:
    """The latest `n` 10-Ks filed on or before as_of, newest first."""
    recent = json.loads(payload)["filings"]["recent"]
    rows = []
    for form, accn, filed, report, doc in zip(recent["form"], recent["accessionNumber"], recent["filingDate"],
                                              recent["reportDate"], recent["primaryDocument"]):
        if form in FORMS and doc and date.fromisoformat(filed) <= as_of:
            rows.append({"form": form, "accn": accn, "filed": date.fromisoformat(filed),
                         "report_date": date.fromisoformat(report) if report else None, "doc": doc})
    rows.sort(key=lambda r: (r["filed"], r["accn"]), reverse=True)
    return rows[:n]


def fetch_sections(con: duckdb.DuckDBPyConnection, sec: Sec, ciks: list[int], as_of: date,
                   n: int = PER_COMPANY) -> Counter:
    """Store Item 1A of each company's latest `n` 10-Ks known on as_of. Returns the
    statuses of the rows added; filings already in the table are not downloaded again."""
    got: Counter = Counter()
    for cik in sorted(ciks):
        have = {a for (a,) in con.execute(
            "SELECT accn FROM filing_sections WHERE cik = ? AND section = ?", [cik, SECTION]).fetchall()}
        for f in annual_reports(sec.get(_submissions_url(cik)), as_of, n):
            if f["accn"] in have:
                continue
            url = _doc_url(cik, f["accn"], f["doc"])
            body = risk_factors(html_text(sec.get(url)))
            status = "ok" if body else "not_found"
            con.execute("INSERT INTO filing_sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [cik, f["accn"], f["form"], f["filed"], f["report_date"], SECTION, status, body, url,
                         datetime.now()])
            got[status] += 1
    return got
