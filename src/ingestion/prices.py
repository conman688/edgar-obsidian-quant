"""Daily prices, splits and dividends from Yahoo Finance (via yfinance).

Yahoo's Close, Volume and Dividends are split-adjusted back in time to today's
share basis. We store raw values, as printed on the day:

    close_raw  = close_adj  x F(d)        F(d) = product of split ratios with ex_date > d
    volume_raw = volume_adj / F(d)
    div_raw    = div_adj    x F(d)

A refresh only needs the splits inside its own window, because every split after
day d and up to today lies between d and today.

yfinance is unofficial and can break or rate-limit; everything Yahoo-specific
stays in this module.
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

import duckdb
import pandas as pd
import pyarrow as pa

SOURCE = "yahoo"
HISTORY_START = date(2010, 1, 1)    # 52-week highs for a 2011+ backtest
REFRESH_OVERLAP = timedelta(days=7)  # re-read the last week to catch late corrections
EXCHANGES = ("NYSE", "Nasdaq")


@dataclass
class PriceStats:
    companies: int = 0
    priced: int = 0
    no_data: int = 0
    errors: int = 0
    price_rows: int = 0
    splits: int = 0
    seconds: float = 0.0
    missing: list[str] = field(default_factory=list)


def universe(con: duckdb.DuckDBPyConnection, exchanges: tuple[str, ...] = EXCHANGES,
             tickers: list[str] | None = None) -> list[tuple[int, str, date | None]]:
    """(cik, primary ticker, last stored price date) for operating companies with facts.

    Primary ticker = the company's first-listed ticker (SEC order puts the main common
    class first, e.g. GOOGL before GOOG) that trades on one of `exchanges`. Financials
    and shells are skipped: the screens exclude them. A changed primary ticker
    returns last_date None so the full history is fetched again.
    """
    where = "t.ticker IN (SELECT unnest($tickers))" if tickers else \
        "t.exchange IN (SELECT unnest($exchanges)) AND NOT c.is_financial AND NOT c.is_shell"
    rows = con.execute(f"""
        SELECT t.cik, t.ticker, CASE WHEN ps.ticker = t.ticker THEN ps.last_date END
        FROM tickers t
        JOIN companies c ON c.cik = t.cik
        LEFT JOIN price_status ps ON ps.cik = t.cik
        WHERE {where} AND t.cik IN (SELECT cik FROM loaded_filings)
        QUALIFY row_number() OVER (
            PARTITION BY t.cik ORDER BY coalesce(list_position(c.tickers, t.ticker), 999), t.ticker) = 1
        ORDER BY 3 NULLS FIRST, 2
    """, {"tickers": [x.upper() for x in tickers]} if tickers else {"exchanges": list(exchanges)}).fetchall()
    return rows


def to_raw(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Undo Yahoo's split adjustment for one ticker.

    `df` is indexed by date with Close, Volume, Dividends and Stock Splits columns.
    Returns (frame with close_raw, volume_raw, dividend_raw; split ratios by ex_date).
    """
    df = df[df["Close"].notna()]
    ratios = df["Stock Splits"].fillna(0.0)
    ratios = ratios[ratios > 0]
    factor = pd.Series(1.0, index=df.index)
    for ex_date, ratio in ratios.items():
        factor = factor.where(df.index >= ex_date, factor * ratio)
    out = pd.DataFrame({
        "close_raw": df["Close"] * factor,
        "volume_raw": df["Volume"] / factor,
        "dividend_raw": df["Dividends"].fillna(0.0) * factor,
    }, index=df.index)
    return out, ratios


def _download(tickers: list[str], start: date) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # yfinance trips pandas 3 deprecation warnings
        df = yf.download(tickers, start=start.isoformat(), auto_adjust=False, actions=True,
                         group_by="ticker", progress=False, threads=True, multi_level_index=True,
                         timeout=30)
    out = {}
    if df is None or df.empty:
        return out
    present = set(df.columns.get_level_values(0))
    for t in tickers:
        if t in present:
            sub = df[t]
            if sub["Close"].notna().any():
                out[t] = sub
    return out


def _download_with_retry(tickers: list[str], start: date, retries: int = 4) -> dict[str, pd.DataFrame]:
    for attempt in range(retries):
        try:
            return _download(tickers, start)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(30 * 2 ** attempt)   # Yahoo rate limits: back off hard
    return {}


PRICE_SCHEMA = pa.schema([("cik", pa.int32()), ("ticker", pa.string()), ("price_date", pa.date32()),
                          ("close_raw", pa.float64()), ("volume_raw", pa.float64()), ("source", pa.string())])


def _store(con: duckdb.DuckDBPyConnection, batch: list[tuple[int, str, date]],
           frames: dict[str, pd.DataFrame], stats: PriceStats) -> None:
    """Replace each company's rows from its window start onward, in one transaction."""
    now = datetime.now()
    prices: dict[str, list] = {f.name: [] for f in PRICE_SCHEMA}
    splits, divs, status = [], [], []
    for cik, ticker, start in batch:
        df = frames.get(ticker)
        if df is None:
            status.append((cik, ticker, "no_data", None, None, "no rows from source", now))
            stats.no_data += 1
            stats.missing.append(ticker)
            continue
        raw, ratios = to_raw(df)
        if raw.empty:
            status.append((cik, ticker, "no_data", None, None, "no closes", now))
            stats.no_data += 1
            stats.missing.append(ticker)
            continue
        days = [d.date() for d in raw.index]
        n = len(days)
        prices["cik"] += [cik] * n
        prices["ticker"] += [ticker] * n
        prices["price_date"] += days
        prices["close_raw"] += raw["close_raw"].tolist()
        prices["volume_raw"] += [None if pd.isna(v) else float(v) for v in raw["volume_raw"]]
        prices["source"] += [SOURCE] * n
        splits += [(cik, d.date(), float(r), SOURCE) for d, r in ratios.items()]
        divs += [(cik, d.date(), float(v), SOURCE) for d, v in raw["dividend_raw"].items() if v > 0]
        status.append((cik, ticker, "ok", days[0], days[-1], None, now))
        stats.priced += 1
        stats.price_rows += n
        stats.splits += len(ratios)

    table = pa.Table.from_pydict(prices, schema=PRICE_SCHEMA)
    windows = [(cik, start) for cik, ticker, start in batch if ticker in frames]
    con.begin()
    try:
        if windows:
            # One set-based delete per batch: a DELETE per company would scan prices each time.
            con.register("windows", pa.table({"cik": pa.array([c for c, _ in windows], pa.int32()),
                                              "start": pa.array([d for _, d in windows], pa.date32())}))
            con.execute("DELETE FROM prices USING windows w WHERE prices.cik = w.cik AND prices.price_date >= w.start")
            con.unregister("windows")
            con.register("price_rows", table)
            con.execute("INSERT INTO prices SELECT * FROM price_rows")
            con.unregister("price_rows")
        if splits:
            con.executemany("INSERT OR REPLACE INTO splits VALUES (?, ?, ?, ?)", splits)
        if divs:
            con.executemany("INSERT OR REPLACE INTO dividends VALUES (?, ?, ?, ?)", divs)
        # Keep the earliest first_date seen: a refresh window starts late.
        con.executemany("""
            INSERT INTO price_status VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (cik) DO UPDATE SET ticker = excluded.ticker, status = excluded.status,
                first_date = least(price_status.first_date, excluded.first_date),
                last_date = coalesce(excluded.last_date, price_status.last_date),
                message = excluded.message, fetched_at = excluded.fetched_at
        """, status)
        con.commit()
    except BaseException:
        con.rollback()
        raise


def load(con: duckdb.DuckDBPyConnection, exchanges: tuple[str, ...] = EXCHANGES,
         tickers: list[str] | None = None, batch_size: int = 50, pause_s: float = 1.0,
         progress: Callable[[PriceStats], None] | None = None,
         download: Callable[[list[str], date], dict[str, pd.DataFrame]] = _download_with_retry) -> PriceStats:
    """Fetch new companies from HISTORY_START and refresh known ones from their last date.

    Each batch commits on its own, so an interrupted run resumes where it stopped.
    """
    t0, stats = time.monotonic(), PriceStats()
    todo = universe(con, exchanges, tickers)
    stats.companies = len(todo)
    for i in range(0, len(todo), batch_size):
        chunk = todo[i:i + batch_size]
        batch = [(cik, t, (last - REFRESH_OVERLAP) if last else HISTORY_START) for cik, t, last in chunk]
        start = min(s for _, _, s in batch)
        try:
            frames = download([t for _, t, _ in batch], start)
        except Exception as e:
            stats.errors += len(batch)
            con.executemany("""INSERT INTO price_status VALUES (?, ?, 'error', NULL, NULL, ?, ?)
                               ON CONFLICT (cik) DO UPDATE SET message = excluded.message,
                               fetched_at = excluded.fetched_at""",
                            [(cik, t, repr(e)[:300], datetime.now()) for cik, t, _ in batch])
            continue
        # A batch downloads from its earliest start; trim each company to its own window.
        frames = {t: df[df.index >= pd.Timestamp(s)] for t, df in frames.items()
                  for _, tk, s in batch if tk == t}
        _store(con, batch, frames, stats)
        if progress:
            progress(stats)
        if pause_s and i + batch_size < len(todo):
            time.sleep(pause_s)
    stats.seconds = time.monotonic() - t0
    return stats
