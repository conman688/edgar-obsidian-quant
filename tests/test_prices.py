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


def _scripted(con, answers):
    """prices.load with the real _download_with_retry and `_download` answering from `answers`:
    each item is a dict to return or an exception to raise, in call order."""
    answers, calls = list(answers), []

    def fake(tickers, start):
        calls.append(sorted(tickers))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return {t: v for t, v in a.items() if t in tickers}

    with patch("src.ingestion.prices._download", fake), patch("src.ingestion.prices.time.sleep"):
        stats = prices.load(con, tickers=["GOOGL", "NVDA"], pause_s=0)
    return stats, calls


def test_retry_that_raises_keeps_the_healthy_tickers_of_the_first_answer():     # Codex, pre-handoff
    con = _seeded_refresh()
    err = prices.TickerError("OperationalError('unable to open database file')")
    stats, calls = _scripted(con, [{"GOOGL": FRESH, "NVDA": err}] + [RuntimeError("Too Many Requests")] * 3)
    assert calls == [["GOOGL", "NVDA"], ["NVDA"], ["NVDA"], ["NVDA"]]
    assert (stats.priced, stats.no_data, stats.errors) == (1, 0, 1)
    assert _status(con, "GOOGL")[0] == "ok" and _status(con, "NVDA")[0] == "error"
    assert "Too Many Requests" in _status(con, "NVDA")[1]


def test_retry_that_finds_no_data_replaces_the_old_error():                    # Codex, pre-handoff
    con = connect(":memory:")
    seed(con)
    err = prices.TickerError("OperationalError('unable to open database file')")
    stats, calls = _scripted(con, [{"GOOGL": FRESH, "NVDA": err}, {}])
    assert calls == [["GOOGL", "NVDA"], ["NVDA"]]
    assert (stats.priced, stats.no_data, stats.errors) == (1, 1, 0)
    assert _status(con, "NVDA")[0] == "no_data"


# --- M9 C5 (2026-11-07): actions on source rows without a close -----------------------------
# yfinance 1.7.0 (keepna=False) keeps a row whose Close is NaN when it carries a dividend or a
# split, and appends such rows for events outside the price range. Actions are kept on every
# source row; prices only where there is a close; a stored close is never deleted by a source
# row that has none.
NAN = float("nan")
DIVS = "SELECT cik, ex_date, amount_raw FROM dividends ORDER BY 1, 2"
PRICES = "SELECT cik, price_date, close_raw FROM prices ORDER BY 1, 2"
ORIGINAL = [("2026-11-02", 10.0, 100.0, 0.0, 0.0), ("2026-11-03", 10.0, 100.0, 0.25, 0.0),
            ("2026-11-04", 11.0, 100.0, 0.0, 0.0)]


def _c5_status(con, ticker):
    return con.execute("SELECT status, first_date, last_date, message FROM price_status WHERE ticker = ?",
                       [ticker]).fetchall()[0]


def _c5_seeded():
    """GOOGL and NVDA loaded once from healthy frames (Codex's C5 seed)."""
    con = connect(":memory:")
    seed(con)
    healthy = yahoo([("2026-11-02", 50.0, 10.0, 0.0, 0.0), ("2026-11-04", 51.0, 10.0, 0.0, 0.0)])
    prices.load(con, tickers=["GOOGL", "NVDA"], pause_s=0,
                download=lambda tickers, start: {"GOOGL": yahoo(ORIGINAL), "NVDA": healthy})
    return con, healthy


def _refresh(con, googl, nvda):
    return prices.load(con, tickers=["GOOGL", "NVDA"], pause_s=0,
                       download=lambda tickers, start: {"GOOGL": googl, "NVDA": nvda})


def test_refresh_keeps_a_dividend_reported_on_a_day_without_a_close():              # Codex C5
    con, healthy = _c5_seeded()
    before_divs, before_prices = con.execute(DIVS).fetchall(), con.execute(PRICES).fetchall()
    assert before_divs == [(1, date(2026, 11, 3), 0.25)]
    no_close = yahoo([ORIGINAL[0], ("2026-11-03", NAN, 0.0, 0.25, 0.0), ORIGINAL[2]])
    for _ in range(2):                                                              # idempotent
        s = _refresh(con, no_close, healthy)
        assert (s.priced, s.no_data, s.errors) == (2, 0, 0)
        assert con.execute(DIVS).fetchall() == before_divs
        assert con.execute(PRICES).fetchall() == before_prices      # stored Nov 3 close kept, no NaN row
    assert _c5_status(con, "GOOGL")[:3] == ("ok", date(2026, 11, 2), date(2026, 11, 4))
    assert _c5_status(con, "NVDA")[:3] == ("ok", date(2026, 11, 2), date(2026, 11, 4))


def test_refresh_still_removes_and_updates_dividends_the_source_changed():         # C5 control
    con, healthy = _c5_seeded()
    removed = yahoo([ORIGINAL[0], ("2026-11-03", 10.0, 100.0, 0.0, 0.0), ORIGINAL[2]])
    _refresh(con, removed, healthy)
    assert con.execute(DIVS).fetchall() == []
    changed = yahoo([ORIGINAL[0], ("2026-11-03", NAN, 0.0, 0.30, 0.0), ORIGINAL[2]])
    _refresh(con, changed, healthy)
    assert con.execute(DIVS).fetchall() == [(1, date(2026, 11, 3), 0.30)]


def test_split_on_a_day_without_a_close_adjusts_earlier_prices_and_is_stored():    # C5 control
    con = connect(":memory:")
    seed(con)
    df = yahoo([("2026-11-02", 5.0, 200.0, 0.0, 0.0), ("2026-11-03", NAN, 0.0, 0.0, 2.0),
                ("2026-11-04", 6.0, 100.0, 0.1, 0.0)])
    prices.load(con, tickers=["GOOGL"], pause_s=0, download=lambda tickers, start: {"GOOGL": df})
    assert con.execute(PRICES).fetchall() == [(1, date(2026, 11, 2), 10.0), (1, date(2026, 11, 4), 6.0)]
    assert con.execute("SELECT cik, ex_date, ratio FROM splits").fetchall() == [(1, date(2026, 11, 3), 2.0)]
    assert con.execute(DIVS).fetchall() == [(1, date(2026, 11, 4), 0.1)]


def test_dividend_after_the_last_close_is_stored_without_a_price():                # C5 out-of-range
    con, healthy = _c5_seeded()
    later = yahoo(ORIGINAL + [("2026-11-06", NAN, 0.0, 0.40, 0.0)])     # yfinance's appended event row
    _refresh(con, later, healthy)
    assert con.execute(DIVS).fetchall() == [(1, date(2026, 11, 3), 0.25), (1, date(2026, 11, 6), 0.40)]
    assert con.execute("SELECT max(price_date) FROM prices WHERE cik = 1").fetchall() == [(date(2026, 11, 4),)]
    assert _c5_status(con, "GOOGL")[:3] == ("ok", date(2026, 11, 2), date(2026, 11, 4))


def test_window_with_no_closes_keeps_stored_history_and_says_so():                 # C5 control
    con, healthy = _c5_seeded()
    before = con.execute(DIVS).fetchall(), con.execute(PRICES).fetchall()
    blank = yahoo([("2026-11-03", NAN, 0.0, 0.30, 0.0), ("2026-11-04", NAN, 0.0, 0.0, 0.0)])
    s = _refresh(con, blank, healthy)
    assert (s.priced, s.no_data, s.errors) == (1, 0, 0)
    assert (con.execute(DIVS).fetchall(), con.execute(PRICES).fetchall()) == before
    status, first, last, message = _c5_status(con, "GOOGL")
    assert (status, first, last) == ("ok", date(2026, 11, 2), date(2026, 11, 4))   # window re-read next run
    assert "no closes" in message and "1 action" in message
