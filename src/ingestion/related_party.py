"""M10: the related-party note (`us-gaap:RelatedPartyTransactionsDisclosureTextBlock`) of each
company's latest two 10-Ks, from the filing's XBRL instance.

Per new 10-K, two requests: the filing folder's `index.json`, to find its one `*_htm.xml`
instance (its name does not always follow the primary document), then the instance itself.
The text block's HTML becomes text through M7's `html_text`. Raw files are not kept.

Which block is current (review C5/C6): a block counts only in a context of the filer itself
(entity identifier = its CIK), with no dimensions (no segment, no scenario) and ending on the
instance's `dei:DocumentPeriodEndDate`. Identical repeats count once; length never decides.

One `filing_sections` row per filing, section `fn_related_party`, with its own PARSER. The row
is also the fetch ledger, kept apart from `risk_factors`: either can be complete while the other
is missing, and each is filled on its own. status / text:
- `ok`: the current block's text.
- `not_found`: the instance parsed and the tag is in no context. Final.
- `ambiguous`: blocks exist but no unique current one. text is the reason, `elsewhere` (only
  other periods or entities) or `conflict` (two different current company-wide texts). Final
  for this PARSER; a parser bump fetches it again.
- `error`: a request failed, the index has no or several `*_htm.xml`, or the instance does
  not parse. text is the error. Retried on every run, and listed in the health warnings.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime

import duckdb

from src.ingestion.filing_text import PER_COMPANY, Sec, _doc_url, _submissions_url, annual_reports, html_text

SECTION = "fn_related_party"
TAG = "RelatedPartyTransactionsDisclosureTextBlock"
PARSER = 1
FINAL = ("ok", "not_found", "ambiguous")      # an `error` row is fetched again

_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def _ns_local(tag: str) -> tuple[str, str]:
    ns, _, local = tag[1:].rpartition("}") if tag.startswith("{") else ("", "", tag)
    return ns, local


def _block_html(el: ET.Element) -> str:
    """A text block's content: escaped HTML (SEC's instances), or HTML as child elements."""
    return (el.text or "") + "".join(ET.tostring(c, encoding="unicode") for c in el)


def _contexts(root: ET.Element) -> dict[str, dict]:
    out = {}
    for c in root.iter(f"{_XBRLI}context"):
        ident = c.findtext(f"{_XBRLI}entity/{_XBRLI}identifier") or ""
        period = c.find(f"{_XBRLI}period")
        end = None if period is None else (period.findtext(f"{_XBRLI}endDate") or period.findtext(f"{_XBRLI}instant"))
        dims = c.find(f"{_XBRLI}entity/{_XBRLI}segment") is not None or c.find(f"{_XBRLI}scenario") is not None
        out[c.get("id")] = {"entity": ident.strip(), "end": (end or "").strip(), "dims": dims}
    return out


def _own(entity: str, cik: int) -> bool:
    return entity.isdigit() and int(entity) == cik


def select_block(raw: bytes, cik: int) -> tuple[str, str | None]:
    """('ok', text) | ('not_found', None) | ('ambiguous', 'elsewhere' | 'conflict').

    Raises ET.ParseError for malformed XML, and ValueError when blocks exist but the instance
    has no single DocumentPeriodEndDate to place them by. An instance never declares entities;
    one that does is refused (ValueError) rather than expanded: expat never fetches external
    entities, and this also rules out entity-expansion bombs."""
    if b"<!ENTITY" in raw:
        raise ValueError("instance declares XML entities")
    root = ET.fromstring(raw)
    blocks, period_ends = [], set()
    for el in root:
        ns, local = _ns_local(el.tag)
        if local == TAG and ns.startswith("http://fasb.org/us-gaap/"):
            if el.get(_XSI_NIL) == "true":
                continue
            text = html_text(_block_html(el).encode())
            if text:
                blocks.append((el.get("contextRef"), text))
        elif local == "DocumentPeriodEndDate" and ns.startswith("http://xbrl.sec.gov/dei/"):
            period_ends.add((el.text or "").strip())
    if not blocks:
        return "not_found", None
    if len(period_ends) != 1 or not next(iter(period_ends)):
        raise ValueError(f"instance has {len(period_ends)} dei:DocumentPeriodEndDate values: {sorted(period_ends)}")
    end = period_ends.pop()
    ctx = _contexts(root)
    current = {text for ref, text in blocks
               if (c := ctx.get(ref)) and not c["dims"] and c["end"] == end and _own(c["entity"], cik)}
    if len(current) == 1:
        return "ok", current.pop()
    return "ambiguous", "conflict" if current else "elsewhere"


def _instance_name(index: bytes) -> str:
    names = [i["name"] for i in json.loads(index)["directory"]["item"] if i["name"].endswith("_htm.xml")]
    if len(names) != 1:
        raise ValueError(f"index lists {len(names)} *_htm.xml instances" + (f": {', '.join(names)}" if names else ""))
    return names[0]


def read_filing(sec: Sec, cik: int, accn: str) -> tuple[str, str | None, str]:
    """(status, text, url) for one 10-K. Every failure to read it is status `error`, with the
    error as text: a 404, SEC throttling, a bad index or malformed XML must never stop the run
    or be taken for an absent note."""
    url = _doc_url(cik, accn, "index.json")
    try:
        url = _doc_url(cik, accn, _instance_name(sec.get(url)))
        status, text = select_block(sec.get(url), cik)
    except Exception as e:                          # noqa: BLE001 - see docstring
        return "error", f"{type(e).__name__}: {e}"[:500], url
    return status, text, url


def fetch_related_party(con: duckdb.DuckDBPyConnection, sec: Sec, ciks: list[int], as_of: date,
                        n: int = PER_COMPANY) -> Counter:
    """Store the related-party note of each company's latest `n` 10-Ks filed on or before as_of.
    Returns the statuses of the rows written. A filing with a final row at this PARSER is not
    fetched again; an `error` row or an older PARSER's row is fetched again and replaced."""
    got: Counter = Counter()
    for cik in sorted(ciks):
        have = {a for (a,) in con.execute(
            "SELECT accn FROM filing_sections WHERE cik = ? AND section = ? AND parser >= ? AND list_contains(?, status)",
            [cik, SECTION, PARSER, list(FINAL)]).fetchall()}
        for f in annual_reports(sec.get(_submissions_url(cik)), as_of, n):
            if f["accn"] in have:
                continue
            status, text, url = read_filing(sec, cik, f["accn"])
            # One row per filing: one statement, so it commits whole or not at all.
            con.execute("""INSERT OR REPLACE INTO filing_sections
                               (cik, accn, form, filed, report_date, section, status, text, url, fetched_at, parser)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [cik, f["accn"], f["form"], f["filed"], f["report_date"], SECTION, status, text, url,
                         datetime.now(), PARSER])
            got[status] += 1
    return got
