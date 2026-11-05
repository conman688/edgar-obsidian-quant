"""M3 Form 4 sources: bulk data set vs per-filing XML, shared ledger, amendments, index."""
import zipfile
from datetime import date
from pathlib import Path

import pytest

from src.db.queries import cluster_buys
from src.db.schema import connect
from src.ingestion import form4_sources as fs
from src.ingestion.form4_insider import COLUMNS, parse_form4

FIX = Path(__file__).parent / "fixtures"
AMD_XML = (FIX / "form4_10b5_1.xml").read_bytes()
AMD_ACCN = (FIX / "form345_amd" / "ACCESSION").read_text().strip()
AMD_FILED = date(2025, 4, 17)
SAME = ["line_no", "issuer_cik", "owner_cik", "owner_name", "is_director", "is_officer", "officer_title",
        "txn_date", "security_title", "txn_code", "acq_disp", "shares", "price", "shares_owned_following",
        "ownership_form", "nature_of_ownership", "is_10b5_1", "footnotes", "form_type", "period_of_report"]


def amd_zip(tmp_path: Path) -> Path:
    z = tmp_path / "2025q2_form345.zip"
    with zipfile.ZipFile(z, "w") as out:
        for name in fs.DATASET_FILES:
            out.write(FIX / "form345_amd" / name, name)
    return z


def rows(con, accn):
    return con.execute(f"SELECT {', '.join(SAME)} FROM form4_transactions WHERE accn = ? ORDER BY line_no",
                       [accn]).fetchall()


def test_real_filing_with_10b5_1_box_checked():
    parsed = parse_form4(AMD_XML, AMD_ACCN, AMD_FILED)
    assert [(r["txn_code"], r["shares"], r["price"]) for r in parsed] == \
        [("M", 17500.0, 19.1), ("S", 10089.0, 95.29), ("S", 7411.0, 95.71)]
    assert all(r["is_10b5_1"] is True for r in parsed)
    assert {(r["form_type"], r["period_of_report"], r["owner_name"]) for r in parsed} == \
        {("4", date(2025, 4, 15), "Papermaster Mark D")}


def test_data_set_and_xml_paths_store_identical_rows(tmp_path):
    xml_con, ds_con = connect(":memory:"), connect(":memory:")
    from src.ingestion.form4_insider import store
    store(xml_con, parse_form4(AMD_XML, AMD_ACCN, AMD_FILED))
    z = amd_zip(tmp_path)
    assert fs.load_dataset(ds_con, z, "2025q2", "sha-a", tmp_path / "work") == 3
    assert rows(ds_con, AMD_ACCN) == rows(xml_con, AMD_ACCN)


def test_data_set_reload_and_overlap_with_xml_insert_nothing(tmp_path):
    con = connect(":memory:")
    z = amd_zip(tmp_path)
    assert fs.load_dataset(con, z, "2025q2", "sha-a", tmp_path / "work") == 3
    assert fs.load_dataset(con, z, "2025q2", "sha-a", tmp_path / "work") is None   # same file: ledger skip
    assert fs.load_dataset(con, z, "2025q2", "sha-b", tmp_path / "work") == 0      # new file, known filing
    assert con.execute("SELECT source FROM form4_filings").fetchall() == [("dataset:2025q2",)]


INDEX = """Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
3                Some Holder                                                   1111111     2026-07-02  edgar/data/1111111/0000000000-26-000001.txt
4                ADVANCED MICRO DEVICES INC                                    2488        2026-07-02  edgar/data/2488/{a}.txt
4                Papermaster Mark D                                            1449649     2026-07-02  edgar/data/1449649/{a}.txt
4/A              ADVANCED MICRO DEVICES INC                                    2488        2026-07-03  edgar/data/2488/0000002488-26-000999.txt
4                NOT IN UNIVERSE CORP                                          9999999     2026-07-03  edgar/data/9999999/0000009999-26-000001.txt
"""


def universe_con():
    con = connect(":memory:")
    con.execute("INSERT INTO price_status VALUES (2488, 'AMD', 'ok', NULL, NULL, NULL, now())")
    return con


def test_index_parse_and_xml_fetch_with_retry_and_replay():
    con = universe_con()
    assert fs.load_index(con, "2026q3", INDEX.format(a=AMD_ACCN)) == 4         # Form 3 line ignored
    assert [p[0] for p in fs.pending_filings(con)] == [AMD_ACCN, "0000002488-26-000999"]

    def flaky(url):
        if "000999" in url:
            raise RuntimeError("timeout")
        return b"<SEC-DOCUMENT>\n<XML>\n" + AMD_XML.split(b"\n", 1)[1] + b"\n</XML>\n</SEC-DOCUMENT>"

    s = fs.fetch_filings(con, flaky)
    assert (s.pending, s.done, s.failed, s.rows) == (2, 1, 1, 3)
    assert [p[0] for p in fs.pending_filings(con)] == ["0000002488-26-000999"]   # failure retried next run
    s = fs.fetch_filings(con, lambda url: b"<SEC-DOCUMENT>plain text filing</SEC-DOCUMENT>")
    assert (s.skipped, fs.pending_filings(con)) == (1, [])                         # replay: nothing left
    assert fs.fetch_filings(con, flaky).pending == 0


def put(con, accn, form, filed, period, owner, shares, line=0, code="P"):
    row = dict.fromkeys(COLUMNS)
    row.update(accn=accn, line_no=line, is_derivative=False, is_holding=False, issuer_cik=7, owner_cik=owner,
               owner_name=f"Owner {owner}", is_director=True, security_title="Common Stock",
               filed=filed, txn_date=period, txn_code=code, acq_disp="A",
               shares=shares, price=10.0, ownership_form="D", nature_of_ownership="", is_10b5_1=False,
               form_type=form, period_of_report=period)
    con.execute(f"INSERT INTO form4_transactions ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                [row[c] for c in COLUMNS])


def bought(con, as_of):
    r = cluster_buys(con, as_of).fetchall()
    return r[0][3] if r else 0


def test_amendment_replaces_original_only_once_filed():
    con = connect(":memory:")
    put(con, "orig", "4", date(2026, 6, 3), date(2026, 6, 1), owner=1, shares=20_000)
    put(con, "amend", "4/A", date(2026, 6, 20), date(2026, 6, 1), owner=1, shares=15_000)
    assert bought(con, date(2026, 6, 10)) == 200_000      # amendment not filed yet: original stands
    assert bought(con, date(2026, 6, 25)) == 150_000      # amendment replaces, no double count


def test_separate_originals_and_identical_lots_all_count():
    con = connect(":memory:")
    put(con, "a1", "4", date(2026, 6, 3), date(2026, 6, 1), owner=1, shares=10_000)
    put(con, "a2", "4", date(2026, 6, 4), date(2026, 6, 1), owner=1, shares=10_000)            # second Form 4
    put(con, "a2", "4", date(2026, 6, 4), date(2026, 6, 1), owner=1, shares=10_000, line=1)    # identical lot
    put(con, "g1", "4", date(2026, 6, 4), date(2026, 6, 1), owner=2, shares=99_999, code="A")  # grant
    assert bought(con, date(2026, 6, 10)) == pytest.approx(300_000)


def test_quarters_and_published_page():
    assert fs.quarters("2025q3", "2026q2") == ["2025q3", "2025q4", "2026q1", "2026q2"]
    assert fs.quarter_of(date(2026, 11, 3)) == "2026q4"
    html = ('<a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">x</a>'
            '<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip">y</a>')
    assert fs.published_quarters(html) == {
        "2026q1": "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip",
        "2026q2": "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip"}


def test_conviction_filter_drops_ipo_preferred_funds_and_bad_prices():
    con = connect(":memory:")
    put(con, "dir", "4", date(2026, 6, 3), date(2026, 6, 1), owner=1, shares=10_000)              # kept
    put(con, "ipo", "4", date(2026, 6, 3), date(2026, 6, 1), owner=2, shares=10_000)
    put(con, "pref", "4", date(2026, 6, 3), date(2026, 6, 1), owner=3, shares=10_000)
    put(con, "fund", "4", date(2026, 6, 3), date(2026, 6, 1), owner=4, shares=10_000)
    put(con, "fat", "4", date(2026, 6, 3), date(2026, 6, 1), owner=5, shares=10)
    con.execute("UPDATE form4_transactions SET is_director = TRUE, security_title = 'Common Stock'")
    con.execute("UPDATE form4_transactions SET footnotes = 'Shares purchased in the Issuer''s initial public offering.' WHERE accn = 'ipo'")
    con.execute("UPDATE form4_transactions SET security_title = 'Series D Convertible Preferred Stock' WHERE accn = 'pref'")
    con.execute("UPDATE form4_transactions SET is_director = FALSE, is_ten_pct = TRUE WHERE accn = 'fund'")
    con.execute("UPDATE form4_transactions SET price = 150000 WHERE accn = 'fat'")          # total entered as price
    con.execute("INSERT INTO prices (cik, ticker, price_date, close_raw, source) VALUES (7, 'X', DATE '2026-06-01', 10.5, 't')")
    r = cluster_buys(con, date(2026, 6, 10)).fetchall()
    assert [(row[1], row[3]) for row in r] == [(1, 100_000)]   # one insider, the real director buy


def test_data_sets_before_2023_have_no_10b5_1_column(tmp_path):
    # SEC added AFF10B5ONE to SUBMISSION.tsv with the 2023 checkbox; 2011-2022 files lack it.
    sub = (FIX / "form345_amd" / "SUBMISSION.tsv").read_text().splitlines()
    head = sub[0].split("\t")
    i = head.index("AFF10B5ONE")
    old = "\n".join("\t".join(c for j, c in enumerate(line.split("\t")) if j != i) for line in sub) + "\n"
    z = tmp_path / "2012q1_form345.zip"
    with zipfile.ZipFile(z, "w") as out:
        for name in fs.DATASET_FILES:
            if name == "SUBMISSION.tsv":
                out.writestr(name, old)
            else:
                out.write(FIX / "form345_amd" / name, name)
    con = connect(":memory:")
    assert fs.load_dataset(con, z, "2012q1", "sha-old", tmp_path / "work") == 3
    # No checkbox: only a plan footnote can mark the trade; it is never coerced to FALSE.
    assert {r[0] for r in con.execute("SELECT is_10b5_1 FROM form4_transactions").fetchall()} <= {True, None}
