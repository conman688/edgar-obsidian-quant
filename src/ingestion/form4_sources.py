"""Where Form 4 rows come from.

Completed quarters: SEC's Insider Transactions Data Sets, one zip per quarter with
every filer, read straight into DuckDB. SEC publishes them more than a quarter
late, so recent quarters come from the quarterly form index instead: each listed
filing's XML is fetched and parsed, for issuers in the price universe only.

form4_filings is the per-accession ledger both paths share: a filing loaded from
XML is skipped when its quarter's data set appears, and the other way round.
Both paths produce the same rows for the same filing (see tests/test_form4_sources.py).
"""
from __future__ import annotations

import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import duckdb

from src.db import ledger
from src.ingestion.form4_insider import COLUMNS, parse_form4

SCHEMA_VERSION = 1
DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{n}/form.idx"
FILING_URL = "https://www.sec.gov/Archives/{path}"
DATASET_FILES = ("SUBMISSION.tsv", "REPORTINGOWNER.tsv", "NONDERIV_TRANS.tsv", "NONDERIV_HOLDING.tsv",
                 "FOOTNOTES.tsv")
_OWNERSHIP_DOC = re.compile(rb"<ownershipDocument>.*?</ownershipDocument>", re.S)
_FN_TRANS = ("SECURITY_TITLE_FN", "TRANS_DATE_FN", "DEEMED_EXECUTION_DATE_FN", "EQUITY_SWAP_TRANS_CD_FN",
             "TRANS_TIMELINESS_FN", "TRANS_SHARES_FN", "TRANS_PRICEPERSHARE_FN", "TRANS_ACQUIRED_DISP_CD_FN",
             "SHRS_OWND_FOLWNG_TRANS_FN", "VALU_OWND_FOLWNG_TRANS_FN", "DIRECT_INDIRECT_OWNERSHIP_FN",
             "NATURE_OF_OWNERSHIP_FN")
_FN_HOLD = ("SECURITY_TITLE_FN", "TRANS_FORM_TYPE_FN", "SHRS_OWND_FOLWNG_TRANS_FN", "VALU_OWND_FOLWNG_TRANS_FN",
            "DIRECT_INDIRECT_OWNERSHIP_FN", "NATURE_OF_OWNERSHIP_FN")


def quarter_of(d: date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def quarters(since: str, until: str) -> list[str]:
    y, q = int(since[:4]), int(since[-1])
    out = []
    while f"{y}q{q}" <= until:
        out.append(f"{y}q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def published_quarters(page_html: str) -> dict[str, str]:
    """Quarter -> download URL, from the links on SEC's data set page. The folder has
    moved before (2026q2 onward sit under a different path), so never build the URL."""
    return {q: "https://www.sec.gov" + href if href.startswith("/") else href
            for href, q in re.findall(r'href="([^"]*?(\d{4}q[1-4])_form345\.zip)"', page_html)}


# ---- completed quarters: bulk data sets ------------------------------------------

def _tsv(path: Path) -> str:
    return f"read_csv('{path}', delim='\t', header=true, all_varchar=true, quote='', escape='')"


def _footnotes_sql(rows: str, fn_cols: tuple[str, ...]) -> str:
    """Footnote text per row, ids de-duplicated and sorted like the XML path."""
    ids = f"concat_ws(',', {', '.join(fn_cols)})"
    return f"""
        SELECT r.accn, r.line_no, string_agg(f.txt, ' | ' ORDER BY f.fid) AS notes
        FROM (SELECT DISTINCT accn, line_no, trim(unnest(string_split({ids}, ','))) AS fid FROM {rows}) r
        JOIN ds_fn f ON f.accn = r.accn AND f.fid = r.fid
        GROUP BY r.accn, r.line_no
    """


def _row_sql(rows: str, notes: str, holding: bool) -> str:
    txn = "NULL" if holding else "try_strptime(t.TRANS_DATE, '%d-%b-%Y')::DATE"
    code = "NULL" if holding else "t.TRANS_CODE"
    acq = "NULL" if holding else "t.TRANS_ACQUIRED_DISP_CD"
    shares = "NULL" if holding else "TRY_CAST(t.TRANS_SHARES AS DOUBLE)"
    price = "NULL" if holding else "TRY_CAST(t.TRANS_PRICEPERSHARE AS DOUBLE)"
    return f"""
        INSERT INTO form4_transactions ({', '.join(COLUMNS)})
        SELECT t.accn, t.line_no, FALSE, {str(holding).upper()}, s.issuer_cik, o.owner_cik, o.owner_name,
               o.rel LIKE '%Director%', o.rel LIKE '%Officer%', o.rel LIKE '%TenPercentOwner%', o.title,
               s.filed, {txn}, t.SECURITY_TITLE, {code}, {acq}, {shares}, {price},
               TRY_CAST(t.SHRS_OWND_FOLWNG_TRANS AS DOUBLE), t.DIRECT_INDIRECT_OWNERSHIP,
               lower(trim(coalesce(t.NATURE_OF_OWNERSHIP, ''))),
               CASE WHEN regexp_matches(coalesce(n.notes, ''), '(?i)10b5-?1') THEN TRUE ELSE s.aff10b5 END,
               n.notes, s.form_type, s.period_of_report
        FROM {rows} t
        JOIN ds_sub s ON s.accn = t.accn
        JOIN ds_owner o ON o.accn = t.accn
        LEFT JOIN ({notes}) n ON n.accn = t.accn AND n.line_no = t.line_no
    """


def _dataset_key(quarter: str, sha256: str) -> str:
    return f"form345:{quarter}@{sha256[:16]}"


def dataset_loaded(con: duckdb.DuckDBPyConnection, quarter: str, sha256: str | None) -> bool:
    return bool(sha256) and ledger.is_done(con, _dataset_key(quarter, sha256), "form4_dataset", SCHEMA_VERSION)


def load_dataset(con: duckdb.DuckDBPyConnection, zip_path: Path, quarter: str, sha256: str,
                 workdir: Path) -> int | None:
    """Load one quarter's Form 4 and 4/A filings. Returns rows inserted, or None if
    this exact file was already loaded. Filings already in form4_filings are skipped."""
    key = _dataset_key(quarter, sha256)
    if dataset_loaded(con, quarter, sha256):
        return None
    d = workdir / quarter
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for name in DATASET_FILES:
            z.extract(name, d)
    before = con.execute("SELECT count(*) FROM form4_transactions").fetchall()[0][0]
    con.begin()
    try:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ds_sub AS
            SELECT ACCESSION_NUMBER AS accn, strptime(FILING_DATE, '%d-%b-%Y')::DATE AS filed,
                   try_strptime(PERIOD_OF_REPORT, '%d-%b-%Y')::DATE AS period_of_report,
                   DOCUMENT_TYPE AS form_type, TRY_CAST(ISSUERCIK AS INTEGER) AS issuer_cik,
                   CASE WHEN lower(AFF10B5ONE) IN ('1', 'true') THEN TRUE
                        WHEN lower(AFF10B5ONE) IN ('0', 'false') THEN FALSE END AS aff10b5
            FROM {_tsv(d / 'SUBMISSION.tsv')}
            WHERE DOCUMENT_TYPE IN ('4', '4/A')
              AND ACCESSION_NUMBER NOT IN (SELECT accn FROM form4_filings WHERE status IN ('done', 'skipped'))
        """)
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ds_owner AS
            SELECT ACCESSION_NUMBER AS accn, TRY_CAST(RPTOWNERCIK AS INTEGER) AS owner_cik,
                   coalesce(RPTOWNERNAME, '') AS owner_name, coalesce(RPTOWNER_RELATIONSHIP, '') AS rel,
                   nullif(trim(RPTOWNER_TITLE), '') AS title
            FROM {_tsv(d / 'REPORTINGOWNER.tsv')}
            QUALIFY row_number() OVER (PARTITION BY ACCESSION_NUMBER ORDER BY TRY_CAST(RPTOWNERCIK AS INTEGER)) = 1
        """)
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ds_fn AS
            SELECT ACCESSION_NUMBER AS accn, FOOTNOTE_ID AS fid,
                   regexp_replace(trim(FOOTNOTE_TXT), '\\s+', ' ', 'g') AS txt
            FROM {_tsv(d / 'FOOTNOTES.tsv')}
        """)
        for table, sk, fn_cols, holding in (("NONDERIV_TRANS.tsv", "NONDERIV_TRANS_SK", _FN_TRANS, False),
                                            ("NONDERIV_HOLDING.tsv", "NONDERIV_HOLDING_SK", _FN_HOLD, True)):
            con.execute(f"""
                CREATE OR REPLACE TEMP TABLE ds_rows AS
                SELECT *, ACCESSION_NUMBER AS accn,
                       row_number() OVER (PARTITION BY ACCESSION_NUMBER ORDER BY TRY_CAST({sk} AS BIGINT)) - 1
                           AS line_no
                FROM {_tsv(d / table)}
                WHERE ACCESSION_NUMBER IN (SELECT accn FROM ds_sub)
            """)
            con.execute(_row_sql("ds_rows", _footnotes_sql("ds_rows", fn_cols), holding))
        con.execute("""
            INSERT OR REPLACE INTO form4_filings
            SELECT accn, issuer_cik, form_type, filed, ?, 'done', NULL, current_timestamp FROM ds_sub
        """, [f"dataset:{quarter}"])
        ledger.mark(con, key, "form4_dataset", SCHEMA_VERSION, "done", sha256)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        for name in DATASET_FILES:
            (d / name).unlink(missing_ok=True)
    return con.execute("SELECT count(*) FROM form4_transactions").fetchall()[0][0] - before


# ---- recent quarters: form index + per-filing XML ---------------------------------

def parse_form_index(text: str) -> list[tuple[str, int, str, date, str]]:
    """(accn, filer_cik, form_type, filed, path) for Form 4 and 4/A lines of a form.idx."""
    out = []
    for line in text.splitlines():
        if not (line.startswith("4 ") or line.startswith("4/A ")):
            continue
        head, cik, filed, path = line.rsplit(None, 3)
        accn = path.rsplit("/", 1)[1].removesuffix(".txt")
        out.append((accn, int(cik), head.split()[0], date.fromisoformat(filed), path))
    return out


def load_index(con: duckdb.DuckDBPyConnection, quarter: str, text: str) -> int:
    rows = [(quarter, *r) for r in parse_form_index(text)]
    con.begin()
    try:
        con.execute("DELETE FROM form4_index WHERE quarter = ?", [quarter])
        con.executemany("INSERT OR IGNORE INTO form4_index VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return len(rows)


@dataclass
class FetchStats:
    pending: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    rows: int = 0
    seconds: float = 0.0


def pending_filings(con: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[tuple[str, str, date]]:
    """Indexed filings by issuers in the price universe that no path has loaded yet."""
    return con.execute(f"""
        SELECT i.accn, min(i.path), min(i.filed) FROM form4_index i
        WHERE i.filer_cik IN (SELECT cik FROM price_status)
          AND i.accn NOT IN (SELECT accn FROM form4_filings WHERE status IN ('done', 'skipped'))
        GROUP BY i.accn ORDER BY 3, 1 {f'LIMIT {int(limit)}' if limit else ''}
    """).fetchall()


def fetch_filings(con: duckdb.DuckDBPyConnection, get: Callable[[str], bytes], limit: int | None = None,
                  batch_size: int = 50, progress: Callable[[FetchStats], None] | None = None) -> FetchStats:
    """Fetch and store pending filings. Each batch commits on its own, so a stopped
    run resumes where it left off; failures are retried on the next run."""
    t0, stats = time.monotonic(), FetchStats()
    todo = pending_filings(con, limit)
    stats.pending = len(todo)
    for i in range(0, len(todo), batch_size):
        rows, ledger_rows = [], []
        for accn, path, filed in todo[i:i + batch_size]:
            now = datetime.now()
            try:
                m = _OWNERSHIP_DOC.search(get(FILING_URL.format(path=path)))
                if not m:
                    ledger_rows.append((accn, None, None, filed, "xml", "skipped", "no ownership XML", now))
                    stats.skipped += 1
                    continue
                parsed = parse_form4(m.group(0), accn, filed)
                rows += parsed
                issuer = parsed[0]["issuer_cik"] if parsed else None
                form = parsed[0]["form_type"] if parsed else None
                ledger_rows.append((accn, issuer, form, filed, "xml", "done", None, now))
                stats.done += 1
            except Exception as e:
                ledger_rows.append((accn, None, None, filed, "xml", "failed", repr(e)[:300], now))
                stats.failed += 1
        con.begin()
        try:
            if rows:
                ph = ",".join("?" * len(COLUMNS))
                con.executemany(f"INSERT INTO form4_transactions ({','.join(COLUMNS)}) VALUES ({ph}) "
                                "ON CONFLICT DO NOTHING", [[r[c] for c in COLUMNS] for r in rows])
            con.executemany("INSERT OR REPLACE INTO form4_filings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ledger_rows)
            con.commit()
        except BaseException:
            con.rollback()
            raise
        stats.rows += len(rows)
        if progress:
            progress(stats)
    stats.seconds = time.monotonic() - t0
    return stats
