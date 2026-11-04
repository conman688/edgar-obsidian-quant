"""Regression tests for review findings on M2: price refresh windows, corporate actions,
status on failure, and filing replacement on databases migrated from M0."""
from datetime import date

import duckdb
import pandas as pd

from src.db.schema import connect
from src.ingestion import companyfacts, prices
from tests.test_ingest import company, count, fact
from tests.test_prices import FakeYahoo, seed, yahoo


def price_rows(con, cik):
    return con.execute("SELECT price_date, close_raw FROM prices WHERE cik = ? ORDER BY 1", [cik]).fetchall()


def test_empty_refresh_window_keeps_existing_prices():
    con = connect(":memory:")
    seed(con)
    googl = yahoo([("2026-10-28", 170.0, 1e6, 0.0, 0.0), ("2026-10-29", 171.0, 1e6, 0.0, 0.0)])
    nvda = yahoo([("2026-10-01", 190.0, 1e6, 0.0, 0.0), ("2026-10-02", 191.0, 1e6, 0.0, 0.0)])
    fake = FakeYahoo({"GOOGL": googl, "NVDA": nvda})
    prices.load(con, pause_s=0, download=fake)
    before = price_rows(con, 1)
    # Refresh batch spans NVDA's earlier window; GOOGL has nothing in its own window.
    fake.frames["GOOGL"] = yahoo([("2026-09-01", 150.0, 1e6, 0.0, 0.0)])
    prices.load(con, pause_s=0, download=fake)
    assert price_rows(con, 1) == before
    assert con.execute("SELECT status FROM price_status WHERE cik = 1").fetchall() == [("ok",)]


def test_refresh_removes_splits_and_dividends_yahoo_dropped():
    con = connect(":memory:")
    seed(con)
    with_actions = yahoo([("2026-10-26", 100.0, 1e6, 0.0, 0.0), ("2026-10-28", 50.0, 2e6, 0.0, 2.0),
                          ("2026-10-29", 51.0, 2e6, 0.25, 0.0)])
    fake = FakeYahoo({"NVDA": with_actions})
    prices.load(con, tickers=["NVDA"], pause_s=0, download=fake)
    assert con.execute("SELECT count(*) FROM splits").fetchall()[0][0] == 1
    fake.frames["NVDA"] = yahoo([("2026-10-26", 100.0, 1e6, 0.0, 0.0), ("2026-10-28", 101.0, 1e6, 0.0, 0.0),
                                 ("2026-10-29", 102.0, 1e6, 0.0, 0.0)])
    prices.load(con, tickers=["NVDA"], pause_s=0, download=fake)
    assert con.execute("SELECT count(*) FROM splits").fetchall()[0][0] == 0
    assert con.execute("SELECT count(*) FROM dividends").fetchall()[0][0] == 0


def test_failed_refresh_marks_error_but_keeps_coverage_dates():
    con = connect(":memory:")
    seed(con)
    prices.load(con, tickers=["NVDA"], pause_s=0,
                download=FakeYahoo({"NVDA": yahoo([("2026-10-28", 100.0, 1e6, 0.0, 0.0)])}))

    def down(tickers, start):
        raise RuntimeError("Too Many Requests")

    prices.load(con, tickers=["NVDA"], pause_s=0, download=down)
    assert con.execute("SELECT status, first_date, last_date FROM price_status WHERE cik = 2").fetchall() == \
        [("error", date(2026, 10, 28), date(2026, 10, 28))]


def m0_database(path: str, doc: dict) -> None:
    m0 = duckdb.connect(path)
    m0.execute("""CREATE TABLE xbrl_facts (
        cik INTEGER NOT NULL, taxonomy VARCHAR NOT NULL, tag VARCHAR NOT NULL, unit VARCHAR NOT NULL,
        period_start DATE NOT NULL, period_end DATE NOT NULL, period_days INTEGER NOT NULL,
        period_kind VARCHAR NOT NULL, fy INTEGER, fp VARCHAR, form VARCHAR NOT NULL, accn VARCHAR NOT NULL,
        filed DATE NOT NULL, frame VARCHAR, value DOUBLE NOT NULL,
        PRIMARY KEY (cik, taxonomy, tag, unit, period_start, period_end, accn))""")
    m0.executemany("INSERT INTO xbrl_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   list(dict.fromkeys(companyfacts.iter_fact_rows(doc))))
    m0.close()


def snapshot(frame: str | None, extra: bool) -> dict:
    d = {"cik": 1, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {**fact("2023-01-01", "2023-12-31", 100.0, "2024-02-15", "a1"), "frame": frame}]}}}}}
    if extra:
        d["facts"]["us-gaap"]["OperatingIncomeLoss"] = {"units": {"USD": [
            fact("2023-01-01", "2023-12-31", 42.0, "2024-02-15", "a1")]}}
    return d


def test_m0_filing_that_grows_and_changes_frame_is_replaced(tmp_path):
    db = str(tmp_path / "m0.duckdb")
    m0_database(db, snapshot(None, extra=False))
    con = connect(db)
    s = companyfacts.load_docs(con, [snapshot("CY2023", extra=True)])
    assert s.filings_changed == 1
    assert con.execute("SELECT tag, frame FROM xbrl_facts ORDER BY tag").fetchall() == \
        [("OperatingIncomeLoss", None), ("Revenues", "CY2023")]


def test_corrected_value_with_same_fact_count_is_replaced():
    con = connect(":memory:")
    companyfacts.load_docs(con, [company(1, ["a1"])])
    fixed = company(1, ["a1"])
    for f in fixed["facts"]["us-gaap"]["Revenues"]["units"]["USD"]:
        f["val"] = 99.0 if f["form"] == "10-K" else f["val"]
    s = companyfacts.load_docs(con, [fixed])
    assert s.filings_changed == 1
    assert con.execute("SELECT value FROM xbrl_facts").fetchall() == [(99.0,)]
    assert companyfacts.load_docs(con, [fixed]).filings_changed == 0
    assert count(con) == 1


def test_frame_moving_between_filings_does_not_rewrite_filing():
    con = connect(":memory:")
    companyfacts.load_docs(con, [snapshot("CY2023", extra=False)])
    s = companyfacts.load_docs(con, [snapshot(None, extra=False)])   # SEC moved the frame to a newer filing
    assert (s.filings_changed, s.facts_inserted) == (0, 0)
