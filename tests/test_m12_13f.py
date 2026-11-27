"""M12-B6: 13F institutional holdings per quarter, informational only.

Live facts (SEC, 2026-11-27, project User-Agent):
- 54 data-set zips on sec.gov/data-research/sec-markets-data/form-13f-data-sets: 2013q2..2023q4
  by filing quarter, 01jan2024-29feb2024, then 3-month filing windows ending Feb/May/Aug/Nov.
  SUBMISSION filing dates span each window exactly (2013q3: 2013-07-01..09-30).
- TSV, tab-separated, no quoting, the same columns 2013-2026. Dates are DD-MON-YYYY.
- Jun-Aug 2026: 9,396 holdings reports, 504 combination reports, 1,952 notices; 262 RESTATEMENT
  and 144 NEW HOLDINGS amendments.
- Fails-to-deliver files (cnsfails, 2009-07..): "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|
  DESCRIPTION|PRICE", the only SEC file that pairs a CUSIP with a ticker.
"""
import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from src.db.schema import connect
from src.ingestion import form13f as f13
from src.obsidian.sync import sync_vault
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company, screen_rows

SUB_HEAD = "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT"
COVER_HEAD = ("ACCESSION_NUMBER\tREPORTCALENDARORQUARTER\tISAMENDMENT\tAMENDMENTNO\tAMENDMENTTYPE\tCONFDENIEDEXPIRED\t"
              "DATEDENIEDEXPIRED\tDATEREPORTED\tREASONFORNONCONFIDENTIALITY\tFILINGMANAGER_NAME\tFILINGMANAGER_STREET1\t"
              "FILINGMANAGER_STREET2\tFILINGMANAGER_CITY\tFILINGMANAGER_STATEORCOUNTRY\tFILINGMANAGER_ZIPCODE\tREPORTTYPE\t"
              "FORM13FFILENUMBER\tCRDNUMBER\tSECFILENUMBER\tPROVIDEINFOFORINSTRUCTION5\tADDITIONALINFORMATION")
INFO_HEAD = ("ACCESSION_NUMBER\tINFOTABLE_SK\tNAMEOFISSUER\tTITLEOFCLASS\tCUSIP\tFIGI\tVALUE\tSSHPRNAMT\tSSHPRNAMTTYPE\t"
             "PUTCALL\tINVESTMENTDISCRETION\tOTHERMANAGER\tVOTING_AUTH_SOLE\tVOTING_AUTH_SHARED\tVOTING_AUTH_NONE")
FTD_HEAD = "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE"
ACME_CUSIP = "000123104"


def dmy(d: date) -> str:
    return d.strftime("%d-%b-%Y").upper()


def zip13f(filings: list[dict]) -> bytes:
    """A data-set zip. Each filing: accn, filed, type, filer, period, amend (type or ""),
    report ("13F HOLDINGS REPORT" by default), rows [(cusip, shares, "SH"/"PRN", putcall)]."""
    sub, cov, info = [SUB_HEAD], [COVER_HEAD], [INFO_HEAD]
    sk = 1
    for f in filings:
        sub.append(f"{f['accn']}\t{dmy(f['filed'])}\t{f['type']}\t{f['filer']:010d}\t{dmy(f['period'])}")
        amend = f.get("amend", "")
        cov.append("\t".join([f["accn"], dmy(f["period"]), "Y" if amend else "", "1" if amend else "", amend, "", "", "",
                              "", f"MANAGER {f['filer']}", "1 MAIN ST", "", "CITY", "NY", "10001",
                              f.get("report", "13F HOLDINGS REPORT"), "028-00001", "", "", "N", ""]))
        for cusip, shares, kind, putcall in f.get("rows", []):
            info.append("\t".join([f["accn"], str(sk), "ACME CORP", "COM", cusip, "", str(shares * 12), str(shares),
                                   kind, putcall, "SOLE", "", str(shares), "0", "0"]))
            sk += 1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, lines in (("SUBMISSION.tsv", sub), ("COVERPAGE.tsv", cov), ("INFOTABLE.tsv", info),
                            ("SUMMARYPAGE.tsv", ["ACCESSION_NUMBER\tOTHERINCLUDEDMANAGERSCOUNT\tTABLEENTRYTOTAL\t"
                                                 "TABLEVALUETOTAL\tISCONFIDENTIALOMITTED"])):
            z.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()


def zipftd(rows: list[tuple[date, str, str, str]]) -> bytes:
    lines = [FTD_HEAD] + [f"{d:%Y%m%d}|{c}|{s}|100|{desc}|1.00" for d, c, s, desc in rows]
    lines.append("Trailer record count " + str(len(rows)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cnsfails.txt", "\n".join(lines) + "\n")
    return buf.getvalue()


def hr(accn, filer, filed, period, rows, type_="13F-HR", amend="", report="13F HOLDINGS REPORT"):
    return {"accn": accn, "filer": filer, "filed": filed, "period": period, "rows": rows, "type": type_,
            "amend": amend, "report": report}


class FakeSec:
    """Stands in for Form13fClient: named zips served by URL, written to dest like SecClient.download."""
    BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    FTD = "https://www.sec.gov/files/data/fails-deliver-data/"

    def __init__(self, windows: dict[str, bytes], ftd: dict[str, bytes] | None = None):
        self.zips = {self.BASE + k: v for k, v in windows.items()} | {self.FTD + k: v for k, v in (ftd or {}).items()}
        self.fetched = []

    def windows(self) -> list[str]:
        return [u for u in self.zips if u.startswith(self.BASE)]

    def ftd_files(self) -> list[str]:
        return [u for u in self.zips if u.startswith(self.FTD)]

    def download(self, url: str, dest: Path) -> dict:
        self.fetched.append(url.rsplit("/", 1)[1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.zips[url])
        return {"path": str(dest), "sha256": "x" * 64, "bytes": len(self.zips[url]), "changed": True}


Q1, Q2 = date(2024, 3, 31), date(2024, 6, 30)
D1, D2 = date(2024, 5, 15), date(2024, 8, 14)            # their deadlines


def two_quarters() -> dict[str, bytes]:
    """Q1 2024: managers 1-3 hold ACME. Q2 2024: 1 keeps, 2 sells out, 4 buys (filed Q1 too),
    5 is new to 13F (no Q1 report), 3 files late."""
    q1 = zip13f([hr("0000000001-24-000001", 1, date(2024, 5, 1), Q1, [(ACME_CUSIP, 100, "SH", "")]),
                 hr("0000000002-24-000001", 2, date(2024, 5, 2), Q1, [(ACME_CUSIP, 200, "SH", "")]),
                 hr("0000000003-24-000001", 3, date(2024, 5, 3), Q1, [(ACME_CUSIP, 300, "SH", "")]),
                 hr("0000000004-24-000001", 4, date(2024, 5, 4), Q1, [("999999999", 1, "SH", "")])])
    q2 = zip13f([hr("0000000001-24-000002", 1, date(2024, 8, 1), Q2, [(ACME_CUSIP, 110, "SH", "")]),
                 hr("0000000002-24-000002", 2, date(2024, 8, 2), Q2, [("999999999", 5, "SH", "")]),
                 hr("0000000004-24-000002", 4, date(2024, 8, 3), Q2, [(ACME_CUSIP, 400, "SH", "")]),
                 hr("0000000005-24-000001", 5, date(2024, 8, 5), Q2, [(ACME_CUSIP, 500, "SH", "")]),
                 hr("0000000003-24-000002", 3, date(2024, 8, 20), Q2, [(ACME_CUSIP, 300, "SH", "")])])
    return {"01mar2024-31may2024_form13f.zip": q1, "01jun2024-31aug2024_form13f.zip": q2}


def ftd_acme(symbol="ACME") -> dict[str, bytes]:
    return {"cnsfails202407a.zip": zipftd([(date(2024, 7, 1), ACME_CUSIP, symbol, "ACME CORP COM")])}


def quarter(con, period, cusip=ACME_CUSIP):
    r = con.execute("SELECT holders, shares, holders_new, holders_exited, known_at FROM form13f_quarters "
                    "WHERE period = ? AND cusip = ?", [period, cusip]).fetchall()
    return r[0] if r else None


@pytest.fixture
def con():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())             # 1,000,000 shares, close 12
    return c


def load(con, tmp_path, windows=None, ftd=None):
    return f13.load(con, FakeSec(two_quarters() if windows is None else windows, ftd_acme() if ftd is None else ftd),
                    tmp_path / "13f", now=datetime(2026, 11, 27, 12))


# --- dates ----------------------------------------------------------------------------

@pytest.mark.parametrize("period, due", [
    (date(2026, 6, 30), date(2026, 8, 14)), (date(2024, 3, 31), date(2024, 5, 15)),
    (date(2023, 9, 30), date(2023, 11, 14)),
    (date(2025, 12, 31), date(2026, 2, 17)),       # Sat Feb 14, Mon Feb 16 Presidents Day
    (date(2026, 12, 31), date(2027, 2, 16)),       # Sun Feb 14, Mon Feb 15 Presidents Day
    (date(2024, 12, 31), date(2025, 2, 14)),       # a Friday
    (date(2025, 3, 31), date(2025, 5, 15)), (date(2021, 6, 30), date(2021, 8, 16)),   # Sat Aug 14
])
def test_the_deadline_is_45_days_after_the_quarter_rolled_to_a_business_day(period, due):
    assert f13.deadline(period) == due


@pytest.mark.parametrize("name, start, end, home", [
    ("2013q3_form13f.zip", date(2013, 7, 1), date(2013, 9, 30), date(2013, 6, 30)),
    ("2023q1_form13f.zip", date(2023, 1, 1), date(2023, 3, 31), date(2022, 12, 31)),
    ("01jan2024-29feb2024_form13f.zip", date(2024, 1, 1), date(2024, 2, 29), date(2023, 12, 31)),
    ("01mar2024-31may2024_form13f.zip", date(2024, 3, 1), date(2024, 5, 31), date(2024, 3, 31)),
    ("01dec2025-28feb2026_form13f.zip", date(2025, 12, 1), date(2026, 2, 28), date(2025, 12, 31)),
])
def test_each_window_holds_exactly_one_quarter_up_to_its_deadline(name, start, end, home):
    assert f13.window_dates(name) == (start, end)
    assert f13.home_period(start, end) == home


def test_an_unknown_window_name_is_refused():
    with pytest.raises(f13.Form13fError, match="window"):
        f13.window_dates("form13f_latest.zip")


# --- quarter aggregates ---------------------------------------------------------------

def test_a_quarter_counts_holders_and_shares_filed_by_its_deadline(con, tmp_path):
    s = load(con, tmp_path)
    assert s.windows == 2 and s.failed == 0
    assert quarter(con, Q1) == (3, 600, None, None, D1)        # no earlier quarter loaded
    # Q2: 1, 4 and 5 hold (3 filed after Aug 14 and is left out).
    holders, shares, new, exited, known = quarter(con, Q2)
    assert (holders, shares, known) == (3, 1010, D2)
    # Among managers with a report in both quarters (1, 2, 4): 4 is new, 2 left. 5 is not "new".
    assert (new, exited) == (1, 1)
    p = con.execute("SELECT filers, notices, restatements, new_holdings, late, deadline FROM form13f_periods "
                    "WHERE period = ?", [Q2]).fetchall()[0]
    assert p == (4, 0, 0, 0, 1, D2)


def test_a_restatement_replaces_and_a_new_holdings_amendment_adds(con, tmp_path):
    q = zip13f([
        hr("0000000001-24-000001", 1, date(2024, 5, 1), Q1, [(ACME_CUSIP, 100, "SH", "")]),
        hr("0000000001-24-000009", 1, date(2024, 5, 10), Q1, [(ACME_CUSIP, 150, "SH", "")],
           type_="13F-HR/A", amend="RESTATEMENT"),
        hr("0000000001-24-000010", 1, date(2024, 5, 12), Q1, [("888888888", 7, "SH", "")],
           type_="13F-HR/A", amend="NEW HOLDINGS"),
        hr("0000000001-24-000011", 1, date(2024, 5, 20), Q1, [(ACME_CUSIP, 999, "SH", "")],   # after the deadline
           type_="13F-HR/A", amend="RESTATEMENT"),
        # Manager 2 files two originals: the newest stands alone, never the sum.
        hr("0000000002-24-000001", 2, date(2024, 5, 1), Q1, [(ACME_CUSIP, 40, "SH", "")]),
        hr("0000000002-24-000002", 2, date(2024, 5, 3), Q1, [(ACME_CUSIP, 50, "SH", "")]),
        # A new-holdings amendment filed before a later restatement is replaced with it.
        hr("0000000006-24-000001", 6, date(2024, 5, 1), Q1, [(ACME_CUSIP, 10, "SH", "")]),
        hr("0000000006-24-000002", 6, date(2024, 5, 2), Q1, [(ACME_CUSIP, 5, "SH", "")],
           type_="13F-HR/A", amend="NEW HOLDINGS"),
        hr("0000000006-24-000003", 6, date(2024, 5, 3), Q1, [(ACME_CUSIP, 12, "SH", "")],
           type_="13F-HR/A", amend="RESTATEMENT"),
        hr("0000000007-24-000001", 7, date(2024, 5, 1), Q1, [], type_="13F-NT", report="13F NOTICE"),
    ])
    load(con, tmp_path, {"01mar2024-31may2024_form13f.zip": q})
    assert quarter(con, Q1)[:2] == (3, 150 + 50 + 12)
    assert quarter(con, Q1, "888888888")[:2] == (1, 7)
    p = con.execute("SELECT filers, notices, restatements, new_holdings, late FROM form13f_periods").fetchall()[0]
    assert p == (3, 1, 2, 2, 1)


def test_only_share_rows_count_never_options_or_principal_amounts(con, tmp_path):
    q = zip13f([hr("0000000001-24-000001", 1, date(2024, 5, 1), Q1,
                   [(ACME_CUSIP, 100, "SH", ""), (ACME_CUSIP, 100, "SH", ""),       # two lines, one position
                    (ACME_CUSIP, 900, "SH", "Put"), (ACME_CUSIP, 800, "SH", "CALL"),
                    (ACME_CUSIP, 5_000, "PRN", "")]),
                hr("0000000002-24-000001", 2, date(2024, 5, 1), Q1, [(ACME_CUSIP, 70, "SH", "Call")])])
    load(con, tmp_path, {"01mar2024-31may2024_form13f.zip": q})
    assert quarter(con, Q1)[:2] == (1, 200)                    # manager 2 holds only an option


def test_a_quarter_filed_in_the_wrong_window_is_refused_and_retried(con, tmp_path):
    # A Q2 report inside the Mar-May window cannot be: the window's quarter is Q1.
    bad = zip13f([hr("0000000001-24-000001", 1, date(2024, 5, 1), Q1, [(ACME_CUSIP, 1, "SH", "")]),
                  hr("0000000001-24-000002", 1, date(2024, 2, 1), Q2, [(ACME_CUSIP, 1, "SH", "")])])
    s = load(con, tmp_path, {"01mar2024-31may2024_form13f.zip": bad})
    assert s.failed == 1 and "outside" in s.errors[0]
    assert con.execute("SELECT count(*) FROM form13f_quarters").fetchall()[0][0] == 0


def test_each_zip_is_loaded_once_and_deleted_and_a_replay_fetches_nothing(con, tmp_path):
    load(con, tmp_path)
    n = con.execute("SELECT count(*) FROM form13f_quarters").fetchall()[0][0]
    fake = FakeSec(two_quarters(), ftd_acme())
    s = f13.load(con, fake, tmp_path / "13f")
    assert fake.fetched == [] and s.windows == 0 and s.ftd_files == 0
    assert con.execute("SELECT count(*) FROM form13f_quarters").fetchall()[0][0] == n
    assert not list((tmp_path / "13f").glob("*.zip")) and not list((tmp_path / "13f").glob("**/*.tsv"))


def test_windows_load_in_date_order_so_changes_see_the_quarter_before(con, tmp_path):
    w = two_quarters()
    later_first = dict(reversed(list(w.items())))
    load(con, tmp_path, later_first)
    assert quarter(con, Q2)[2:4] == (1, 1)


# --- CUSIP to company ---------------------------------------------------------------

def test_fails_to_deliver_files_give_each_cusip_its_symbols(con, tmp_path):
    ftd = {"cnsfails202406b.zip": zipftd([(date(2024, 6, 20), ACME_CUSIP, "ACM", "ACME CORP"),
                                          (date(2024, 6, 21), ACME_CUSIP, "ACM", "ACME CORP")]),
           "cnsfails202407a.zip": zipftd([(date(2024, 7, 1), ACME_CUSIP, "ACME", "ACME CORP COM")])}
    s = load(con, tmp_path, {}, ftd)
    assert s.ftd_files == 2
    rows = con.execute("SELECT cusip, symbol, first_seen, last_seen FROM cusip_symbols ORDER BY symbol").fetchall()
    assert rows == [(ACME_CUSIP, "ACM", date(2024, 6, 20), date(2024, 6, 21)),
                    (ACME_CUSIP, "ACME", date(2024, 7, 1), date(2024, 7, 1))]


def test_an_old_cusip_whose_ticker_was_reused_never_maps_to_the_new_issuer(con, tmp_path):
    ftd = {"cnsfails201501a.zip": zipftd([(date(2015, 1, 5), "777777107", "ACME", "OLD ACME MINING")]),
           "cnsfails202407a.zip": zipftd([(date(2024, 7, 1), ACME_CUSIP, "ACME", "ACME CORP COM")])}
    load(con, tmp_path, {}, ftd)
    assert con.execute(f"SELECT cik, cusip FROM ({f13.cusip_cik_sql()})").fetchall() == [(CIK, ACME_CUSIP)]


# --- screen ---------------------------------------------------------------------------

def test_the_newest_quarter_past_its_deadline_is_read(con, tmp_path):
    load(con, tmp_path)
    r = screen_rows(con)["ACME"]                               # AS_OF 2024-09-03 > Aug 14
    assert r["inst_period"] == Q2 and r["inst_known_at"] == D2
    assert (r["inst_holders"], r["inst_holders_new"], r["inst_holders_exited"]) == (3, 1, 1)
    assert r["inst_pct"] == pytest.approx(1010 / 1_000_000)
    early = screen_rows(con, date(2024, 8, 13))["ACME"]       # before the Q2 deadline: Q1
    assert early["inst_period"] == Q1 and early["inst_holders"] == 3


def test_a_mapped_company_no_manager_reports_has_zero_holders(con, tmp_path):
    q = zip13f([hr("0000000001-24-000001", 1, date(2024, 5, 1), Q1, [("999999999", 1, "SH", "")])])
    load(con, tmp_path, {"01mar2024-31may2024_form13f.zip": q})
    r = screen_rows(con)["ACME"]
    assert r["inst_period"] == Q1 and r["inst_holders"] == 0 and r["inst_pct"] == 0


def test_a_company_without_a_cusip_or_without_13f_data_is_null(con, tmp_path):
    assert screen_rows(con)["ACME"]["inst_holders"] is None   # nothing loaded
    load(con, tmp_path, ftd={"cnsfails202407a.zip": zipftd([(date(2024, 7, 1), ACME_CUSIP, "OTHER", "OTHER")])})
    r = screen_rows(con)["ACME"]
    assert r["inst_holders"] is None and r["inst_period"] is None and r["inst_pct"] is None


def test_institutional_share_is_not_a_share_of_an_untrusted_count(tmp_path):
    from tests.test_m4_screen import DEI, inst
    c = connect(":memory:")
    tags = cheap_company()
    tags[(DEI, "EntityCommonStockSharesOutstanding", "shares")] = [inst("2023-12-01", 1_000_000)]
    add_company(c, CIK, "ACME", tags)
    load(c, tmp_path)
    r = screen_rows(c)["ACME"]
    assert "stale_shares" in r["issues"] and r["inst_holders"] == 3 and r["inst_pct"] is None


def test_13f_data_never_changes_status_rank_or_shortlist(tmp_path):
    def build(with_13f, d):
        c = connect(":memory:")
        add_company(c, CIK, "ACME", cheap_company())
        add_buy(c, CIK, "b1")
        if with_13f:
            load(c, d)
        return {t: (r["status"], r["rank"], r["failed_filters"], r["issues"], r["flags"], r["signals"])
                for t, r in screen_rows(c).items()}
    assert build(True, tmp_path / "a") == build(False, tmp_path / "b")


# --- vault and CLI --------------------------------------------------------------------

def test_company_note_dashboard_and_source_note_show_13f(con, tmp_path):
    add_buy(con, CIK, "b1")
    load(con, tmp_path)
    vault = tmp_path / "vault"
    sync_vault(con, vault, AS_OF)
    note = (vault / "Companies" / "ACME.md").read_text()
    meta = dict(split_note(note)[0])
    assert meta["inst_holders"] == 3 and meta["inst_pct"] == pytest.approx(0.001)
    assert ("13F managers reporting a position at 2024-06-30 (filed by 2024-08-14): 3, 0.1% of shares; "
            "1 new and 1 gone among managers who filed both quarters.") in note
    assert "13F quarter 2024-06-30" in (vault / "Dashboard.md").read_text()
    source = (vault / "Sources" / "SEC Form 13F data sets.md").read_text()
    assert "45 days" in source and "fails-to-deliver" in source
    assert sum(sync_vault(con, vault, AS_OF).written.values()) == 0


def test_ingest_13f_is_a_manual_job_outside_the_schedule(con, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from src import cli, config
    monkeypatch.setattr(f13, "Form13fClient", lambda: FakeSec(two_quarters(), ftd_acme()))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    cli.ingest_13f(con, SimpleNamespace())
    out = capsys.readouterr().out
    assert "13f: 1 FTD files, 2 windows" in out
    assert cli.INGEST_JOBS["13f"] == [cli.ingest_13f]
    assert cli.ingest_13f not in cli.INGEST_JOBS["all"]
    assert all("13f" not in step for steps in cli.RUN_STEPS.values() for step in steps)
