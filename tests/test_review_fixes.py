"""Regression tests for review findings: split direction on prices, tag priority vs filing order."""
from datetime import date

import pytest

from src.db.pit import concept
from src.db.queries import valuation
from src.db.schema import connect
from src.ingestion.companyfacts import load_docs
from tests.test_failure_modes import CIK, doc, fact


@pytest.fixture
def con():
    return connect(":memory:")


def test_52w_high_divides_pre_split_prices(con):
    load_docs(con, [doc({("dei", "EntityCommonStockSharesOutstanding", "shares"):
                         [fact(None, "2024-06-30", 1_000_000, "2024-07-01", "k")]})])
    con.executemany("INSERT INTO prices (cik, ticker, price_date, close_raw, source) VALUES (?, 'X', ?, ?, 't')",
                    [(CIK, date(2024, 6, 7), 1200.0), (CIK, date(2024, 7, 1), 125.0)])
    con.execute("INSERT INTO splits VALUES (?, DATE '2024-06-10', 10.0, 't')", [CIK])
    rel = valuation(con, date(2024, 7, 2))
    row = dict(zip(rel.columns, rel.fetchall()[0]))
    assert row["drawdown_52w"] == pytest.approx(0.0)   # 1200 pre-split = 120 post-split < 125 now


def test_first_reported_ignores_later_higher_priority_tag(con):
    load_docs(con, [doc({
        ("us-gaap", "Revenues", "USD"): [fact("2019-01-01", "2019-12-31", 100.0, "2020-02-01", "orig")],
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"):
            [fact("2019-01-01", "2019-12-31", 90.0, "2021-02-01", "later")],
    })])
    value = lambda as_of, policy: concept(con, "revenue", as_of, policy).fetchall()[0][7]
    assert value(date(2022, 1, 1), "as_first_reported") == 100.0
    assert value(date(2022, 1, 1), "as_last_known") == 90.0
    assert value(date(2020, 6, 1), "as_last_known") == 100.0
