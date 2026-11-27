"""M12-B6: 13F institutional holdings per quarter and CUSIP, from SEC's Form 13F data sets.

Sources (checked 2026-11-27, SEC hosts, project User-Agent):
- sec.gov/data-research/sec-markets-data/form-13f-data-sets lists one zip per filing window:
  2013q2..2023q4 (calendar quarters of filing date), 01jan2024-29feb2024, then three months
  ending Feb, May, Aug and Nov. Each zip holds tab-separated SUBMISSION, COVERPAGE and INFOTABLE
  tables (no quoting, dates DD-MON-YYYY), with the same columns from 2013 to 2026.
- sec.gov/data-research/sec-markets-data/fails-deliver-data lists the fails-to-deliver files
  (cnsfailsYYYYMM[ab].zip, from 2009-07): "SETTLEMENT DATE|CUSIP|SYMBOL|...", the only SEC file
  that pairs a CUSIP with the ticker it trades under.

Known-at: a manager reports a quarter's holdings within 45 days of its end (the next business
day when that is a weekend or holiday). A quarter here counts only the reports filed by that
deadline, so its numbers are fixed and known from the deadline. Late reports and amendments
filed after it are left out and counted. Every report filed by the deadline is in one zip:
the window holding the deadline.

Per manager and quarter, by the deadline: the newest original or RESTATEMENT amendment stands
(two originals are never summed), and NEW HOLDINGS amendments filed after it add lines. Only SH
lines without PUT/CALL count, and shares rather than value (value was in thousands before
2023-01-03). 13F covers long positions in 13(f) securities of managers with $100M or more;
shorts, small or confidential positions are absent, so no position reported is never "not held".
"""
from __future__ import annotations

import calendar
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import pyarrow as pa
import requests

from src.db import ledger
from src.db.queries import cusip_cik_sql  # noqa: F401  (the CUSIP map lives with the screen SQL)

STAGE, FTD_STAGE, SCHEMA_VERSION = "form13f", "ftd_cusips", 1
SEC = "https://www.sec.gov"
DATASETS_PAGE = SEC + "/data-research/sec-markets-data/form-13f-data-sets"
FTD_PAGE = SEC + "/data-research/sec-markets-data/fails-deliver-data"
SUB_COLS = ["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"]
INFO_COLS = ["ACCESSION_NUMBER", "INFOTABLE_SK", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "FIGI", "VALUE",
             "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL", "INVESTMENTDISCRETION", "OTHERMANAGER",
             "VOTING_AUTH_SOLE", "VOTING_AUTH_SHARED", "VOTING_AUTH_NONE"]
MON = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
ACCN = re.compile(r"\d{10}-\d{2}-\d{6}")


class Form13fError(RuntimeError):
    pass


# ---- dates ----------------------------------------------------------------------------------

def _presidents_day(year: int) -> date:
    d = date(year, 2, 15)                               # the third Monday falls on Feb 15-21
    return d + timedelta(days=(0 - d.weekday()) % 7)


def deadline(period: date) -> date:
    """Quarter end + 45 days, rolled to the next business day. Presidents Day is the only
    federal holiday a rolled deadline can meet (Feb 14 on a weekend)."""
    d = period + timedelta(days=45)
    while d.weekday() >= 5 or d == _presidents_day(d.year):
        d += timedelta(days=1)
    return d


def _dmy(s: str) -> date:
    d, m, y = s.strip().split("-")
    return date(int(y), MON[m.upper()], int(d))


def window_dates(name: str) -> tuple[date, date]:
    """First and last filing date a data-set zip covers, from its file name."""
    if m := re.fullmatch(r"(\d{4})q([1-4])_form13f\.zip", name):
        y, q = int(m[1]), int(m[2])
        return date(y, 3 * q - 2, 1), date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
    if m := re.fullmatch(r"(\d{2})([a-z]{3})(\d{4})-(\d{2})([a-z]{3})(\d{4})_form13f\.zip", name):
        try:
            return (date(int(m[3]), MON[m[2].upper()], int(m[1])), date(int(m[6]), MON[m[5].upper()], int(m[4])))
        except (KeyError, ValueError):
            pass
    raise Form13fError(f"unknown 13F window {name!r}")


def _quarter_end(y: int, q: int) -> date:
    return date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])


def _prev_quarter(period: date) -> date:
    q = (period.month - 1) // 3 + 1
    return _quarter_end(period.year - 1, 4) if q == 1 else _quarter_end(period.year, q - 1)


def home_period(start: date, end: date) -> date:
    """The one quarter whose deadline falls inside the window."""
    homes = [_quarter_end(y, q) for y in range(start.year - 1, end.year + 1) for q in range(1, 5)
             if start <= deadline(_quarter_end(y, q)) <= end]
    if len(homes) != 1:
        raise Form13fError(f"window {start}..{end} holds {len(homes)} quarter deadlines")
    return homes[0]


# ---- client ---------------------------------------------------------------------------------

def _sec_links(html: str, pattern: str) -> list[str]:
    out = set()
    for href in re.findall(r'href="([^"]+)"', html):
        url = href if href.startswith("http") else SEC + href
        if urlparse(url).netloc == "www.sec.gov" and re.search(pattern, url):
            out.add(url)
    return sorted(out)


class Form13fClient:
    """Data-set and fails-to-deliver lists and files, through SecClient (project User-Agent,
    SEC hosts only)."""

    def __init__(self, sec=None):
        if sec is None:
            from src import config
            from src.ingestion.sec import SecClient
            sec = SecClient(config.sec_user_agent())
        self.sec = sec

    def windows(self) -> list[str]:
        return _sec_links(self.sec.get(DATASETS_PAGE).decode("utf-8", "replace"), r"/[^/]+_form13f\.zip$")

    def ftd_files(self) -> list[str]:
        return _sec_links(self.sec.get(FTD_PAGE).decode("utf-8", "replace"), r"/cnsfails\d{6}[ab]\.zip$")

    def download(self, url: str, dest: Path) -> dict:
        # force: the zip is deleted after loading, so a conditional GET would answer 304.
        return self.sec.download(url, dest, force=True)


# ---- fails-to-deliver: CUSIP to symbol ------------------------------------------------------

def parse_ftd(data: bytes) -> list[tuple[date, str, str, str]]:
    """(settlement date, CUSIP, symbol, description) per line. Lines that are not a dated row
    (a trailer, a blank) are skipped."""
    with zipfile.ZipFile(_bytes_io(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if len(names) != 1:
            raise Form13fError(f"fails-to-deliver zip holds {names}")
        text = z.read(names[0]).decode("latin-1")
    lines = text.splitlines()
    if not lines or not lines[0].upper().startswith("SETTLEMENT DATE|CUSIP|SYMBOL"):
        raise Form13fError(f"unexpected fails-to-deliver header {lines[:1]!r:.120}")
    out = []
    for ln in lines[1:]:
        p = [x.strip() for x in ln.split("|")]
        if len(p) < 5 or not re.fullmatch(r"\d{8}", p[0]) or not p[1] or not p[2]:
            continue
        out.append((date(int(p[0][:4]), int(p[0][4:6]), int(p[0][6:])), p[1].upper(), p[2].upper(), p[4] or None))
    return out


def _bytes_io(data: bytes):
    import io
    return io.BytesIO(data)


def load_ftd(con: duckdb.DuckDBPyConnection, rows: list[tuple[date, str, str, str]]) -> int:
    agg: dict[tuple[str, str], list] = {}
    for d, cusip, sym, desc in rows:
        a = agg.get((cusip, sym))
        if a is None:
            agg[(cusip, sym)] = [d, d, desc]
        else:
            a[0], a[1] = min(a[0], d), max(a[1], d)
            if d >= a[1] and desc:
                a[2] = desc
    if not agg:
        return 0
    t = pa.table({"cusip": [k[0] for k in agg], "symbol": [k[1] for k in agg],
                  "first_seen": [v[0] for v in agg.values()], "last_seen": [v[1] for v in agg.values()],
                  "description": [v[2] for v in agg.values()]})
    con.register("ftd_rows", t)
    try:
        con.execute("""
            INSERT INTO cusip_symbols SELECT * FROM ftd_rows
            ON CONFLICT (cusip, symbol) DO UPDATE SET
                first_seen = least(cusip_symbols.first_seen, excluded.first_seen),
                description = CASE WHEN excluded.last_seen >= cusip_symbols.last_seen
                                   THEN coalesce(excluded.description, cusip_symbols.description)
                                   ELSE cusip_symbols.description END,
                last_seen = greatest(cusip_symbols.last_seen, excluded.last_seen)""")
    finally:
        con.unregister("ftd_rows")
    return len(agg)


# ---- 13F windows ----------------------------------------------------------------------------

def _tsv_rows(path: Path, n_min: int):
    """Rows of a data-set table that start with an accession number. A free-text field broken
    by a newline leaves a continuation line, which is skipped."""
    with open(path, encoding="utf-8", errors="replace") as f:
        header = f.readline().rstrip("\r\n").split("\t")
        yield header
        for ln in f:
            p = ln.rstrip("\r\n").split("\t")
            if len(p) >= n_min and ACCN.fullmatch(p[0].strip()):
                yield p


def _extract(z: zipfile.ZipFile, table: str, dest: Path) -> Path:
    names = [n for n in z.namelist() if n.rsplit("/", 1)[-1].upper() == f"{table}.TSV"]
    if len(names) != 1:
        raise Form13fError(f"{table}.tsv not found once in the zip")
    out = dest / f"{table}.tsv"
    with z.open(names[0]) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    return out


def load_window(con: duckdb.DuckDBPyConnection, zip_path: Path, name: str, work: Path,
                loaded_at: datetime, sha256: str | None = None) -> dict:
    """Aggregate one data-set zip into its quarter's rows and mark it done, in one transaction.
    Raises Form13fError (nothing written) on a layout the code does not know or a filing
    outside the window."""
    start, end = window_dates(name)
    home = home_period(start, end)
    due, prev = deadline(home), _prev_quarter(home)
    tmp = Path(tempfile.mkdtemp(prefix=".tsv-", dir=work))
    try:
        with zipfile.ZipFile(zip_path) as z:
            sub_p, cov_p, info_p = (_extract(z, t, tmp) for t in ("SUBMISSION", "COVERPAGE", "INFOTABLE"))
        sub = _tsv_rows(sub_p, 5)
        if next(sub)[:5] != SUB_COLS:
            raise Form13fError(f"{name}: unexpected SUBMISSION columns")
        cov = _tsv_rows(cov_p, 16)
        ch = next(cov)
        if ch[0] != "ACCESSION_NUMBER" or ch[4] != "AMENDMENTTYPE" or ch[15] != "REPORTTYPE":
            raise Form13fError(f"{name}: unexpected COVERPAGE columns")
        amend = {p[0]: p[4].strip().upper() for p in cov}
        with open(info_p, encoding="utf-8", errors="replace") as f:
            if f.readline().rstrip("\r\n").split("\t") != INFO_COLS:
                raise Form13fError(f"{name}: unexpected INFOTABLE columns")

        filings = []
        for p in sub:
            filed = _dmy(p[1])
            if not start <= filed <= end:
                raise Form13fError(f"{name}: {p[0]} filed {filed}, outside the window {start}..{end}")
            filings.append((p[0], filed, p[2].strip().upper(), int(p[3]), _dmy(p[4])))
        mine = [f for f in filings if f[4] == home]
        late = sum(1 for f in mine if f[1] > due)
        ontime = [f for f in mine if f[1] <= due]
        notices = len({f[3] for f in ontime if f[2].startswith("13F-NT")})
        kind = {}
        for accn, filed, typ, filer, _ in ontime:
            if typ == "13F-HR":
                kind[accn] = "base"
            elif typ == "13F-HR/A":
                kind[accn] = {"RESTATEMENT": "base", "NEW HOLDINGS": "add"}.get(amend.get(accn, ""), "other")
        restatements = sum(1 for f in ontime if f[2] == "13F-HR/A" and kind[f[0]] == "base")
        new_holdings = sum(1 for v in kind.values() if v == "add")
        other = sum(1 for v in kind.values() if v == "other")
        eff = []
        by_filer: dict[int, list] = {}
        for accn, filed, typ, filer, _ in ontime:
            if accn in kind and kind[accn] != "other":
                by_filer.setdefault(filer, []).append((filed, accn, kind[accn]))
        for filer, fs in by_filer.items():
            bases = [(fd, a) for fd, a, k in fs if k == "base"]
            base = max(bases) if bases else None
            eff += [(a, filer) for fd, a, k in fs
                    if (k == "base" and (fd, a) == base) or (k == "add" and (base is None or (fd, a) > base))]

        con.begin()
        try:
            con.register("eff_rows", pa.table({"accn": [e[0] for e in eff], "filer": [e[1] for e in eff]}))
            try:
                con.execute("CREATE OR REPLACE TEMP TABLE f13_eff AS SELECT * FROM eff_rows")
            finally:
                con.unregister("eff_rows")
            cols = ", ".join(f"'{c}': 'VARCHAR'" for c in INFO_COLS)
            con.execute(f"""
                CREATE OR REPLACE TEMP TABLE f13_pos AS
                SELECT e.filer, upper(trim(i.CUSIP)) AS cusip,
                       sum(CAST(round(CAST(i.SSHPRNAMT AS DOUBLE)) AS BIGINT)) AS shares
                FROM read_csv('{info_p}', delim='\t', header=true, quote='', escape='', auto_detect=false,
                              columns={{{cols}}}) i
                JOIN f13_eff e ON e.accn = i.ACCESSION_NUMBER
                WHERE upper(trim(i.SSHPRNAMTTYPE)) = 'SH' AND coalesce(trim(i.PUTCALL), '') = ''
                GROUP BY ALL HAVING sum(CAST(round(CAST(i.SSHPRNAMT AS DOUBLE)) AS BIGINT)) > 0""")
            has_prev = con.execute("SELECT count(*) FROM form13f_last_positions WHERE period = ?",
                                   [prev]).fetchall()[0][0] > 0
            con.execute("""
                CREATE OR REPLACE TEMP TABLE f13_prev AS
                SELECT filer_cik AS filer, cusip FROM form13f_last_positions WHERE period = ?""", [prev])
            con.execute("""
                CREATE OR REPLACE TEMP TABLE f13_both AS
                SELECT DISTINCT filer FROM f13_eff INTERSECT SELECT DISTINCT filer FROM f13_prev""")
            con.execute("DELETE FROM form13f_quarters WHERE period = ?", [home])
            con.execute("""
                INSERT INTO form13f_quarters
                WITH now_ AS (SELECT cusip, count(*) AS holders, sum(shares) AS shares FROM f13_pos GROUP BY cusip),
                     new_ AS (SELECT p.cusip, count(*) AS n FROM f13_pos p SEMI JOIN f13_both b USING (filer)
                              ANTI JOIN f13_prev q ON q.filer = p.filer AND q.cusip = p.cusip GROUP BY p.cusip),
                     gone AS (SELECT q.cusip, count(*) AS n FROM f13_prev q SEMI JOIN f13_both b USING (filer)
                              ANTI JOIN f13_pos p ON p.filer = q.filer AND p.cusip = q.cusip
                              WHERE q.cusip IS NOT NULL GROUP BY q.cusip),
                     cusips AS (SELECT cusip FROM now_ UNION SELECT cusip FROM gone)
                SELECT ?, c.cusip, coalesce(now_.holders, 0), coalesce(now_.shares, 0),
                       CASE WHEN ? THEN coalesce(new_.n, 0) END, CASE WHEN ? THEN coalesce(gone.n, 0) END, ?
                FROM cusips c LEFT JOIN now_ USING (cusip) LEFT JOIN new_ USING (cusip) LEFT JOIN gone USING (cusip)
            """, [home, has_prev, has_prev, due])
            n_cusips = con.execute("SELECT count(*) FROM form13f_quarters WHERE period = ?", [home]).fetchall()[0][0]
            con.execute("INSERT OR REPLACE INTO form13f_periods VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [home, due, name, len(by_filer), notices, restatements, new_holdings, other, late,
                         n_cusips, has_prev, loaded_at])
            newest = con.execute("SELECT max(period) FROM form13f_last_positions").fetchall()[0][0]
            if newest is None or home >= newest:       # keep the newest quarter's positions only
                con.execute("DELETE FROM form13f_last_positions")
                con.execute("""
                    INSERT INTO form13f_last_positions
                    SELECT ?, filer, cusip FROM f13_pos
                    UNION ALL
                    SELECT ?, filer, NULL FROM (SELECT DISTINCT filer FROM f13_eff EXCEPT SELECT filer FROM f13_pos)
                """, [home, home])
            for t in ("f13_eff", "f13_pos", "f13_prev", "f13_both"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
            ledger.mark(con, f"13f@{name}", STAGE, SCHEMA_VERSION, "done", input_sha256=sha256,
                        output_ref=f"period={home};cusips={n_cusips};filers={len(by_filer)};late={late}")
            con.commit()
        except BaseException:
            con.rollback()
            raise
        return {"period": home, "cusips": n_cusips, "filers": len(by_filer), "late": late}
    except (duckdb.Error, ValueError, KeyError, zipfile.BadZipFile) as e:
        raise Form13fError(f"{name}: {type(e).__name__}: {e}"[:500]) from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- the job --------------------------------------------------------------------------------

@dataclass
class LoadStats:
    ftd_files: int = 0
    windows: int = 0
    cusip_rows: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def load(con: duckdb.DuckDBPyConnection, client, work: Path, now: datetime | None = None) -> LoadStats:
    """Fails-to-deliver files, then data-set zips in window order, each loaded once. A zip is
    deleted after loading (SecClient keeps its metadata). A failed file is reported and retried
    on the next run; it never stops the others."""
    s = LoadStats()
    work.mkdir(parents=True, exist_ok=True)
    ftd = sorted(client.ftd_files(), key=lambda u: u.rsplit("/", 1)[1])
    for url in ftd:
        name = url.rsplit("/", 1)[1]
        if ledger.is_done(con, f"ftd@{name}", FTD_STAGE, SCHEMA_VERSION):
            continue
        dest = work / name
        try:
            client.download(url, dest)
            n = load_ftd_file(con, dest, name)
        except (Form13fError, requests.RequestException, OSError, zipfile.BadZipFile, RuntimeError) as e:
            s.failed += 1
            s.errors.append(f"{name}: {e}")
            continue
        finally:
            dest.unlink(missing_ok=True)
        s.ftd_files += 1
        s.cusip_rows += n
    wins = []
    for url in client.windows():
        name = url.rsplit("/", 1)[1]
        try:
            wins.append((window_dates(name)[0], name, url))
        except Form13fError as e:
            s.failed += 1
            s.errors.append(str(e))
    for _, name, url in sorted(wins):
        if ledger.is_done(con, f"13f@{name}", STAGE, SCHEMA_VERSION):
            continue
        dest = work / name
        try:
            meta = client.download(url, dest)
            load_window(con, dest, name, work, now or datetime.now(), meta.get("sha256"))
        except (Form13fError, requests.RequestException, OSError, RuntimeError) as e:
            s.failed += 1
            s.errors.append(str(e))
            continue
        finally:
            dest.unlink(missing_ok=True)
        s.windows += 1
    return s


def load_ftd_file(con: duckdb.DuckDBPyConnection, path: Path, name: str) -> int:
    rows = parse_ftd(path.read_bytes())
    con.begin()
    try:
        n = load_ftd(con, rows)
        ledger.mark(con, f"ftd@{name}", FTD_STAGE, SCHEMA_VERSION, "done", output_ref=f"pairs={n}")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return n
