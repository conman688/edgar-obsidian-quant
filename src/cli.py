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

from src import config
from src.db.pit import concept_sql
from src.db.schema import connect
from src.ingestion import companyfacts, reference
from src.ingestion.sec import SecClient, sha256_file


def _client() -> SecClient:
    return SecClient(config.sec_user_agent())


def _fetch_zip(url: str, name: str, local: str | None) -> tuple[Path | None, str | None, bool]:
    """Return (path, sha256, delete_after). path is None when SEC reports no change."""
    if local:
        p = Path(local)
        return p, sha256_file(p), False
    meta = _client().download(url, config.RAW_DIR / name)
    if not meta["changed"]:
        print(f"{name}: not modified since last run")
        return None, meta.get("sha256"), False
    print(f"{name}: downloaded {meta['bytes'] / 1e6:,.0f} MB")
    return Path(meta["path"]), meta["sha256"], True


def _finish(path: Path, delete: bool, keep_raw: bool) -> None:
    # Disk is tight; only validators in *.meta.json are needed for the next conditional GET.
    if delete and not keep_raw:
        path.unlink(missing_ok=True)


def ingest_facts(con, args) -> None:
    path, sha, delete = _fetch_zip(config.COMPANYFACTS_ZIP_URL, "companyfacts.zip", args.zip)
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
              f"{stats.facts_inserted:,} inserted in {stats.seconds:,.0f}s")
    _finish(path, delete, args.keep_raw)


def ingest_companies(con, args) -> None:
    path, sha, delete = _fetch_zip(config.SUBMISSIONS_ZIP_URL, "submissions.zip", args.zip)
    if path is None:
        return
    n = reference.load_companies_zip(con, path, sha)
    print(f"companies: {n:,} profiles loaded" if n else "companies: this version is already loaded")
    _finish(path, delete, args.keep_raw)


def ingest_tickers(con, args) -> None:
    n = reference.load_tickers(con, _client().get(config.TICKERS_URL), date.today())
    print(f"tickers: {n:,}")


def cmd_ingest(args) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = connect(str(config.DB_PATH))
    jobs = {"tickers": [ingest_tickers], "companies": [ingest_companies], "facts": [ingest_facts],
            "all": [ingest_tickers, ingest_companies, ingest_facts]}[args.what]
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
    ing.add_argument("what", choices=["tickers", "companies", "facts", "all"])
    ing.add_argument("--zip", help="use an already-downloaded zip instead of fetching")
    ing.add_argument("--cik", type=int, action="append", help="load only these CIKs (repeatable)")
    ing.add_argument("--keep-raw", action="store_true", help="keep downloaded zips after loading")
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
