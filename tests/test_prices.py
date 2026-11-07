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
    con.executemany("INSERT INTO loaded_filings (cik, accn, n_facts) VALUES (?, ?, 1)", [(c, f"a{c}") for c in range(1, 6)])


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


# --- M9 C2 (2026-11-07): per-ticker errors that yfinance swallows ---------------------------
# yfinance.download() catches each ticker's exception, logs it and returns no frame for it.
# These tests go through the installed yfinance download path (no network, no cache): only its
# Ticker, session and YfData are replaced.

import sqlite3                                          # noqa: E402
from unittest.mock import patch                         # noqa: E402

from src import ops                                     # noqa: E402

FRESH = yahoo([("2026-10-30", 100.0, 10_000, 0.0, 0.0), ("2026-11-06", 101.0, 11_000, 0.0, 0.0)])


def _seeded_refresh():
    """GOOGL and NVDA priced once, so the next load is a refresh of both."""
    con = connect(":memory:")
    seed(con)
    prices.load(con, tickers=["GOOGL", "NVDA"], pause_s=0, download=lambda ts, start: {t: FRESH for t in ts})
    return con


def _through_yfinance(con, fail: dict[str, int], soft: dict[str, str] | None = None):
    """Run prices.load with the real _download_with_retry. `fail[t]` = how many calls of ticker t
    raise sqlite's 'unable to open database file'; `soft[t]` = a yfinance soft error (no data)."""
    calls: list[str] = []
    soft = soft or {}

    class History:
        def __init__(self, err):
            self._last_error = err

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker, self._price_history = ticker, None

        def history(self, **kwargs):
            calls.append(self.ticker)
            if fail.get(self.ticker, 0) > 0:
                fail[self.ticker] -= 1
                raise sqlite3.OperationalError("unable to open database file")
            if self.ticker in soft:
                self._price_history = History(f"${self.ticker}: {soft[self.ticker]}")
                return FRESH.iloc[0:0]
            return FRESH

    with patch("yfinance.multi.Ticker", FakeTicker), patch("yfinance.multi.new_session", return_value=object()), \
            patch("yfinance.multi.YfData"), patch("src.ingestion.prices.time.sleep") as backoff:
        stats = prices.load(con, tickers=["GOOGL", "NVDA"], pause_s=0)
    return stats, calls, backoff.call_count


def _status(con, ticker):
    return con.execute("SELECT status, message, last_date FROM price_status WHERE ticker = ?", [ticker]).fetchall()[0]


def test_swallowed_ticker_error_is_retried_then_reported_as_error():
    con = _seeded_refresh()
    before = con.execute("SELECT count(*) FROM prices WHERE cik = 2").fetchall()[0][0]
    stats, calls, backoffs = _through_yfinance(con, fail={"NVDA": 99})
    assert (stats.priced, stats.no_data, stats.errors) == (1, 0, 1)
    assert calls.count("NVDA") == 1 + prices.TICKER_RETRIES and calls.count("GOOGL") == 1
    assert backoffs == prices.TICKER_RETRIES
    status, message, last = _status(con, "NVDA")
    assert status == "error" and "unable to open database file" in message and last == date(2026, 11, 6)
    assert con.execute("SELECT count(*) FROM prices WHERE cik = 2").fetchall()[0][0] == before
    assert _status(con, "GOOGL")[0] == "ok"
    assert "1 companies failed their price refresh" in ops._warnings(con, date(2026, 11, 7), {}, {}, [], 0, 0, None)


def test_swallowed_ticker_error_that_clears_on_retry_is_priced():
    con = _seeded_refresh()
    stats, calls, backoffs = _through_yfinance(con, fail={"NVDA": 1})
    assert (stats.priced, stats.no_data, stats.errors) == (2, 0, 0)
    assert calls.count("NVDA") == 2 and backoffs == 1
    assert _status(con, "NVDA")[0] == "ok"


def test_yfinance_no_data_stays_no_data_and_is_not_retried():
    con = connect(":memory:")
    seed(con)
    stats, calls, backoffs = _through_yfinance(
        con, fail={}, soft={"NVDA": "possibly delisted; no price data found  (1d 2010-01-01 -> 2026-11-07)"})
    assert (stats.priced, stats.no_data, stats.errors) == (1, 1, 0)
    assert calls.count("NVDA") == 1 and backoffs == 0
    assert _status(con, "NVDA")[0] == "no_data"
