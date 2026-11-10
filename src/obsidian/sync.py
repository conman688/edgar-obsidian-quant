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
from src.db.queries import conviction_buys_sql
from src.forensics import assess, forensics
from src.journal import REVIEW_DAYS, read_journals, since, snapshot, to_date
from src.obsidian.writer import PersonRegistry, create_note, slug, split_note, upsert_note
from src.redline import risk_factor_redline
from src.tone import load_lists
from src import config
from src.screen import Filters, screen, screen_table

FOLDERS = ("Companies", "Executives", "Filings", "Events", "Journal")
SYNCED = ("shortlist", "quarantine")     # statuses that get a note; existing notes are always updated
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
    rows = screen_table(con, as_of, filters, policy).to_pylist()
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
    forensic = forensics(con, sorted(by_cik), as_of, policy)
    accns = ({a for r in by_cik.values() for s in r["sources"] or [] for a in s["accns"]} | {a for a, _ in lots}
             | set(tenks) | {a for f in forensic.values() for a in f["accns"]})
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
        rl, fx = redlines.get(cik), forensic.get(cik)
        fa = assess(fx, r) if fx else None
        changed = upsert_note(companies_idx[cik], {
            "type": "company", "ticker": r["ticker"], "cik": cik, "name": r["name"], "status": r["status"],
            "rank": r["rank"], "ev_ebit": _r(r["ev_ebit"]), "net_cash_to_mcap": _r(r["net_cash_to_mcap"]),
            "fcf_yield": _r(r["fcf_yield"]), "share_cagr_3y": _r(r["share_cagr_3y"]),
            "insider_buy_90d_usd": _r(r["insider_buy_90d_usd"], 0), "drawdown_52w": _r(r["drawdown_52w"]),
            "mcap": _r(r["mcap"], 0), "as_of": as_of, "issues": list(r["issues"]), "flags": list(r["flags"]),
            "issue_checks": [f"{c['issue']}: {c['reason']}" for c in r["issue_checks"]],
            "rf_new_sentences": len(rl["new"]) if rl and rl["status"] == "ok" else None,
            "forensic_flags": fa["flags"] if fa else [],
            "forensic_unknown": fa["unknown"] if fa else [],
            **{f"tone_{k}_change": _r(rl["tone"][k][0] - rl["tone"][k][1], 4)
               if rl and "tone" in rl and None not in rl["tone"][k] else None for k in TONE_KEYS},
            "aliases": [r["name"]] if r["name"] else [], "tags": ["company"]},
            env.get_template("company.md.j2").render(c={**r, "as_of": as_of}, buys=mine, filing_link=filing_link,
                                                       rl=rl, fx=fx, fa=fa, tone_installed=lists is not None,
                                                       new_shown=NEW_SHOWN,
                                                       removed_shown=REMOVED_SHOWN))
        stats.record("Companies", changed)

    changed = upsert_note(vault / "Dashboard.md", {"type": "dashboard"}, env.get_template("dashboard.md.j2").render(
        as_of=as_of, filters=json.dumps(asdict(filters), sort_keys=True),
        decisions=_decisions(con, journals, {r["cik"]: r for r in rows}, as_of)))
    stats.record("Dashboard", changed)
    return stats


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
