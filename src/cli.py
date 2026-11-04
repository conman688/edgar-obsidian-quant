"""Command line entry point.

    uv run --env-file .env python -m src.cli ingest all
    uv run python -m src.cli ingest facts --zip data/raw/companyfacts.zip
    uv run python -m src.cli facts AAPL revenue --as-of 2024-06-30
    uv run python -m src.cli stats
    uv run python -m src.cli screen --as-of 2026-10-30
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path
from typing import Callable

from src import config
from src.db.pit import concept_sql
from src.db.schema import connect
from src.ingestion import companyfacts, form4_sources, prices, reference
from src.ingestion.sec import SecClient, sha256_file
from src.screen import Filters, screen, write_parquet


def _client() -> SecClient:
    return SecClient(config.sec_user_agent())


def _fetch_zip(url: str, name: str, local: str | None,
               is_loaded: Callable[[str | None], bool]) -> tuple[Path | None, str | None, bool]:
    """Return (path, sha256, delete_after). path is None only when this version is fully loaded.

    A 304 says the file is unchanged, not that it was loaded: a crashed or filtered
    run also leaves the validators behind. So a 304 skips only when the ledger agrees.
    """
    if local:
        p = Path(local)
        return p, sha256_file(p), False
    client, dest = _client(), config.RAW_DIR / name
    meta = client.download(url, dest)
    if not meta["changed"]:
        if is_loaded(meta.get("sha256")):
            print(f"{name}: not modified since last run")
            return None, meta.get("sha256"), False
        if dest.exists() and sha256_file(dest) == meta.get("sha256"):
            print(f"{name}: not modified; finishing load from local copy")
            return dest, meta["sha256"], True
        print(f"{name}: not modified but never fully loaded; downloading again")
        meta = client.download(url, dest, force=True)
    print(f"{name}: downloaded {meta['bytes'] / 1e6:,.0f} MB")
    return Path(meta["path"]), meta["sha256"], True


def _finish(path: Path, delete: bool, keep_raw: bool) -> None:
    # Disk is tight; only validators in *.meta.json are needed for the next conditional GET.
    if delete and not keep_raw:
        path.unlink(missing_ok=True)


def ingest_facts(con, args) -> None:
    path, sha, delete = _fetch_zip(config.COMPANYFACTS_ZIP_URL, "companyfacts.zip", args.zip,
                                   lambda sha: companyfacts.is_loaded(con, sha))
    if path is None:
        return
    ciks = set(args.cik) if args.cik else None

    def progress(s: companyfacts.LoadStats) -> None:
        print(f"  {s.files:,} companies, {s.facts_inserted:,} facts inserted", flush=True)

    stats = companyfacts.load_zip(con, path, sha, ciks, progress=progress)
    if stats.skipped:
        print("companyfacts: this version is already loaded")
    else:
        print(f"companyfacts: {stats.files:,} companies, {stats.facts_seen:,} facts read, "
              f"{stats.facts_inserted:,} inserted in {stats.seconds:,.0f}s"
              f"{f'; {stats.filings_changed:,} known filings changed and were replaced' if stats.filings_changed else ''}")
    # A --cik load is partial: keep the zip so the full load needs no second download.
    _finish(path, delete and not ciks, args.keep_raw)


def ingest_companies(con, args) -> None:
    path, sha, delete = _fetch_zip(config.SUBMISSIONS_ZIP_URL, "submissions.zip", args.zip,
                                   lambda sha: reference.is_loaded(con, sha))
    if path is None:
        return
    n = reference.load_companies_zip(con, path, sha)
    print(f"companies: {n:,} profiles loaded" if n else "companies: this version is already loaded")
    _finish(path, delete, args.keep_raw)


def ingest_tickers(con, args) -> None:
    n = reference.load_tickers(con, _client().get(config.TICKERS_URL), date.today())
    print(f"tickers: {n:,}")


def ingest_prices(con, args) -> None:
    exchanges = prices.EXCHANGES + (("OTC",) if args.include_otc else ())

    def progress(s: prices.PriceStats) -> None:
        print(f"  {s.priced + s.no_data + s.errors:,}/{s.companies:,} companies, "
              f"{s.price_rows:,} rows", flush=True)

    s = prices.load(con, exchanges, args.ticker, progress=progress)
    print(f"prices: {s.priced:,} of {s.companies:,} companies priced, {s.price_rows:,} rows, "
          f"{s.splits:,} splits, {s.no_data:,} with no data, {s.errors:,} errors in {s.seconds:,.0f}s")


def ingest_form4(con, args) -> None:
    """Bulk data sets for quarters SEC has published, per-filing XML for the rest."""
    today = date.today()
    since = args.since or form4_sources.quarter_of(date(today.year - 2, today.month, 1))
    wanted = form4_sources.quarters(since, form4_sources.quarter_of(today))
    client = _client()
    published = form4_sources.published_quarters(client.get(form4_sources.DATASETS_PAGE).decode("utf-8", "replace"))
    for q in wanted:
        if q in published:
            path, sha, delete = _fetch_zip(published[q], f"{q}_form345.zip", None,
                                           lambda sha, q=q: form4_sources.dataset_loaded(con, q, sha))
            if path is None:
                continue
            n = form4_sources.load_dataset(con, path, q, sha, config.RAW_DIR / "form345")
            print(f"form4 {q}: data set, {n or 0:,} rows")
            _finish(path, delete, args.keep_raw)
        else:
            year, n = q[:4], q[-1]
            text = client.get(form4_sources.INDEX_URL.format(year=year, n=n)).decode("latin-1")
            print(f"form4 {q}: index, {form4_sources.load_index(con, q, text):,} Form 4 lines")

    def progress(s: form4_sources.FetchStats) -> None:
        print(f"  {s.done + s.skipped + s.failed:,}/{s.pending:,} filings, {s.rows:,} rows", flush=True)

    s = form4_sources.fetch_filings(con, client.get, args.xml_limit, progress=progress)
    print(f"form4 xml: {s.done:,} filings loaded, {s.skipped:,} without XML, {s.failed:,} failed, "
          f"{s.rows:,} rows in {s.seconds:,.0f}s")


def cmd_ingest(args) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = connect(str(config.DB_PATH))
    jobs = {"tickers": [ingest_tickers], "companies": [ingest_companies], "facts": [ingest_facts],
            "prices": [ingest_prices], "form4": [ingest_form4],
            "all": [ingest_tickers, ingest_companies, ingest_facts, ingest_prices, ingest_form4]}[args.what]
    if args.zip and args.what == "all":
        sys.exit("--zip works with a single source, not 'all'")
    for job in jobs:
        job(con, args)


def _resolve_cik(con, ident: str) -> int:
    if ident.isdigit():
        return int(ident)
    row = next(iter(con.execute("SELECT cik FROM tickers WHERE ticker = ?", [ident.upper()]).fetchall()), None)
    if row is None:
        sys.exit(f"unknown ticker {ident}; run 'ingest tickers' first or pass a CIK")
    return row[0]


def cmd_facts(args) -> None:
    con = connect(str(config.DB_PATH))
    cik = _resolve_cik(con, args.ident)
    rows = con.sql(
        f"SELECT period_kind, period_start, period_end, value, tag, form, filed, accn "
        f"FROM ({concept_sql(args.policy)}) WHERE cik = $cik "
        f"ORDER BY period_end DESC, period_kind LIMIT $limit",
        params={"as_of": args.as_of, "concept": args.concept, "cik": cik, "limit": args.limit},
    ).fetchall()
    if not rows:
        print("no facts")
    for kind, start, end, value, tag, form, filed, accn in rows:
        print(f"{kind:<10} {start} {end} {value:>20,.0f}  {form:<7} filed {filed}  {tag}  {accn}")


def cmd_stats(args) -> None:
    con = connect(str(config.DB_PATH))
    for label, sql in [
        ("facts", "SELECT count(*) FROM xbrl_facts"),
        ("companies with facts", "SELECT count(DISTINCT cik) FROM xbrl_facts"),
        ("filings", "SELECT count(*) FROM loaded_filings"),
        ("latest filed", "SELECT max(filed) FROM xbrl_facts"),
        ("tickers", "SELECT count(*) FROM tickers"),
        ("company profiles", "SELECT count(*) FROM companies"),
        ("financials (SIC 6000-6799)", "SELECT count(*) FROM companies WHERE is_financial"),
        ("price rows", "SELECT count(*) FROM prices"),
        ("companies priced", "SELECT count(*) FROM price_status WHERE status = 'ok'"),
        ("tickers with no price data", "SELECT count(*) FROM price_status WHERE status = 'no_data'"),
        ("latest price", "SELECT max(price_date) FROM prices"),
        ("splits", "SELECT count(*) FROM splits"),
        ("form 4 rows", "SELECT count(*) FROM form4_transactions"),
        ("form 4 filings (data set / xml)", """SELECT count(*) FILTER (WHERE source LIKE 'dataset:%') || ' / '
                                               || count(*) FILTER (WHERE source = 'xml') FROM form4_filings
                                               WHERE status = 'done'"""),
        ("form 4 filings pending or failed", """SELECT (SELECT count(DISTINCT accn) FROM form4_index
                                                        WHERE filer_cik IN (SELECT cik FROM price_status))
                                                - (SELECT count(*) FROM form4_filings f WHERE f.status IN ('done', 'skipped')
                                                   AND f.accn IN (SELECT accn FROM form4_index))"""),
        ("latest form 4 filed", "SELECT max(filed) FROM form4_transactions"),
        # Survivorship gap: filers with a 10-K since 2012 but no current ticker are
        # mostly delisted, and free price sources drop their history.
        ("10-K filers with no ticker", """SELECT count(DISTINCT cik) FROM xbrl_facts
                                          WHERE form = '10-K' AND filed >= DATE '2012-01-01'
                                            AND cik NOT IN (SELECT cik FROM tickers)"""),
        # Group count, not row count, sets memory: ~1M filings is cheap, 100M fact tuples is not.
        ("filings missing from ledger", """SELECT count(*) FROM (SELECT DISTINCT cik, accn FROM xbrl_facts) f
                                           ANTI JOIN loaded_filings l USING (cik, accn)"""),
    ]:
        print(f"{label:<28} {con.execute(sql).fetchall()[0][0]}")
    if config.DB_PATH.exists():
        print(f"{'db size':<28} {config.DB_PATH.stat().st_size / 1e9:.2f} GB")


def _num(v, fmt: str) -> str:
    return "-" if v is None else format(v, fmt)


def _usd(v) -> str:
    if v is None:
        return "-"
    return f"{v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"{v / 1e6:,.1f}M" if abs(v) >= 1e6 else f"{v / 1e3:,.0f}k"


def cmd_screen(args) -> None:
    con = connect(str(config.DB_PATH))
    filters = Filters(args.min_drawdown, args.max_ev_ebit, args.max_net_debt, args.insider_days)
    t0 = time.perf_counter()
    table = screen(con, args.as_of, filters, args.policy).to_arrow_table()
    seconds = time.perf_counter() - t0
    path = write_parquet(table, args.as_of, filters, Path(args.out), args.policy)
    rows = table.to_pylist()
    by = {k: [r for r in rows if r["status"] == k] for k in ("shortlist", "quarantine", "fail")}

    print(f"Screen as of {args.as_of} ({args.policy}): {len(rows):,} companies, "
          f"{len(by['shortlist'])} shortlisted, {len(by['quarantine'])} quarantined, "
          f"{len(by['fail']):,} failed, in {seconds:.1f}s")
    print(f"Filters: drawdown > {filters.min_drawdown:.0%}, FCF > 0, EV/EBIT < {filters.max_ev_ebit:g}, "
          f"net debt <= {filters.max_net_debt_to_mcap:.0%} of mcap, insider buying in {filters.insider_window_days} days\n")
    head = f"{'#':>3} {'ticker':<7} {'name':<26} {'EV/EBIT':>7} {'drawdn':>6} {'FCF yld':>7} {'netcash':>7} {'mcap':>7} {'buyers':>6} {'bought':>7}  flags"
    print("SHORTLIST\n" + head)
    for r in by["shortlist"]:
        print(f"{r['rank']:>3} {r['ticker']:<7} {(r['name'] or '')[:26]:<26} {_num(r['ev_ebit'], '7.1f')} "
              f"{_num(r['drawdown_52w'], '6.0%')} {_num(r['fcf_yield'], '7.0%')} {_num(r['net_cash_to_mcap'], '7.0%')} "
              f"{_usd(r['mcap']):>7} {r['n_insiders']:>6} {_usd(r['insider_buy_90d_usd']):>7}  {','.join(r['flags'])}")
        if args.trace:
            for src in r["sources"]:
                print(f"      {src['item']:<14} {src['period_end']}  {src['tag']}  {' '.join(src['accns'])}")
            print(f"      {'form 4':<14} {' '.join(r['form4_accns'])}")
    q = by["quarantine"]
    print(f"\nQUARANTINE (not ranked: data missing or suspect; {len(q)} names, showing {min(len(q), args.limit)})")
    print(f"    {'ticker':<7} {'name':<26} {'EV/EBIT':>7} {'drawdn':>6} {'bought':>7}  issues")
    for r in q[:args.limit]:
        print(f"    {r['ticker']:<7} {(r['name'] or '')[:26]:<26} {_num(r['ev_ebit'], '7.1f')} "
              f"{_num(r['drawdown_52w'], '6.0%')} {_usd(r['insider_buy_90d_usd']):>7}  {','.join(r['issues'] + r['flags'])}")
    print(f"\nwrote {path}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="edgar-obsidian-quant")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="download and load SEC data")
    ing.add_argument("what", choices=["tickers", "companies", "facts", "prices", "form4", "all"])
    ing.add_argument("--zip", help="use an already-downloaded zip instead of fetching")
    ing.add_argument("--cik", type=int, action="append", help="load only these CIKs (repeatable)")
    ing.add_argument("--keep-raw", action="store_true", help="keep downloaded zips after loading")
    ing.add_argument("--ticker", action="append", help="prices: only these tickers (repeatable)")
    ing.add_argument("--include-otc", action="store_true", help="prices: also OTC-traded companies")
    ing.add_argument("--since", help="form4: first quarter to load, e.g. 2024q4 (default: 2 years back)")
    ing.add_argument("--xml-limit", type=int, help="form4: fetch at most this many filings as XML")
    ing.set_defaults(func=cmd_ingest)

    f = sub.add_parser("facts", help="show point-in-time facts for one company")
    f.add_argument("ident", help="ticker or CIK")
    f.add_argument("concept", help="concept from concept_map, e.g. revenue, diluted_shares")
    f.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    f.add_argument("--policy", choices=["as_last_known", "as_first_reported"], default="as_last_known")
    f.add_argument("--limit", type=int, default=12)
    f.set_defaults(func=cmd_facts)

    sc = sub.add_parser("screen", help="rank cheap, out-of-favour companies with insider buying")
    sc.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    sc.add_argument("--policy", choices=["as_last_known", "as_first_reported"], default="as_last_known")
    d = Filters()
    sc.add_argument("--min-drawdown", type=float, default=d.min_drawdown, help="fraction below 52-week high")
    sc.add_argument("--max-ev-ebit", type=float, default=d.max_ev_ebit)
    sc.add_argument("--max-net-debt", type=float, default=d.max_net_debt_to_mcap, help="net debt / market cap")
    sc.add_argument("--insider-days", type=int, default=d.insider_window_days)
    sc.add_argument("--out", default=str(config.DATA_DIR / "screens"), help="folder for the dated Parquet snapshot")
    sc.add_argument("--trace", action="store_true", help="print the filings behind each shortlisted number")
    sc.add_argument("--limit", type=int, default=30, help="quarantined names to print")
    sc.set_defaults(func=cmd_screen)

    st = sub.add_parser("stats", help="row counts and health checks")
    st.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
