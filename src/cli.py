"""Command line entry point.

    uv run --env-file .env python -m src.cli ingest all
    uv run python -m src.cli ingest facts --zip data/raw/companyfacts.zip
    uv run python -m src.cli facts AAPL revenue --as-of 2024-06-30
    uv run python -m src.cli stats
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Callable

from src import config
from src.db.pit import concept_sql
from src.db.schema import connect
from src.ingestion import companyfacts, prices, reference
from src.ingestion.sec import SecClient, sha256_file


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
              f"{f'; {stats.filings_grown:,} known filings gained facts' if stats.filings_grown else ''}")
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


def cmd_ingest(args) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = connect(str(config.DB_PATH))
    jobs = {"tickers": [ingest_tickers], "companies": [ingest_companies], "facts": [ingest_facts],
            "prices": [ingest_prices],
            "all": [ingest_tickers, ingest_companies, ingest_facts, ingest_prices]}[args.what]
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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="edgar-obsidian-quant")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="download and load SEC data")
    ing.add_argument("what", choices=["tickers", "companies", "facts", "prices", "all"])
    ing.add_argument("--zip", help="use an already-downloaded zip instead of fetching")
    ing.add_argument("--cik", type=int, action="append", help="load only these CIKs (repeatable)")
    ing.add_argument("--keep-raw", action="store_true", help="keep downloaded zips after loading")
    ing.add_argument("--ticker", action="append", help="prices: only these tickers (repeatable)")
    ing.add_argument("--include-otc", action="store_true", help="prices: also OTC-traded companies")
    ing.set_defaults(func=cmd_ingest)

    f = sub.add_parser("facts", help="show point-in-time facts for one company")
    f.add_argument("ident", help="ticker or CIK")
    f.add_argument("concept", help="concept from concept_map, e.g. revenue, diluted_shares")
    f.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    f.add_argument("--policy", choices=["as_last_known", "as_first_reported"], default="as_last_known")
    f.add_argument("--limit", type=int, default=12)
    f.set_defaults(func=cmd_facts)

    st = sub.add_parser("stats", help="row counts and health checks")
    st.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
