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

import ast
import gc
import logging
import re
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


TICKER_RETRIES = 3          # a ticker yfinance failed is fetched again this many times, alone
# yfinance's own words for "this ticker has no prices": not an error, nothing to retry.
_NO_DATA = ("possibly delisted", "no price data found", "no data found")
_FAILED = re.compile(r"\[([^\]]*)\]:\s*(.+)$")


class TickerError(Exception):
    """One ticker's download failed (yfinance logged an exception for it and returned no frame)."""


class _Failures(logging.Handler):
    """Collects the per-ticker errors yfinance.download logs (`['A', 'B']: <error>`) instead of
    raising: without them a failed ticker looks the same as a ticker with no prices."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.errors: dict[str, str] = {}

    def emit(self, record):
        m = _FAILED.search(record.getMessage())
        if not m or any(h in m.group(2).lower() for h in _NO_DATA):
            return
        try:
            syms = ast.literal_eval(f"[{m.group(1)}]")
        except (ValueError, SyntaxError):
            return
        for t in syms:
            self.errors[str(t).upper()] = m.group(2).strip()


def _download(tickers: list[str], start: date) -> dict[str, pd.DataFrame | TickerError]:
    """Frames by ticker; a ticker whose download raised inside yfinance maps to a TickerError."""
    import yfinance as yf

    failures, log = _Failures(), logging.getLogger("yfinance")
    log.addHandler(failures)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # yfinance trips pandas 3 deprecation warnings
            df = yf.download(tickers, start=start.isoformat(), auto_adjust=False, actions=True,
                             group_by="ticker", progress=False, threads=True, multi_level_index=True,
                             timeout=30)
    finally:
        log.removeHandler(failures)
    out: dict[str, pd.DataFrame | TickerError] = {
        t: TickerError(failures.errors[t]) for t in tickers if t.upper() in failures.errors}
    if df is None or df.empty:
        return out
    present = set(df.columns.get_level_values(0))
    for t in tickers:
        if t in present and t not in out:
            sub = df[t]
            if sub["Close"].notna().any():
                out[t] = sub
    return out


def _download_with_retry(tickers: list[str], start: date, retries: int = 4) -> dict[str, pd.DataFrame | TickerError]:
    for attempt in range(retries):
        try:
            frames = _download(tickers, start)
            break
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(30 * 2 ** attempt)   # Yahoo rate limits: back off hard
    else:
        return {}
    for attempt in range(TICKER_RETRIES):
        failed = [t for t, v in frames.items() if isinstance(v, TickerError)]
        if not failed:
            break
        gc.collect()        # yfinance leaves a cache connection open per finished thread
        time.sleep(5 * 2 ** attempt)
        frames.update(_download(failed, start))
    return frames


PRICE_SCHEMA = pa.schema([("cik", pa.int32()), ("ticker", pa.string()), ("price_date", pa.date32()),
                          ("close_raw", pa.float64()), ("volume_raw", pa.float64()), ("source", pa.string())])


def _store(con: duckdb.DuckDBPyConnection, batch: list[tuple[int, str, date]],
           frames: dict[str, pd.DataFrame], stats: PriceStats) -> None:
    """Replace each company's prices, splits and dividends inside its refresh window.

    Only companies that returned rows for their own window are replaced: an empty
    window (no trading that week, or a batch start earlier than the company's) must
    not delete what is stored. Yahoo's view of the window is authoritative, so a
    split or dividend it no longer reports is removed with the window.
    """
    now = datetime.now()
    prices: dict[str, list] = {f.name: [] for f in PRICE_SCHEMA}
    windows, splits, divs, status, touched = [], [], [], [], []
    for cik, ticker, start in batch:
        refresh = start != HISTORY_START
        df = frames.get(ticker)
        raw, ratios = to_raw(df) if df is not None else (None, None)
        if raw is None or raw.empty:
            if refresh and df is not None:
                touched.append(("no rows in refresh window", now, cik))  # keep status and dates
                continue
            reason = "no rows from source" if df is None else "no closes"
            status.append((cik, ticker, "no_data", None, None, reason, now))
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
        windows.append((cik, start))
        splits += [(cik, d.date(), float(r), SOURCE) for d, r in ratios.items()]
        divs += [(cik, d.date(), float(v), SOURCE) for d, v in raw["dividend_raw"].items() if v > 0]
        status.append((cik, ticker, "ok", days[0], days[-1], None, now))
        stats.priced += 1
        stats.price_rows += n
        stats.splits += len(ratios)

    con.begin()
    try:
        if windows:
            con.register("windows", pa.table({"cik": pa.array([c for c, _ in windows], pa.int32()),
                                              "start": pa.array([d for _, d in windows], pa.date32())}))
            # One set-based delete per table and batch: a DELETE per company would rescan prices.
            con.execute("DELETE FROM prices USING windows w WHERE prices.cik = w.cik AND prices.price_date >= w.start")
            for table in ("splits", "dividends"):
                con.execute(f"""DELETE FROM {table} USING windows w WHERE {table}.cik = w.cik
                                AND {table}.ex_date >= w.start AND {table}.source = '{SOURCE}'""")
            con.unregister("windows")
            con.register("price_rows", pa.Table.from_pydict(prices, schema=PRICE_SCHEMA))
            con.execute("INSERT INTO prices SELECT * FROM price_rows")
            con.unregister("price_rows")
        if splits:
            con.executemany("INSERT INTO splits VALUES (?, ?, ?, ?)", splits)
        if divs:
            con.executemany("INSERT INTO dividends VALUES (?, ?, ?, ?)", divs)
        if status:
            # Keep the earliest first_date seen: a refresh window starts late.
            con.executemany("""
                INSERT INTO price_status VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cik) DO UPDATE SET ticker = excluded.ticker, status = excluded.status,
                    first_date = least(price_status.first_date, excluded.first_date),
                    last_date = coalesce(excluded.last_date, price_status.last_date),
                    message = excluded.message, fetched_at = excluded.fetched_at
            """, status)
        if touched:
            con.executemany("UPDATE price_status SET message = ?, fetched_at = ? WHERE cik = ?", touched)
        con.commit()
    except BaseException:
        con.rollback()
        raise


def _mark_error(con: duckdb.DuckDBPyConnection, failed: list[tuple[int, str, Exception]]) -> None:
    """status says the last attempt failed; first/last_date keep the stored coverage."""
    con.executemany("""INSERT INTO price_status VALUES (?, ?, 'error', NULL, NULL, ?, ?)
                       ON CONFLICT (cik) DO UPDATE SET status = 'error',
                       message = excluded.message, fetched_at = excluded.fetched_at""",
                    [(cik, t, (str(e) if isinstance(e, TickerError) else repr(e))[:300], datetime.now())
                     for cik, t, e in failed])


def load(con: duckdb.DuckDBPyConnection, exchanges: tuple[str, ...] = EXCHANGES,
         tickers: list[str] | None = None, batch_size: int = 50, pause_s: float = 1.0,
         progress: Callable[[PriceStats], None] | None = None,
         download: Callable[[list[str], date], dict[str, pd.DataFrame | TickerError]] = _download_with_retry,
         ) -> PriceStats:
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
            _mark_error(con, [(cik, t, e) for cik, t, _ in batch])
            continue
        failed = {t: v for t, v in frames.items() if isinstance(v, TickerError)}
        if failed:
            _mark_error(con, [(cik, t, failed[t]) for cik, t, _ in batch if t in failed])
            stats.errors += len(failed)
            batch = [b for b in batch if b[1] not in failed]
        # A batch downloads from its earliest start; trim each company to its own window.
        frames = {t: df[df.index >= pd.Timestamp(s)] for t, df in frames.items() if t not in failed
                  for _, tk, s in batch if tk == t}
        _store(con, batch, frames, stats)
        if progress:
            progress(stats)
        if pause_s and i + batch_size < len(todo):
            time.sleep(pause_s)
    stats.seconds = time.monotonic() - t0
    return stats
