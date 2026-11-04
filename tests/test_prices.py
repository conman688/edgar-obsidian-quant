"""M2 prices: raw-price rebuild from Yahoo's split-adjusted data, incremental refresh, coverage."""
from datetime import date

import pandas as pd
import pytest

from src.db.schema import connect
from src.ingestion import prices


def yahoo(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    """(date, adjusted close, adjusted volume, dividend, split ratio) rows, as yfinance returns them."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows])
    return pd.DataFrame({"Close": [r[1] for r in rows], "Volume": [r[2] for r in rows],
                         "Dividends": [r[3] for r in rows], "Stock Splits": [r[4] for r in rows]}, index=idx)


NVDA = yahoo([  # real yfinance values around the 2024-06-10 10-for-1 split
    ("2024-06-06", 120.998, 664_696_000, 0.0, 0.0),
    ("2024-06-07", 120.888, 412_386_000, 0.0, 0.0),
    ("2024-06-10", 121.790, 313_434_100, 0.0, 10.0),
    ("2024-06-11", 120.910, 222_551_200, 0.01, 0.0),
])


def test_to_raw_undoes_split_adjustment():
    raw, ratios = prices.to_raw(NVDA)
    assert raw.loc["2024-06-07", "close_raw"] == pytest.approx(1208.88)
    assert raw.loc["2024-06-07", "volume_raw"] == pytest.approx(41_238_600)
    assert raw.loc["2024-06-10", "close_raw"] == pytest.approx(121.79)   # split day is post-split
    assert raw.loc["2024-06-11", "dividend_raw"] == pytest.approx(0.01)
    assert list(ratios.items()) == [(pd.Timestamp("2024-06-10"), 10.0)]


def test_to_raw_compounds_multiple_splits():
    df = yahoo([("2020-01-02", 5.0, 400.0, 0.0, 0.0), ("2021-07-20", 6.0, 100.0, 0.0, 4.0),
                ("2022-01-03", 7.0, 50.0, 0.0, 0.0), ("2024-06-10", 8.0, 10.0, 0.0, 10.0)])
    raw, _ = prices.to_raw(df)
    assert raw["close_raw"].tolist() == pytest.approx([200.0, 60.0, 70.0, 8.0])
    assert raw["volume_raw"].tolist() == pytest.approx([10.0, 10.0, 5.0, 10.0])


def seed(con):
    con.executemany("INSERT INTO tickers VALUES (?, ?, ?, ?, DATE '2026-11-01')", [
        (1, "GOOGL", "Alphabet", "Nasdaq"), (1, "GOOG", "Alphabet", "Nasdaq"),
        (2, "NVDA", "Nvidia", "Nasdaq"), (3, "JPM", "JPMorgan", "NYSE"),
        (4, "GONE", "Delisted Co", "NYSE"), (5, "OTCX", "Pink Co", "OTC")])
    con.executemany("INSERT INTO companies VALUES (?, ?, ?, NULL, NULL, NULL, NULL, [], ?, ?, FALSE)", [
        (1, "Alphabet", 7370, ["GOOGL", "GOOG"], False), (2, "Nvidia", 3674, ["NVDA"], False),
        (3, "JPMorgan", 6021, ["JPM"], True), (4, "Delisted Co", 3674, ["GONE"], False),
        (5, "Pink Co", 3674, ["OTCX"], False)])
    con.executemany("INSERT INTO loaded_filings VALUES (?, ?, 1)", [(c, f"a{c}") for c in range(1, 6)])


class FakeYahoo:
    def __init__(self, frames):
        self.frames, self.calls = frames, []

    def __call__(self, tickers, start):
        self.calls.append((sorted(tickers), start))
        return {t: df[df.index >= pd.Timestamp(start)] for t, df in self.frames.items() if t in tickers}


def test_universe_picks_primary_ticker_and_skips_financials_and_otc():
    con = connect(":memory:")
    seed(con)
    assert [(c, t) for c, t, _ in prices.universe(con)] == [(4, "GONE"), (1, "GOOGL"), (2, "NVDA")]
    assert [t for _, t, _ in prices.universe(con, exchanges=("NYSE", "Nasdaq", "OTC"))] == \
        ["GONE", "GOOGL", "NVDA", "OTCX"]
    assert [t for _, t, _ in prices.universe(con, tickers=["goog", "jpm"])] == ["GOOG", "JPM"]  # explicit wins


def test_load_then_refresh_is_idempotent_and_tracks_coverage():
    con = connect(":memory:")
    seed(con)
    googl = yahoo([("2026-10-28", 170.0, 1e6, 0.0, 0.0), ("2026-10-29", 171.0, 1e6, 0.0, 0.0)])
    fake = FakeYahoo({"NVDA": NVDA, "GOOGL": googl})
    s = prices.load(con, pause_s=0, download=fake)
    assert (s.companies, s.priced, s.no_data, s.splits) == (3, 2, 1, 1) and s.missing == ["GONE"]
    assert fake.calls[0][1] == prices.HISTORY_START
    rows = lambda: con.execute("SELECT count(*) FROM prices").fetchall()[0][0]
    assert rows() == 6
    assert con.execute("SELECT close_raw FROM prices WHERE cik = 2 AND price_date = DATE '2024-06-07'"
                       ).fetchall()[0][0] == pytest.approx(1208.88)
    assert con.execute("SELECT ratio FROM splits WHERE cik = 2").fetchall() == [(10.0,)]
    assert con.execute("SELECT amount_raw FROM dividends WHERE cik = 2").fetchall()[0][0] == pytest.approx(0.01)

    # Refresh: Yahoo corrects one close and adds a day. Only the overlap window is replaced.
    fake.frames["GOOGL"] = yahoo([("2026-10-28", 170.0, 1e6, 0.0, 0.0), ("2026-10-29", 171.5, 1e6, 0.0, 0.0),
                                  ("2026-10-30", 172.0, 1e6, 0.0, 0.0)])
    prices.load(con, pause_s=0, download=fake)
    assert rows() == 7
    assert con.execute("SELECT close_raw FROM prices WHERE cik = 1 ORDER BY price_date DESC LIMIT 1"
                       ).fetchall()[0][0] == 172.0
    status = dict(con.execute("SELECT ticker, status FROM price_status").fetchall())
    assert status == {"GOOGL": "ok", "NVDA": "ok", "GONE": "no_data"}
    first = con.execute("SELECT first_date FROM price_status WHERE cik = 2").fetchall()[0][0]
    assert first == date(2024, 6, 6)                       # refresh never moves first_date later


def test_failed_batch_is_recorded_and_others_continue():
    con = connect(":memory:")
    seed(con)

    def flaky(tickers, start):
        if "GOOGL" in tickers:
            raise RuntimeError("Too Many Requests")
        return {"NVDA": NVDA}

    s = prices.load(con, batch_size=1, pause_s=0, download=flaky)
    assert s.errors == 1 and s.priced == 1
    assert dict(con.execute("SELECT ticker, status FROM price_status").fetchall())["GOOGL"] == "error"
