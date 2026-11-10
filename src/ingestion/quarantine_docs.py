"""M11 Phase 2 parsers for one filing's documents (no network here).

- `instance_name`: the one `*_htm.xml` XBRL instance named in the filing's index.json.
- `parse_instance` / `cover_classes` (2b): DEI cover counts and trading symbols per
  `StatementClassOfStockAxis` member.
- `balance_sheet_report` / `r_page` / `zero_debt` (2c): debt is proved zero only when every
  liability row of the consolidated balance sheet is a listed non-debt element and the rows
  reconcile to `Liabilities` within the rounding budget of their `decimals`.

Every failure to read a document is a `DocError`: the caller records an `error` row and retries
on the next run. A document that reads fine but proves nothing is a normal result, never zero.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
CLASS_AXIS = "us-gaap:StatementClassOfStockAxis"
COVER_COUNT = "dei:EntityCommonStockSharesOutstanding"
TRADING_SYMBOL = "dei:TradingSymbol"
METHOD = "balance_sheet_no_debt"

# Fixed list of balance-sheet liability elements that are not debt: payables, accruals, deferred
# revenue, operating leases, taxes and similar. Anything else keeps the quarantine, including
# aggregates with "other" in them and every extension element (M11 2c).
NON_DEBT = frozenset(f"us-gaap:{t}" for t in (
    "AccountsPayableCurrent", "AccountsPayableNoncurrent", "AccountsPayableTradeCurrent",
    "AccountsPayableRelatedPartiesCurrent",
    "AccruedLiabilitiesCurrent", "AccruedLiabilitiesNoncurrent", "AccountsPayableAndAccruedLiabilitiesCurrent",
    "EmployeeRelatedLiabilitiesCurrent", "AccruedSalariesCurrent", "AccruedEmployeeBenefitsCurrent",
    "ContractWithCustomerLiabilityCurrent", "ContractWithCustomerLiabilityNoncurrent",
    "DeferredRevenueCurrent", "DeferredRevenueNoncurrent",
    "OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent", "OperatingLeaseLiability",
    "TaxesPayableCurrent", "AccruedIncomeTaxesCurrent", "AccruedIncomeTaxesNoncurrent",
    "SalesAndExciseTaxPayableCurrent",
    "DeferredIncomeTaxLiabilitiesNet", "DeferredTaxLiabilitiesNoncurrent",
    "DividendsPayableCurrent"))
SUBTOTALS = ("us-gaap:LiabilitiesCurrent", "us-gaap:LiabilitiesNoncurrent")
TOTAL = "us-gaap:Liabilities"
ASSETS = "us-gaap:Assets"
DEBT_ROW = re.compile(r"(Debt|Notes|Borrowing|LineOfCredit|Loan)")
NOT_DEBT_ROW = re.compile(r"(Lease|Receivable|Asset)")

_PREFIX = (("http://fasb.org/us-gaap/", "us-gaap"), ("http://xbrl.sec.gov/dei/", "dei"))


class DocError(Exception):
    """A document could not be read: failed request, wrong instance count, no balance-sheet
    page, malformed XML or HTML. Recorded as an `error` row and retried next run."""


@dataclass
class Instance:
    # id -> (instant, dimensions, entity identifier)
    contexts: dict[str, tuple[date | None, tuple[tuple[str, str], ...], str]] = field(default_factory=dict)
    facts: list[dict] = field(default_factory=list)       # name, context, value (str), decimals


def instance_name(index_json: bytes) -> str:
    try:
        items = json.loads(index_json)["directory"]["item"]
        names = [i["name"] for i in items]
    except (ValueError, KeyError, TypeError) as e:
        raise DocError(f"malformed index.json: {e}") from None
    hits = [n for n in names if n.endswith("_htm.xml")]
    if len(hits) != 1:
        raise DocError(f"{len(hits)} *_htm.xml instances in the filing index")
    return hits[0]


def _qname(tag: str) -> str:
    """'{ns}Local' -> 'prefix:Local'; extension namespaces keep a prefix from their URI."""
    if not tag.startswith("{"):
        return tag
    ns, local = tag[1:].split("}", 1)
    for uri, p in _PREFIX:
        if ns.startswith(uri):
            return f"{p}:{local}"
    return f"ext:{local}" if not ns.startswith("http://www.xbrl.org/") else tag


def parse_instance(xml: bytes) -> Instance:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise DocError(f"malformed instance XML: {e}") from None
    inst = Instance()
    # Extension prefixes as the filer declared them (ElementTree drops prefixes).
    ext: dict[str, str] = {}
    for m in re.finditer(rb'xmlns:([\w.-]+)="([^"]+)"', xml[:200_000]):
        ext[m.group(2).decode()] = m.group(1).decode()
    for c in root.iter(f"{XBRLI}context"):
        instant = c.find(f"{XBRLI}period/{XBRLI}instant")
        dims = tuple(sorted((m.get("dimension"), (m.text or "").strip())
                            for m in c.iter(f"{XBRLDI}explicitMember")))
        typed = any(True for _ in c.iter(f"{XBRLDI}typedMember"))
        if typed:
            dims += (("typed", "typed"),)
        day = date.fromisoformat(instant.text.strip()) if instant is not None and instant.text else None
        ident = c.find(f"{XBRLI}entity/{XBRLI}identifier")
        inst.contexts[c.get("id")] = (day, dims, (ident.text or "").strip() if ident is not None else "")
    for el in root:
        ctx = el.get("contextRef")
        if ctx is None or not el.tag.startswith("{"):
            continue
        ns, local = el.tag[1:].split("}", 1)
        name = _qname(el.tag)
        if name.startswith("ext:"):
            name = f"{ext.get(ns, 'ext')}:{local}"
        inst.facts.append({"name": name, "context": ctx, "value": (el.text or "").strip(),
                           "decimals": el.get("decimals")})
    return inst


def cover_classes(inst: Instance) -> dict:
    """Cover counts per share-class member, the class's own symbol, and every symbol tagged.

    conflict: two contexts for one class disagree (count or date). Facts on any axis other than
    the share-class axis, alone, are not a class.
    """
    def member(ctx: str) -> str | None:
        dims = inst.contexts.get(ctx, (None, (), ""))[1]
        return dims[0][1] if len(dims) == 1 and dims[0][0] == CLASS_AXIS else None

    counts: dict[str, set] = {}
    syms: dict[str | None, set] = {}
    for f in inst.facts:
        if f["name"] == COVER_COUNT:
            m = member(f["context"])
            if m:
                counts.setdefault(m, set()).add((float(f["value"]), inst.contexts[f["context"]][0]))
        elif f["name"] == TRADING_SYMBOL and f["value"]:
            dims = inst.contexts.get(f["context"], (None, (), ""))[1]
            syms.setdefault(member(f["context"]) if dims else None, set()).add(f["value"].upper())
    conflict = any(len(v) > 1 for v in counts.values())
    classes = []
    for m in sorted(counts):
        (shares, day), *_ = sorted(counts[m], key=lambda x: (x[1] or date.min, x[0]))
        own = syms.get(m, set())
        classes.append({"member": m, "shares": shares, "symbol": next(iter(own)) if len(own) == 1 else None,
                        "dei_date": day})
        conflict = conflict or len(own) > 1
    symbols = sorted(set().union(*syms.values())) if syms else []
    return {"classes": classes, "symbols": symbols, "conflict": conflict}


_CONSOLIDATED = re.compile(r"consolidated|combined", re.I)
_NOT_CONSOLIDATED = re.compile(r"parent|registrant|guarantor|issuer|subsidiar", re.I)


def balance_sheet_report(filing_summary: bytes) -> dict:
    """The filing's balance-sheet statement: {html, name, consolidated}.

    `consolidated` is True only when the statement's name says consolidated or combined and names
    no parent, registrant, guarantor, issuer or subsidiary scope (C13): no dimensions on the facts
    does not prove consolidated scope. Raises DocError when there is no balance-sheet statement or
    several consolidated ones.
    """
    try:
        root = ET.fromstring(filing_summary)
    except ET.ParseError as e:
        raise DocError(f"malformed FilingSummary.xml: {e}") from None
    hits = []
    for rep in root.iter("Report"):
        name, cat = (rep.findtext("ShortName") or ""), (rep.findtext("MenuCategory") or "")
        if (cat == "Statements" and re.search(r"balance sheet|financial position|financial condition", name, re.I)
                and not re.search(r"parenthetical", name, re.I) and rep.findtext("HtmlFileName")):
            hits.append({"html": rep.findtext("HtmlFileName"), "name": name,
                         "consolidated": bool(_CONSOLIDATED.search(name)) and not _NOT_CONSOLIDATED.search(name)})
    if not hits:
        raise DocError("no balance-sheet statement in FilingSummary.xml")
    cons = [h for h in hits if h["consolidated"]]
    if len(cons) > 1:
        raise DocError(f"{len(cons)} consolidated balance-sheet statements in FilingSummary.xml")
    return cons[0] if cons else hits[0]


_DATE = re.compile(r"([A-Z][a-z]{2})\.? (\d{1,2}), (\d{4})")


def r_page(html: bytes) -> tuple[list[date], list[tuple[str, str]]]:
    """Column dates (header order) and rows as (element, label) in statement order."""
    text = html.decode("utf-8", "replace")
    table = re.search(r"<table[^>]*class=\"report\"[^>]*>(.*?)</table>", text, re.S)
    if not table:
        raise DocError("no report table in the balance-sheet R page")
    dates = []
    for th in re.findall(r"<th[^>]*class=\"th\"[^>]*>(.*?)</th>", table.group(1), re.S):
        m = _DATE.search(unescape(re.sub(r"<[^>]+>", " ", th)))
        if m:
            dates.append(datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date())
    rows = []
    for prefix, local, label in re.findall(r"defref_([\w-]+?)_(\w+)'[^>]*>(.*?)</a>", table.group(1), re.S):
        rows.append((f"{prefix}:{local}", " ".join(unescape(re.sub(r"<[^>]+>", " ", label)).split())))
    if not dates or not rows:
        raise DocError("balance-sheet R page has no dated column or no rows")
    return dates, rows


def _half_unit(decimals: str | None) -> float | None:
    if decimals is None:
        return None
    if decimals.upper() == "INF":
        return 0.0
    return 0.5 * 10 ** (-int(decimals))


def zero_debt(dates: list[date], rows: list[tuple[str, str]], inst: Instance, column: date, cik: int) -> dict:
    """Whether the balance sheet's `column` proves debt is zero (M11 2c). Never raises on content.

    Liability rows are those between `Assets` and `Liabilities` in statement order. Values and
    `decimals` come from undimensioned instance facts dated `column`.
    """
    def no(reason: str, candidates=()):
        return {"debt_zero": False, "reason": reason, "column": column, "method": None,
                "candidates": list(candidates)}

    labels = {e: lab for e, lab in rows}
    rows = [e for e, _ in rows if not e.endswith("Abstract")]          # section headings carry no value
    if column not in dates:
        return no(f"no balance-sheet column dated {column} (columns: {', '.join(map(str, dates))})")
    if TOTAL not in rows:
        return no("no Liabilities total row, so coverage cannot be shown")
    start = rows.index(ASSETS) + 1 if ASSETS in rows else 0
    liab = rows[start:rows.index(TOTAL)]
    candidates = [r for r in liab if DEBT_ROW.search(r.split(":", 1)[1]) and not NOT_DEBT_ROW.search(r.split(":", 1)[1])]
    if candidates:
        return no("debt-like rows on the balance sheet: " + ", ".join(candidates), candidates)
    if len(set(liab)) != len(liab):
        return no("an element appears on more than one row")
    unlisted = [r for r in liab if r not in NON_DEBT and r not in SUBTOTALS]
    if unlisted:
        return no("rows not on the non-debt list: " + ", ".join(unlisted))
    # C12: an allowed element on a row whose label says it also holds "other" items is an aggregate.
    aggregates = [f"{labels[r]} ({r})" for r in liab if re.search(r"\bother\b", labels[r], re.I)]
    if aggregates:
        return no("aggregate rows: " + ", ".join(aggregates))

    # Only the reporting entity's own undimensioned contexts (C13).
    plain = {c for c, (d, dims, ident) in inst.contexts.items()
             if d == column and not dims and ident.isdigit() and int(ident) == cik}
    vals: dict[str, tuple[float, float | None]] = {}
    for f in inst.facts:
        if f["context"] in plain and (f["name"] in liab or f["name"] == TOTAL):
            try:
                v = float(f["value"])
            except ValueError:
                return no(f"{f['name']} has a non-numeric value")
            prev = vals.get(f["name"])
            if prev and prev[0] != v:
                return no(f"{f['name']} has two different values at {column}")
            vals[f["name"]] = (v, _half_unit(f["decimals"]))
    if TOTAL not in vals:
        return no(f"no Liabilities value at {column}")
    used = [r for r in liab if r in vals] + [TOTAL]
    if any(vals[r][1] is None for r in used):
        return no("a fact has no decimals attribute: " + ", ".join(r for r in used if vals[r][1] is None))

    def reconcile(children: list[str], total: str) -> str | None:
        got = sum(vals[c][0] for c in children)
        budget = sum(vals[c][1] for c in children) + vals[total][1]
        residual = abs(vals[total][0] - got)
        return None if residual <= budget else (
            f"residual {residual:,.0f} between {total} {vals[total][0]:,.0f} and its rows {got:,.0f} "
            f"exceeds the rounding budget {budget:,.0f}")

    top: list[str] = []
    group: list[str] = []
    for r in liab:
        if r not in vals:
            continue                               # not reported in this column
        if r in SUBTOTALS:
            if not group:                          # C11: a subtotal is not itself a non-debt category
                return no(f"subtotal {r} has no classified rows of its own")
            err = reconcile(group, r)
            if err:
                return no(err)
            top.append(r)                          # the subtotal stands in for its children
            group = []
        else:
            group.append(r)
    top += group
    err = reconcile(top, TOTAL)
    if err:
        return no(err)
    return {"debt_zero": True, "reason": f"every liability row at {column} is a listed non-debt element and "
                                         f"the rows reconcile to Liabilities", "column": column, "method": METHOD,
            "candidates": []}
