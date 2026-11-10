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

import io
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
ISO4217 = "http://www.xbrl.org/2003/iso4217"
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
# C15: allowed elements that roll up other allowed elements (us-gaap calculation parents). With any
# of its children on the statement, a rollup stands in once for them, and they must reconcile to it.
# Ordered so that a narrower rollup is applied before a wider one that contains it.
ROLLUPS = [(f"us-gaap:{p}", frozenset(f"us-gaap:{c}" for c in cs)) for p, cs in (
    ("AccountsPayableCurrent", ("AccountsPayableTradeCurrent", "AccountsPayableRelatedPartiesCurrent")),
    ("EmployeeRelatedLiabilitiesCurrent", ("AccruedSalariesCurrent", "AccruedEmployeeBenefitsCurrent")),
    ("TaxesPayableCurrent", ("AccruedIncomeTaxesCurrent", "SalesAndExciseTaxPayableCurrent")),
    ("OperatingLeaseLiability", ("OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent")),
    ("AccountsPayableAndAccruedLiabilitiesCurrent", (
        "AccountsPayableCurrent", "AccountsPayableTradeCurrent", "AccountsPayableRelatedPartiesCurrent",
        "AccruedLiabilitiesCurrent", "EmployeeRelatedLiabilitiesCurrent", "AccruedSalariesCurrent",
        "AccruedEmployeeBenefitsCurrent")))]
# C15: a broad accrual aggregate beside an element it may contain. A flat statement cannot show the
# two lines are disjoint, so the pair is refused (not assumed nested, not assumed separate).
AMBIGUOUS = [(f"us-gaap:{b}", frozenset(f"us-gaap:{p}" for p in ps)) for b, ps in (
    ("AccruedLiabilitiesCurrent", ("EmployeeRelatedLiabilitiesCurrent", "AccruedSalariesCurrent",
                                   "AccruedEmployeeBenefitsCurrent", "TaxesPayableCurrent", "AccruedIncomeTaxesCurrent",
                                   "SalesAndExciseTaxPayableCurrent", "DividendsPayableCurrent")),
    ("AccruedLiabilitiesNoncurrent", ("AccruedIncomeTaxesNoncurrent",)),
    ("AccountsPayableAndAccruedLiabilitiesCurrent", ("TaxesPayableCurrent", "AccruedIncomeTaxesCurrent",
                                                     "SalesAndExciseTaxPayableCurrent", "DividendsPayableCurrent")))]
# Alternative elements for one concept: both on one statement cannot be told apart, so refused.
ALTERNATIVES = [frozenset(f"us-gaap:{t}" for t in pair) for pair in (
    ("DeferredRevenueCurrent", "ContractWithCustomerLiabilityCurrent"),
    ("DeferredRevenueNoncurrent", "ContractWithCustomerLiabilityNoncurrent"),
    ("DeferredIncomeTaxLiabilitiesNet", "DeferredTaxLiabilitiesNoncurrent"))]
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
    facts: list[dict] = field(default_factory=list)       # name, context, value (str), decimals, unit, nil
    # unit id -> ISO 4217 currency code for a single-measure monetary unit, else None (C16)
    units: dict[str, str | None] = field(default_factory=dict)


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


def _canonical(uri: str, local: str) -> str:
    """An expanded name as 'us-gaap:X' / 'dei:X' for the standard taxonomies, else '{uri}X'."""
    for base, p in _PREFIX:
        if uri.startswith(base):
            return f"{p}:{local}"
    return f"{{{uri}}}{local}"


def _resolve(text: str | None, scope: dict[str, str]) -> tuple[str, str] | None:
    """A QName value (attribute or element text) resolved against its element's in-scope
    namespaces (C17): the prefix as written means nothing on its own."""
    if not text:
        return None
    prefix, local = text.strip().split(":", 1) if ":" in text else ("", text.strip())
    uri = scope.get(prefix)
    return (uri, local) if uri else None


def parse_instance(xml: bytes) -> Instance:
    """Contexts (date, dimensions as expanded names, entity identifier), units (ISO 4217 code or
    None) and top-level facts. QName values are resolved with each element's in-scope namespace
    declarations, so aliases, local declarations and rebinding are handled."""
    scopes: dict[ET.Element, dict[str, str]] = {}
    stack: list[dict[str, str]] = [{}]
    pending: dict[str, str] = {}
    root = None
    try:
        for ev, obj in ET.iterparse(io.BytesIO(xml), events=("start-ns", "start", "end")):
            if ev == "start-ns":
                pending[obj[0]] = obj[1]
            elif ev == "start":
                scope = {**stack[-1], **pending} if pending else stack[-1]
                pending = {}
                stack.append(scope)
                scopes[obj] = scope
                root = obj if root is None else root
            else:
                stack.pop()
    except ET.ParseError as e:
        raise DocError(f"malformed instance XML: {e}") from None
    if root is None:
        raise DocError("empty instance XML")
    inst = Instance()
    for u in root.iter(f"{XBRLI}unit"):
        measures = u.findall(f"{XBRLI}measure")    # direct children only: a divide unit has none
        r = _resolve(measures[0].text, scopes[measures[0]]) if len(measures) == 1 else None
        inst.units[u.get("id")] = r[1].upper() if r and r[0] == ISO4217 else None
    for c in root.iter(f"{XBRLI}context"):
        instant = c.find(f"{XBRLI}period/{XBRLI}instant")
        dims = []
        for m in c.iter(f"{XBRLDI}explicitMember"):
            axis, member = _resolve(m.get("dimension"), scopes[m]), _resolve(m.text, scopes[m])
            dims.append((_canonical(*axis) if axis else f"unresolved:{m.get('dimension')}",
                         _canonical(*member) if member else f"unresolved:{(m.text or '').strip()}"))
        if any(True for _ in c.iter(f"{XBRLDI}typedMember")):
            dims.append(("typed", "typed"))
        try:
            day = date.fromisoformat(instant.text.strip()) if instant is not None and instant.text else None
        except ValueError as e:
            raise DocError(f"bad context date in instance: {e}") from None
        ident = c.find(f"{XBRLI}entity/{XBRLI}identifier")
        inst.contexts[c.get("id")] = (day, tuple(sorted(dims)), (ident.text or "").strip() if ident is not None else "")
    for el in root:
        ctx = el.get("contextRef")
        if ctx is None or not el.tag.startswith("{"):
            continue
        ns, local = el.tag[1:].split("}", 1)
        name = _canonical(ns, local)
        if name.startswith("{"):                   # extension: show it with the filer's own prefix
            p = next((k for k, v in scopes[el].items() if v == ns and k), None)
            name = f"{p}:{local}" if p else name
        inst.facts.append({"name": name, "context": ctx, "value": (el.text or "").strip(),
                           "decimals": el.get("decimals"), "nil": el.get(XSI_NIL) == "true",
                           "unit": el.get("unitRef")})
    return inst


def _own(inst: Instance, ctx: str, cik: int) -> bool:
    ident = inst.contexts.get(ctx, (None, (), ""))[2]
    return ident.isdigit() and int(ident) == cik


def cover_classes(inst: Instance, cik: int) -> dict:
    """The filer's cover counts per share-class member, each class's own symbol, and every symbol
    the filer tagged. Only contexts whose entity identifier is `cik` count (C18).

    conflict: two contexts for one class disagree (count or date), a class has two symbols, or a
    class count is nil. Facts on any axis other than the share-class axis, alone, are not a class.
    """
    def member(ctx: str) -> str | None:
        dims = inst.contexts.get(ctx, (None, (), ""))[1]
        return dims[0][1] if len(dims) == 1 and dims[0][0] == CLASS_AXIS else None

    counts: dict[str, set] = {}
    syms: dict[str | None, set] = {}
    nil = False
    for f in inst.facts:
        if not _own(inst, f["context"], cik):
            continue
        if f["name"] == COVER_COUNT:
            m = member(f["context"])
            if m and f["nil"]:
                nil = True                         # C14: a nil count is unusable, never zero shares
            elif m:
                try:
                    n = float(f["value"])
                except ValueError:
                    raise DocError(f"non-numeric cover count {f['value']!r} for {m}") from None
                if not math.isfinite(n) or n < 0:
                    nil = True                     # unusable like nil: never a share count
                    continue
                counts.setdefault(m, set()).add((n, inst.contexts[f["context"]][0]))
        elif f["name"] == TRADING_SYMBOL and f["value"]:
            dims = inst.contexts.get(f["context"], (None, (), ""))[1]
            syms.setdefault(member(f["context"]) if dims else None, set()).add(f["value"].upper())
    conflict = nil or any(len(v) > 1 for v in counts.values())
    classes = []
    for m in sorted(counts):
        (shares, day), *_ = sorted(counts[m], key=lambda x: (x[1] or date.min, x[0]))
        own = syms.get(m, set())
        classes.append({"member": m, "shares": shares, "symbol": next(iter(own)) if len(own) == 1 else None,
                        "dei_date": day})
        conflict = conflict or len(own) > 1
    symbols = sorted(set().union(*syms.values())) if syms else []
    return {"classes": classes, "symbols": symbols, "conflict": conflict}


_CONSOLIDATED = re.compile(r"\bconsolidated\b", re.I)
# Negated or narrower scopes. "Combined" alone does not prove the issuer's consolidated scope (C13).
_NOT_CONSOLIDATED = re.compile(r"\b(?:un|non)-?consolidated\b|parent|registrant|guarantor|issuer|subsidiar", re.I)


def balance_sheet_report(filing_summary: bytes) -> dict:
    """The filing's balance-sheet statement: {html, name, consolidated}.

    `consolidated` is True only when the statement's name has the whole word "consolidated", is not
    un- or non-consolidated, and names no parent, registrant, guarantor, issuer or subsidiary scope (C13): no dimensions on the facts
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
    """Half a unit of the reported precision; None when missing or unreadable (C14)."""
    if decimals is None:
        return None
    if decimals.upper() == "INF":
        return 0.0
    try:
        return 0.5 * 10 ** (-int(decimals))
    except ValueError:
        return None


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
    plain = {c for c, (d, dims, _) in inst.contexts.items() if d == column and not dims and _own(inst, c, cik)}
    vals: dict[str, tuple[float, float | None]] = {}
    for f in inst.facts:
        if f["context"] in plain and (f["name"] in liab or f["name"] == TOTAL):
            if f["nil"]:
                return no(f"{f['name']} is nil at {column}")
            try:
                v = float(f["value"])
            except ValueError:
                return no(f"{f['name']} has a non-numeric value")
            prev = vals.get(f["name"])
            if prev and prev[0] != v:
                return no(f"{f['name']} has two different values at {column}")
            currency = inst.units.get(f["unit"]) if f["unit"] else None
            if currency != "USD":                  # C16: one currency, the screen's (USD)
                return no(f"{f['name']} is not in a USD monetary unit (unitRef {f['unit']!r}: {currency})")
            vals[f["name"]] = (v, _half_unit(f["decimals"]))
    if TOTAL not in vals:
        return no(f"no Liabilities value at {column}")
    used = [r for r in liab if r in vals] + [TOTAL]
    if any(vals[r][1] is None for r in used):
        return no("a fact has no readable decimals attribute: " + ", ".join(r for r in used if vals[r][1] is None))

    def reconcile(children: list[str], total: str) -> str | None:
        got = sum(vals[c][0] for c in children)
        budget = sum(vals[c][1] for c in children) + vals[total][1]
        residual = abs(vals[total][0] - got)
        return None if residual <= budget else (
            f"residual {residual:,.0f} between {total} {vals[total][0]:,.0f} and its rows {got:,.0f} "
            f"exceeds the rounding budget {budget:,.0f}")

    for broad, parts in AMBIGUOUS:
        if broad in liab and parts & set(liab):
            return no(f"ambiguous coverage: {broad} beside {', '.join(sorted(parts & set(liab)))}")
    for alt in ALTERNATIVES:
        if alt <= set(liab):
            return no("two elements for one concept: " + ", ".join(sorted(alt)))
    consumed: set[str] = set()                     # children replaced by their rollup (C15)
    for parent, children in ROLLUPS:
        if parent not in vals:
            continue
        kids = [r for r in liab if r in children and r in vals and r not in consumed]
        if kids:
            err = reconcile(kids, parent)
            if err:
                return no(err)
            consumed.update(kids)
    top: list[str] = []
    group: list[str] = []
    for r in liab:
        if r not in vals or r in consumed:
            continue                               # not reported in this column, or counted in a rollup
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
