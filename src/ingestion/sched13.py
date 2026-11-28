"""Schedule 13D and 13G stakes of 5% or more (M12-B7), informational only.

Live facts (SEC, 2026-11-27; ROADMAP item 7):
- No bulk data set: filings are listed in the quarterly form.idx and fetched one by one. The
  form type is the first 17 characters of a line. Each filing is listed twice, under the
  subject company and under the filer; only the submission header says which is which.
- XML filings (SCHEDULE 13D, 13D/A, 13G, 13G/A) are the main form from 2025Q1; structured data
  became mandatory on 2024-12-18. Older 13D filings (SC 13D, SC 13D/A) are HTML or text cover
  pages. Legacy 13G is not loaded (user, 2026-11-27): most 13G(b) stakes repeat 13F.
- Known at the filing date, never the event date: 23 of 99 sampled initial XML 13Ds came more
  than 20 business days after their event.
"""
import html
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

import duckdb

from src.ingestion.form13f import valid_cusip
from src.ingestion.related_party import _refuse_dtd

SCHEMA_VERSION = 1
INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{n}/form.idx"
FILING_URL = "https://www.sec.gov/Archives/{path}"
# form.idx type -> (schedule, amendment, source)
FORMS = {"SCHEDULE 13D": ("13D", False, "xml"), "SCHEDULE 13D/A": ("13D", True, "xml"),
         "SCHEDULE 13G": ("13G", False, "xml"), "SCHEDULE 13G/A": ("13G", True, "xml"),
         "SC 13D": ("13D", False, "text"), "SC 13D/A": ("13D", True, "text")}
FORM_WIDTH = 17
COLUMNS = ("accn", "form", "amendment", "form_type", "source", "issuer_cik", "filer_cik", "filer_name", "cusip",
           "event_date", "filed", "accepted", "rule", "persons", "shares", "pct")


# --- index ------------------------------------------------------------------------------

def parse_form_index(text: str) -> list[tuple[str, int, str, date, str]]:
    """(accn, cik, form_type, filed, path) for the FORMS lines of a form.idx."""
    out = []
    for line in text.splitlines():
        form = line[:FORM_WIDTH].strip()
        if form not in FORMS:
            continue
        _, cik, filed, path = line.rsplit(None, 3)
        accn = path.rsplit("/", 1)[1].removesuffix(".txt")
        out.append((accn, int(cik), form, date.fromisoformat(filed), path))
    return out


def load_index(con: duckdb.DuckDBPyConnection, quarter: str, text: str) -> int:
    rows = [(quarter, *r) for r in parse_form_index(text)]
    con.begin()
    try:
        con.execute("DELETE FROM sched13_index WHERE quarter = ?", [quarter])
        con.executemany("INSERT OR IGNORE INTO sched13_index VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return len(rows)


def indexed_quarters(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SELECT DISTINCT quarter FROM sched13_index").fetchall()}


# --- parsing --------------------------------------------------------------------------

def _header(raw: str) -> dict:
    hdr = raw[:raw.find("</SEC-HEADER>")]
    subj = re.search(r"SUBJECT COMPANY:.*?CENTRAL INDEX KEY:\s*(\d+)", hdr, re.S)
    by = re.search(r"FILED BY:.*?COMPANY CONFORMED NAME:\s*([^\n]+).*?CENTRAL INDEX KEY:\s*(\d+)", hdr, re.S)
    acc = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", hdr)
    filed = re.search(r"FILED AS OF DATE:\s*(\d{8})", hdr)
    return {"form_type": re.search(r"CONFORMED SUBMISSION TYPE:\s*([^\n]+)", hdr).group(1).strip(),
            "subject": int(subj.group(1)) if subj else None,
            "filer_cik": int(by.group(2)) if by else None,
            "filer_name": by.group(1).strip() if by else None,
            "accepted": datetime.strptime(acc.group(1), "%Y%m%d%H%M%S") if acc else None,
            "filed": datetime.strptime(filed.group(1), "%Y%m%d").date()}


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().replace(",", "")
    return float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else None


def _pct_ok(p: float | None) -> bool:
    """A percent of class is 0-100; anything else is a filer or parse error (live: 862,190 shares
    typed into classPercent), read as unknown."""
    return p is not None and 0 <= p <= 100


def _texts(root: ET.Element, *names: str) -> list[str]:
    want = {n.lower() for n in names}
    return [(e.text or "").strip() for e in root.iter() if e.tag.rsplit("}", 1)[-1].lower() in want]


def _parse_xml(doc: str) -> dict:
    raw = doc.encode()
    _refuse_dtd(raw)                    # no entity declaration is ever expanded
    root = ET.fromstring(raw)
    event = (_texts(root, "dateOfEvent", "eventDateRequiresFilingThisStatement") or [""])[0]
    shares = [x for x in map(_num, _texts(root, "aggregateAmountOwned",
                                          "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")) if x is not None]
    pcts = [x for x in map(_num, _texts(root, "percentOfClass", "classPercent")) if _pct_ok(x)]
    issuer = (_texts(root, "issuerCik") or [""])[0]           # issuerCik (13G) and issuerCIK (13D)
    cusips = [c.upper() for c in _texts(root, "issuerCusipNumber", "issuerCUSIP") if c]
    rules = sorted({r.removeprefix("Rule ").strip() for r in _texts(root, "designateRulePursuantThisScheduleFiled") if r})
    return {"issuer_cik": int(issuer) if issuer.isdigit() else None,
            "cusip": cusips[0] if cusips else None,
            "event_date": datetime.strptime(event, "%m/%d/%Y").date() if re.fullmatch(r"\d\d/\d\d/\d{4}", event) else None,
            "rule": ",".join(rules) or None,
            "persons": len(_texts(root, "reportingPersonName")),
            "shares": max(shares) if shares else None, "pct": max(pcts) if pcts else None}


_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%m/%d/%Y", "%m/%d/%y")
_EVENT = re.compile(r"([A-Za-z]+\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{2,4})\s*\(\s*Date of Event", re.I)
_PCT = re.compile(r"PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW\s*\(?\s*\d+\s*\)?\s*[:.]?\s*(\d+(?:\.\d+)?)\s*%", re.I)
_SHARES = re.compile(r"AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON\s*[:.]?\s*(\d[\d,]*(?:\.\d+)?)"
                     r"(?!\s*(?:CHECK|%))", re.I)
_CUSIP = re.compile(r"CUSIP\s*(?:No\.?|Number|#)?\s*[:.]?\s*([0-9A-Z]{6} ?[0-9A-Z]{2} ?[0-9A-Z])\b")
_PERSON = re.compile(r"NAMES? OF REPORTING PERSONS?", re.I)


def _plain(doc: str) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", " ", doc)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t)


def _date(s: str) -> date | None:
    s = re.sub(r"\s+,", ",", s.strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _parse_text(doc: str) -> dict:
    """The cover pages of a legacy SC 13D: event date, CUSIP, and per person rows 11 (shares) and
    13 (percent). The largest person's stake stands for the filing: group members repeat it."""
    t = _plain(doc)
    t = re.sub(r"(\d) ,", r"\1,", t)
    ev = _EVENT.search(t)
    cusips = [c.replace(" ", "") for c in _CUSIP.findall(t)]
    cusip = next((c for c in cusips if valid_cusip(c)), None)
    shares = [float(x.replace(",", "")) for x in _SHARES.findall(t)]
    pcts = [p for p in map(float, _PCT.findall(t)) if _pct_ok(p)]
    return {"issuer_cik": None, "cusip": cusip, "event_date": _date(ev.group(1)) if ev else None, "rule": None,
            "persons": len(_PERSON.findall(t)), "shares": max(shares) if shares else None,
            "pct": max(pcts) if pcts else None}


def parse_submission(raw: bytes, accn: str, form_type: str) -> dict | None:
    """One stake row from a full submission (.txt), or None if it has no schedule document."""
    text = raw.decode("utf-8", "replace")
    form, amendment, source = FORMS[form_type]
    h = _header(text)
    if source == "xml":
        m = re.search(r"<XML>\s*(.*?)\s*</XML>", text, re.S)
        if not m or "edgarSubmission" not in m.group(1)[:400]:
            return None
        body = _parse_xml(m.group(1))
    else:
        docs = re.findall(r"<DOCUMENT>\s*<TYPE>([^\n]+)\n(.*?)</DOCUMENT>", text, re.S)
        doc = next((d for t, d in docs if t.strip().upper().startswith("SC 13D")), None)
        if doc is None:
            return None
        body = _parse_text(doc)
    issuer = body.pop("issuer_cik") or h["subject"]
    return {"accn": accn, "form": form, "amendment": amendment, "form_type": form_type, "source": source,
            "issuer_cik": issuer, "filer_cik": h["filer_cik"], "filer_name": h["filer_name"],
            "filed": h["filed"], "accepted": h["accepted"], **body}


# --- fetching -------------------------------------------------------------------------

@dataclass
class FetchStats:
    pending: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    seconds: float = 0.0


def pending_filings(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[tuple[str, str, str]]:
    """Indexed filings that list a priced company (as subject or filer) and are not loaded yet.
    The path is the subject's when that is priced, else any listed one."""
    return con.execute(f"""
        SELECT i.accn, arg_min(i.path, i.cik NOT IN (SELECT cik FROM price_status)), min(i.form_type)
        FROM sched13_index i
        WHERE i.accn IN (SELECT accn FROM sched13_index WHERE cik IN (SELECT cik FROM price_status))
          AND i.accn NOT IN (SELECT accn FROM sched13_filings WHERE status IN ('done', 'skipped'))
        GROUP BY i.accn ORDER BY min(i.filed), i.accn {f'LIMIT {int(limit)}' if limit else ''}
    """).fetchall()


def fetch_filings(con: duckdb.DuckDBPyConnection, get: Callable[[str], bytes], limit: int | None = None,
                  batch_size: int = 50, progress: Callable[[FetchStats], None] | None = None) -> FetchStats:
    """Fetch and store pending filings. Each batch commits on its own, so a stopped run resumes
    where it left off; failures are retried on the next run."""
    t0, stats = time.monotonic(), FetchStats()
    todo = pending_filings(con, limit)
    stats.pending = len(todo)
    for i in range(0, len(todo), batch_size):
        rows, ledger_rows = [], []
        for accn, path, form_type in todo[i:i + batch_size]:
            now = datetime.now()
            try:
                r = parse_submission(get(FILING_URL.format(path=path)), accn, form_type)
                if r is None:
                    ledger_rows.append((accn, "skipped", "no schedule document", now))
                    stats.skipped += 1
                    continue
                rows.append(r)
                ledger_rows.append((accn, "done", None, now))
                stats.done += 1
            except Exception as e:
                ledger_rows.append((accn, "failed", repr(e)[:300], now))
                stats.failed += 1
        con.begin()
        try:
            if rows:
                ph = ",".join("?" * len(COLUMNS))
                con.executemany(f"INSERT OR REPLACE INTO sched13_stakes ({','.join(COLUMNS)}) VALUES ({ph})",
                                [[r[c] for c in COLUMNS] for r in rows])
            con.executemany("INSERT OR REPLACE INTO sched13_filings VALUES (?, ?, ?, ?)", ledger_rows)
            con.commit()
        except BaseException:
            con.rollback()
            raise
        if progress:
            progress(stats)
    stats.seconds = time.monotonic() - t0
    return stats
