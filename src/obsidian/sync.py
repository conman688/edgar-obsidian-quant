"""Write the screen into the Obsidian vault.

One note per company (shortlisted, quarantined, or already in the vault), per insider
(SEC owner CIK, name variants as aliases), per insider purchase (Events), and per filing a
number came from (Filings), plus Dashboard.md with Dataview tables.

Notes are found by the `cik` in their frontmatter, not by file name, so a note the user
renames keeps being updated. Only the AUTO-GENERATED block and the auto frontmatter keys
change (src/obsidian/writer.py); everything else in a note is the user's. A sync writes a
file only when its content changes, so re-syncing unchanged data writes nothing.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from itertools import count
from pathlib import Path

import duckdb
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from src.db.pit import Policy
from src.db.queries import EVENT_ITEMS, conviction_buys_sql, events_8k_sql, insider_sells_sql
from src.forensics import STALE_FY_DAYS, assess, forensics
from src.journal import REVIEW_DAYS, read_journals, since, snapshot, to_date
from src.obsidian.writer import PersonRegistry, create_note, slug, split_note, upsert_note
from src.redline import related_party_redline, risk_factor_redline
from src.tone import load_lists
from src import config
from src.ingestion.macro import macro_regime
from src.screen import Filters, screen

FOLDERS = ("Companies", "Executives", "Filings", "Events", "Journal", "Sources")
SYNCED = ("shortlist", "quarantine")     # statuses that get a note; existing notes are always updated
SOURCES = {
    "FINRA short interest": {
        "what": "Equity short interest positions reported by FINRA member firms, twice a month, all exchanges and OTC.",
        "access": "Public, no key: settlement dates from api.finra.org, files from cdn.finra.org "
                  "(shrtYYYYMMDD.csv, pipe-delimited). Fetched at 1 request a second with a generic User-Agent.",
        "terms": "FINRA Data is provided for non-commercial use.",
        "history": "Settlement dates from 2017-12-29; nothing earlier on the public file list.",
        "known_at": "The later of settlement + 9 weekdays (FINRA publishes 7 business days after "
                    "settlement) and the day we first fetched the file. Rows can be FINRA revisions, "
                    "so the 2017-2026 backfill is known only from the day it ran (2026-11-25 or later).",
        "refresh": "Saturday (`ingest all`); each settlement file is fetched once.",
    },
    "FRED macro series": {
        "what": "T10Y2Y (10-year minus 2-year Treasury spread, daily) and NFCI (Chicago Fed National "
                "Financial Conditions Index, weekly), as ALFRED published them. Informational only.",
        "access": "FRED REST API (api.stlouisfed.org) with a free key, FRED_API_KEY in .env, sent to no other "
                  "host. 1 request a second, generic User-Agent, at most 2,000 vintages a request.",
        "terms": "FRED API Terms of Use. Neither series carries a copyright notice on FRED. The ICE BofA "
                 "high-yield spread (BAMLH0A0HYM2) is not stored: ICE prohibits reproduction.",
        "history": "Vintages from 2011-05-25 (NFCI) and 2014-01-27 (T10Y2Y); older observations are known "
                   "only from those dates.",
        "known_at": "The ALFRED vintage date (realtime_start). NFCI is revised every week; each value is read "
                    "as it was published on or before the date.",
        "refresh": "Saturday (`ingest all`); only vintages newer than the newest stored are fetched.",
    },
    "Cboe volatility indexes": {
        "what": "Daily closes of VIX (30-day S&P 500 implied volatility), VIX9D (9-day) and VVIX (volatility "
                "of VIX). VIX9D/VIX above 1 is read as short-term stress (vol_backwardation). Informational only.",
        "access": "Public, no key: cdn-api.cboe.com/api/global/us_indices/daily_prices/<INDEX>_History.csv. "
                  "1 request a second, generic User-Agent.",
        "terms": "Cboe states the data is furnished without responsibility for accuracy.",
        "history": "VIX from 1990-01-02, VIX9D from 2011-01-04, VVIX from 2006-03-06. Cboe backdated the "
                   "modern VIX to 1990 after its 2003 redesign, so early values were not published at the time.",
        "known_at": "The later of the trade date and our first fetch of the close: the backfill is known only "
                    "from the day it ran. VIX9D/VIX uses two closes from the same trade date.",
        "refresh": "Saturday (`ingest all`); each close is stored once.",
    },
    "Factor returns (French, q5, AQR)": {
        "what": "Monthly US factor returns used only to explain the backtest (its Factor attribution section): "
                "Fama-French 3 and 5 factors (2x3) and momentum, Hou-Xue-Zhang q5 (MKT, ME, IA, ROE, EG), and "
                "AQR Quality Minus Junk and Betting Against Beta (USA column). Never an input to the screen.",
        "access": "Public, no key: Kenneth French's data library (mba.tuck.dartmouth.edu, CSV zips), global-q.org "
                  "(q5_factors_monthly_2025.csv; the name changes each year) and aqr.com (xlsx, first sheet). "
                  "Generic User-Agent. `fetch-factors` writes data/factors/YYYY-MM-DD with a sha256 manifest.",
        "terms": "The files carry copyright notices (Fama and French; Asness, Frazzini and Pedersen) and state no "
                 "licence. Raw files stay in data/ (not in git); only regression results are published.",
        "history": "French from 1926-07 (5 factors from 1963-07), q5 from 1967-01, QMJ from 1957-07, BAB from "
                   "1930-12. French and q5 are in percent, AQR in decimals; all are stored as decimals.",
        "known_at": "Not point-in-time. All three recompute their full histories at each update, so a report "
                    "names the one snapshot it used (date and sha256). A same-date fetch keeps that snapshot.",
        "refresh": "Manual (`fetch-factors`); not scheduled.",
    },
}
SOURCES["SEC Form 13F data sets"] = {
    "what": "Quarter-end long positions that investment managers with $100M or more in 13(f) securities "
            "report on Form 13F, summed per CUSIP: managers reporting a position and their shares. "
            "Informational only.",
    "access": "Public, no key: one zip per filing window from sec.gov/data-research/sec-markets-data/"
              "form-13f-data-sets, through the SEC client (8 requests a second, project User-Agent). Each zip "
              "is deleted after loading. CUSIPs are matched to tickers with SEC fails-to-deliver files "
              "(cnsfails, from 2009-07), then to the company's current ticker.",
    "terms": "SEC data, public. SEC does not guarantee the data sets' accuracy; they are not a substitute "
             "for the filings.",
    "history": "Filing windows from 2013-04 (quarter 2013-03-31). SEC refreshed every set in March 2024.",
    "known_at": "A quarter counts only reports filed within 45 days of its end (the next business day when "
                "that is a weekend or Presidents Day) and is read from that deadline. Late reports and "
                "later amendments are left out and counted. A restatement replaces a report; a new-holdings "
                "amendment adds to it. Only share lines, never options or principal amounts.",
    "refresh": "Manual (`ingest 13f`); each zip and fails-to-deliver file is loaded once.",
}
SELL_LABELS = {"discretionary": "discretionary", "planned": "10b5-1 plan", "unknown": "plan unknown"}
HISTORY_DAYS = 365                       # insider purchases shown per company and person
TONE_KEYS = ("negative", "litigious", "uncertainty")
NEW_SHOWN, REMOVED_SHOWN = 25, 10        # risk-factor sentences listed per company note
DECISION_KEYS = ("status", "ev_ebit", "fcf_yield", "net_cash_to_mcap")   # dashboard: then vs now


@dataclass
class SyncStats:
    written: Counter = field(default_factory=Counter)
    unchanged: Counter = field(default_factory=Counter)

    def record(self, folder: str, changed: bool) -> None:
        (self.written if changed else self.unchanged)[folder] += 1


def _num(v, digits: int = 2) -> str:
    return "-" if v is None else f"{v:,.{digits}f}"


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _pct2(v) -> str:
    return "-" if v is None else f"{v * 100:.2f}%"


def _spct(v) -> str:
    return "-" if v is None else f"{v * 100:+.1f}%"


def _cell(v) -> str:
    """A user's frontmatter value inside a Markdown table cell."""
    return "-" if v is None or v == "" else str(v).replace("|", "\\|").replace("\n", " ")


def _pp(pair) -> str:
    """Change in percentage points between (this year, prior year) shares."""
    now, before = pair
    return "-" if now is None or before is None else f"{(now - before) * 100:+.2f}"


def _usd(v) -> str:
    if v is None:
        return "-"
    a = abs(v)
    return (f"${v / 1e9:,.2f}B" if a >= 1e9 else f"${v / 1e6:,.1f}M" if a >= 1e6
            else f"${v / 1e3:,.0f}k" if a >= 1e3 else f"${v:,.0f}")


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True, autoescape=False)
    env.filters.update(num=_num, pct=_pct, pct2=_pct2, pp=_pp, usd=_usd, spct=_spct, cell=_cell)
    return env


def _r(v, digits: int = 4):
    """Round floats for frontmatter: stable text, and Dataview sorts the numbers fine."""
    if v is None:
        return None
    return int(round(float(v))) if digits == 0 else round(float(v), digits)


def _index(folder: Path, *keys: str) -> dict:
    """Identity (frontmatter `keys`) -> note. A note whose frontmatter does not parse is left alone."""
    out: dict = {}
    for p in sorted(folder.glob("*.md")):
        try:
            meta = split_note(p.read_text(encoding="utf-8"))[0]
        except Exception:
            continue
        k = tuple(meta.get(x) for x in keys)
        if all(isinstance(v, (int, str)) for v in k):
            out.setdefault(k if len(k) > 1 else k[0], p)
    return out


def _note_path(index: dict, folder: Path, key, title: str, tag) -> Path:
    """The note for `key`: the one found by identity, else the first name no file and no other
    note of this sync uses: TITLE, TITLE (tag), TITLE (tag) 2, ..."""
    if key in index:
        return index[key]
    taken = {p.name.casefold() for p in index.values()}
    base = slug(title)
    for i in count(1):
        p = folder / (f"{base}.md" if i == 1 else f"{base} ({tag}).md" if i == 2 else f"{base} ({tag}) {i - 1}.md")
        if not p.exists() and p.name.casefold() not in taken:
            index[key] = p
            return p


def _edgar_url(cik: int, accn: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/"


def _targets(rows: list[dict], companies_idx: dict, journals: list[dict]) -> list[dict]:
    """Shortlisted and quarantined companies, plus every company with a note or a journal entry."""
    keep = set(companies_idx) | {j["cik"] for j in journals}
    return [r for r in rows if r["status"] in SYNCED or r["cik"] in keep]


def vault_ciks(con: duckdb.DuckDBPyConnection, vault: Path | None, as_of: date, filters: Filters = Filters(),
               policy: Policy = "as_last_known") -> list[int]:
    """The companies a sync on as_of would write: for fetching their 10-K text first."""
    rows = screen(con, as_of, filters, policy).to_arrow_table().to_pylist()
    idx = _index(vault / "Companies", "cik") if vault and (vault / "Companies").is_dir() else {}
    journals = read_journals(vault / "Journal", as_of) if vault else []
    return sorted(r["cik"] for r in _targets(rows, idx, journals))


def sync_vault(con: duckdb.DuckDBPyConnection, vault: Path, as_of: date, filters: Filters = Filters(),
               policy: Policy = "as_last_known") -> SyncStats:
    stats = SyncStats()
    env = _env()
    for f in FOLDERS:
        (vault / f).mkdir(parents=True, exist_ok=True)
    rows = screen(con, as_of, filters, policy).to_arrow_table().to_pylist()
    companies_idx = _index(vault / "Companies", "cik")
    journals = read_journals(vault / "Journal", as_of)
    targets = _targets(rows, companies_idx, journals)
    by_cik = {r["cik"]: r for r in targets}
    company_link = {cik: _note_path(companies_idx, vault / "Companies", cik, r["ticker"], cik).stem
                    for cik, r in sorted(by_cik.items())}

    # Insider purchases of these companies over the last year, oldest first, so a person's
    # first-seen name becomes their canonical one.
    buys = con.sql(f"""
        SELECT * FROM ({conviction_buys_sql(HISTORY_DAYS)})
        WHERE list_contains($ciks, issuer_cik) ORDER BY filed, accn, line_no
    """, params={"as_of": as_of, "ciks": sorted(by_cik)}).to_arrow_table().to_pylist()

    # M12-A1: officer and director sales over the same year (informational). Not linked to
    # Executives notes: those stay the buyers'; each sale links to its filing on EDGAR.
    sells = con.sql(f"""
        SELECT * FROM ({insider_sells_sql(HISTORY_DAYS)})
        WHERE list_contains($ciks, issuer_cik) ORDER BY txn_date DESC, filed DESC, accn, line_no
    """, params={"as_of": as_of, "ciks": sorted(by_cik)}).to_arrow_table().to_pylist()

    # M12-A2: red-flag 8-Ks inside each item's window, one row per filing (informational).
    events_8k: dict[int, list[dict]] = defaultdict(list)
    for accn, cik, form, filed, items in con.execute(f"""
            SELECT accn, cik, form, filed, list(item) FROM ({events_8k_sql()})
            WHERE list_contains($ciks, cik) GROUP BY ALL ORDER BY filed DESC, accn""",
            {"as_of": as_of, "ciks": sorted(by_cik)}).fetchall():
        events_8k[cik].append({"filed": filed, "form": form, "url": _edgar_url(cik, accn),
                               "labels": "; ".join(f"{i} {EVENT_ITEMS[i][1]}" for i in EVENT_ITEMS if i in items)})

    registry = PersonRegistry(con)
    people_idx = _index(vault / "Executives", "cik")
    canonical = {}
    for b in buys:
        canonical[b["owner_cik"]] = registry.register(b["owner_cik"], b["owner_name"], b["issuer_cik"])
    person_link = {cik: _note_path(people_idx, vault / "Executives", cik, name, cik).stem
                   for cik, name in sorted(canonical.items())}

    # Events: one per Form 4 filing and insider, all its purchase lots together.
    lots: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for b in buys:
        lots[(b["accn"], b["owner_cik"])].append(b)
    # Filings are found by accession too; generated links use each note's actual file name.
    filings_idx = _index(vault / "Filings", "accn")
    # M7: Risk Factors redline (10-K text from `fetch-text`) and forensics from facts.
    lists = load_lists(config.LM_DICT)
    redlines = risk_factor_redline(con, sorted(by_cik), as_of, lists)
    tenks = {a: cik for cik, rl in redlines.items() for a in (rl["accn"], rl["prior_accn"]) if a}
    # M10: related-party note of the same 10-Ks (informational: never feeds status or rank).
    related = related_party_redline(con, sorted(by_cik), as_of)
    rp_tenks = {a: cik for cik, x in related.items() for a in (x["accn"], x["prior_accn"]) if a}
    forensic = forensics(con, sorted(by_cik), as_of, policy)
    accns = ({a for r in by_cik.values() for s in r["sources"] or [] for a in s["accns"]} | {a for a, _ in lots}
             | set(tenks) | set(rp_tenks) | {a for f in forensic.values() for a in f["accns"]})
    filing_link = {a: _note_path(filings_idx, vault / "Filings", a, a, "filing").stem for a in sorted(accns)}

    events = {}
    events_idx = _index(vault / "Events", "accn", "owner_cik")      # found by identity, not file name
    for (accn, owner), ls in sorted(lots.items()):
        first = ls[0]
        shares, usd = sum(b["shares"] for b in ls), sum(b["usd"] for b in ls)
        ticker = by_cik[first["issuer_cik"]]["ticker"]
        e = {"accn": accn, "owner_cik": owner, "cik": first["issuer_cik"], "ticker": ticker,
             "txn_date": min(b["txn_date"] for b in ls), "filed": first["filed"], "shares": shares, "usd": usd,
             "price": usd / shares, "lots": len(ls), "person": person_link[owner],
             "company": company_link[first["issuer_cik"]], "filing": filing_link[accn],
             "role": first["officer_title"] or ("Director" if first["is_director"] else None)}
        # The accession is the unique part of the name: shorten the rest, never it.
        head = slug(f"{e['txn_date']} {ticker} buy by {e['person']}")[:95].rstrip()
        title = f"{head} ({accn})"
        path = _note_path(events_idx, vault / "Events", (accn, owner), title, owner)
        e["title"] = path.stem
        events[(accn, owner)] = e
        changed = upsert_note(path, {
            "type": "insider_buy", "date": e["txn_date"], "filed": e["filed"], "ticker": ticker, "cik": e["cik"],
            "company": f"[[{e['company']}]]", "insider": f"[[{e['person']}]]", "owner_cik": owner,
            "shares": _r(shares, 0), "usd": _r(usd, 0), "price": _r(e["price"]), "accn": accn,
            "tags": ["insider_buy"]}, env.get_template("event.md.j2").render(e=e))
        stats.record("Events", changed)

    # Executives
    for owner, link in sorted(person_link.items()):
        mine = [b for b in buys if b["owner_cik"] == owner]
        p = {"name": canonical[owner], "cik": owner,
             "companies": sorted({company_link[b["issuer_cik"]] for b in mine}),
             "buys": [{**b, "company": company_link[b["issuer_cik"]], "title": b["officer_title"],
                       "event": events[(b["accn"], owner)]["title"]} for b in reversed(mine)]}
        changed = upsert_note(people_idx[owner], {
            "type": "person", "cik": owner, "name": canonical[owner],
            "aliases": registry.aliases(canonical[owner]),
            "companies": [f"[[{c}]]" for c in p["companies"]], "tags": ["insider"]},
            env.get_template("executive.md.j2").render(p=p))
        stats.record("Executives", changed)

    # Filings: every accession a shown number or purchase came from.
    filings: dict[str, dict] = {}
    for cik, r in sorted(by_cik.items()):
        for s in r["sources"] or []:
            for a in s["accns"]:
                f = filings.setdefault(a, {"accn": a, "cik": cik, "used_for": [], "event": None, "form": None,
                                           "filed": None})
                if s["item"] not in f["used_for"]:
                    f["used_for"].append(s["item"])
    for cik, f in sorted(forensic.items()):
        for a in f["accns"]:
            filings.setdefault(a, {"accn": a, "cik": cik, "used_for": [], "event": None, "form": None, "filed": None})
            if "forensics" not in filings[a]["used_for"]:
                filings[a]["used_for"].append("forensics")
    for a, cik in sorted(tenks.items()):
        filings.setdefault(a, {"accn": a, "cik": cik, "used_for": [], "event": None, "form": None, "filed": None})
        filings[a]["used_for"].append("risk factors")
    for a, cik in sorted(rp_tenks.items()):
        filings.setdefault(a, {"accn": a, "cik": cik, "used_for": [], "event": None, "form": None, "filed": None})
        filings[a]["used_for"].append("related parties")
    for e in events.values():
        filings.setdefault(e["accn"], {"accn": e["accn"], "cik": e["cik"], "used_for": [], "event": e["title"],
                                       "form": "4", "filed": e["filed"]})
    xbrl = [a for a, f in filings.items() if f["form"] is None]
    if xbrl:
        for accn, form, filed in con.execute("""
                SELECT accn, min(form), min(filed) FROM xbrl_facts
                WHERE list_contains($ciks, cik) AND list_contains($accns, accn) GROUP BY accn""",
                {"ciks": sorted({filings[a]["cik"] for a in xbrl}), "accns": sorted(xbrl)}).fetchall():
            filings[accn].update(form=form, filed=filed)
    text_only = [a for a, f in filings.items() if f["form"] is None]   # 10-Ks known from their text
    if text_only:
        for accn, form, filed in con.execute(
                "SELECT DISTINCT accn, form, filed FROM filing_sections WHERE list_contains($accns, accn)",
                {"accns": sorted(text_only)}).fetchall():
            filings[accn].update(form=form, filed=filed)
    for accn, f in sorted(filings.items()):
        f.update(company=company_link[f["cik"]], url=_edgar_url(f["cik"], accn))
        changed = upsert_note(filings_idx[accn], {
            "type": "filing", "accn": accn, "cik": f["cik"], "ticker": by_cik[f["cik"]]["ticker"],
            "form": f["form"], "filed": f["filed"], "url": f["url"], "company": f"[[{f['company']}]]",
            "tags": ["filing"]}, env.get_template("filing.md.j2").render(f=f))
        stats.record("Filings", changed)

    # Companies
    for cik, r in sorted(by_cik.items()):
        mine = [{**b, "person": person_link[b["owner_cik"]], "event": events[(b["accn"], b["owner_cik"])]["title"]}
                for b in reversed(buys) if b["issuer_cik"] == cik]
        sold = [{**x, "label": SELL_LABELS[x["plan"]] + (", routine" if x["routine"] else ""),
                 "url": _edgar_url(cik, x["accn"])} for x in sells if x["issuer_cik"] == cik]
        rl, fx, rp = redlines.get(cik), forensic.get(cik), related.get(cik)
        fa = assess(fx, r) if fx else None
        changed = upsert_note(companies_idx[cik], {
            "type": "company", "ticker": r["ticker"], "cik": cik, "name": r["name"], "status": r["status"],
            "rank": r["rank"], "ev_ebit": _r(r["ev_ebit"]), "net_cash_to_mcap": _r(r["net_cash_to_mcap"]),
            "fcf_yield": _r(r["fcf_yield"]), "share_cagr_3y": _r(r["share_cagr_3y"]),
            "insider_buy_90d_usd": _r(r["insider_buy_90d_usd"], 0),
            "insider_sell_90d_usd": _r(r["insider_sell_90d_usd"], 0),
            "net_insider_flow_90d_usd": _r(r["net_insider_flow_90d_usd"], 0), "signals": list(r["signals"]),
            "short_interest_pct": _r(r["short_interest_pct"]), "days_to_cover": _r(r["days_to_cover"], 2),
            "inst_holders": r["inst_holders"], "inst_pct": _r(r["inst_pct"]), "inst_pct_note": r["inst_pct_note"],
            "drawdown_52w": _r(r["drawdown_52w"]),
            "mcap": _r(r["mcap"], 0), "as_of": as_of, "price_date": r["price_date"], "issues": list(r["issues"]), "flags": list(r["flags"]),
            "rf_new_sentences": len(rl["new"]) if rl and rl["status"] == "ok" else None,
            "related_party_new": len(rp["new"]) if rp and rp["compared"] else None,
            "forensic_flags": fa["flags"] if fa else [],
            "forensic_unknown": fa["unknown"] if fa else [],
            **{f"tone_{k}_change": _r(rl["tone"][k][0] - rl["tone"][k][1], 4)
               if rl and "tone" in rl and None not in rl["tone"][k] else None for k in TONE_KEYS},
            "aliases": [r["name"]] if r["name"] else [], "tags": ["company"]},
            env.get_template("company.md.j2").render(c={**r, "as_of": as_of}, buys=mine, sells=sold, events_8k=events_8k.get(cik, []),
                                                       window=filters.insider_window_days, filing_link=filing_link,
                                                       rl=rl, fx=fx, fa=fa, rp=rp, stale_days=STALE_FY_DAYS,
                                                       tone_installed=lists is not None,
                                                       new_shown=NEW_SHOWN,
                                                       removed_shown=REMOVED_SHOWN))
        stats.record("Companies", changed)

    # M12: one note per added data source (terms, access, known-at rule).
    for title, meta in SOURCES.items():
        stats.record("Sources", upsert_note(vault / "Sources" / f"{title}.md", {"type": "source", "tags": ["source"]},
                                            env.get_template("source.md.j2").render(title=title, s=meta)))

    changed = upsert_note(vault / "Dashboard.md", {"type": "dashboard"}, env.get_template("dashboard.md.j2").render(
        as_of=as_of, filters=json.dumps(asdict(filters), sort_keys=True), data=_data_dates(con, as_of),
        macro=macro_regime(con, as_of),
        decisions=_decisions(con, journals, {r["cik"]: r for r in rows}, as_of)))
    stats.record("Dashboard", changed)
    return stats


def _data_dates(con: duckdb.DuckDBPyConnection, as_of: date) -> dict:
    """The newest dated price, Form 4, XBRL fact and 8-K stored by as_of, across all companies.
    Not coverage: an import can have gaps, and a company's own inputs are in its note."""
    (px, f4, xf, e8, si, q13), = con.execute("""
        SELECT (SELECT max(price_date) FROM prices WHERE price_date <= $as_of),
               (SELECT max(filed) FROM form4_transactions WHERE filed <= $as_of),
               (SELECT max(filed) FROM xbrl_facts WHERE filed <= $as_of),
               (SELECT max(filed) FROM filings_8k WHERE filed <= $as_of),
               (SELECT max(settlement_date) FROM short_interest WHERE known_at <= $as_of),
               (SELECT max(period) FROM form13f_periods WHERE deadline <= $as_of)""",
                              {"as_of": as_of}).fetchall()
    return {"price": px, "form4": f4, "facts": xf, "form8k": e8, "short": si, "form13f": q13}


def _decisions(con: duckdb.DuckDBPyConnection, journals: list[dict], now: dict, as_of: date) -> list[dict]:
    """Each journal's frozen snapshot against the screen on as_of, newest decision first."""
    out = []
    for j, moved in zip(journals, since(con, journals, as_of)):
        review = to_date(j["meta"].get("review_date"))
        out.append({"title": j["path"].stem, "decided": j["decided"], "decision": j["meta"].get("decision"),
                    "review_date": review, "due": isinstance(review, date) and review <= as_of,
                    # The note is the user's: a key they deleted shows as "-".
                    "then": {k: j["snapshot"].get(k) for k in DECISION_KEYS}, "now": now.get(j["cik"]), **moved})
    return out[::-1]


def write_journal(con: duckdb.DuckDBPyConnection, vault: Path, ticker: str, as_of: date,
                  review_days: int = REVIEW_DAYS, filters: Filters = Filters(),
                  policy: Policy = "as_last_known") -> Path:
    """Create Journal/TICKER-as_of.md with the decision template and the screen row frozen in it.
    Never replaces a note (FileExistsError); a ticker outside the screen is a LookupError."""
    rows = screen(con, as_of, filters, policy).to_arrow_table().to_pylist()
    row = next((r for r in rows if (r["ticker"] or "").upper() == ticker.upper()), None)
    if row is None:
        raise LookupError(f"{ticker.upper()} is not in the screen on {as_of}: unknown ticker, "
                          "a financial or shell company, or no price")
    path = vault / "Journal" / f"{slug(row['ticker'])}-{as_of}.md"
    if path.exists():
        raise FileExistsError(path)
    companies = vault / "Companies"
    idx = _index(companies, "cik") if companies.is_dir() else {}
    j = {"ticker": row["ticker"], "decided": as_of, "review_date": as_of + timedelta(days=review_days),
         "company": _note_path(idx, companies, row["cik"], row["ticker"], row["cik"]).stem}
    snap = snapshot(row, filters, policy)
    create_note(path, {
        "type": "journal", "ticker": row["ticker"], "cik": row["cik"], "company": f"[[{j['company']}]]",
        "decided": as_of, "decision": None, "position_size": None, "review_date": j["review_date"],
        "thesis": "", "snapshot": snap, "tags": ["journal"]},
        _env().get_template("journal.md.j2").render(j=j, s=snap))
    return path
