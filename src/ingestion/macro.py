"""M12-A4: macro-regime overlay. FRED series as ALFRED published them, informational only.

Source (checked 2026-11-26; LINE.md "M12-A4 design"): the FRED REST API at api.stlouisfed.org
with a free key (FRED_API_KEY in .env). The key goes only to that host and never into an
error or log. fredapi is not used (unmaintained since 2024-05).

- NFCI (Chicago Fed, weekly, week ending Friday): revised every week. ALFRED vintages from
  2011-05-25, published on Wednesdays.
- T10Y2Y (10-year minus 2-year Treasury, daily): vintages from 2014-01-27, almost never revised.

Known-at is the vintage date (realtime_start). Observations older than a series' first vintage
carry that first vintage date, so nothing is known before ALFRED began recording the series.
One request holds at most 2,000 vintages, so requests are chunked. HY OAS (BAMLH0A0HYM2) is not
stored: ICE prohibits reproduction.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import duckdb
import pyarrow as pa
import requests

API = "https://api.stlouisfed.org/fred/"
USER_AGENT = "edgar-obsidian-quant research script"        # generic: never the SEC User-Agent
SERIES = ("T10Y2Y", "NFCI")
MAX_VINTAGES_PER_REQUEST = 1900                            # FRED refuses more than 2,000
PAGE = 100_000                                             # FRED's largest observations page
ROW_SCHEMA = pa.schema([("series_id", pa.string()), ("obs_date", pa.date32()), ("realtime_start", pa.date32()),
                        ("value", pa.float64()), ("fetched_at", pa.timestamp("us"))])


class FredError(RuntimeError):
    pass


class FredClient:
    """FRED's REST API, 1 request a second, generic User-Agent, key from FRED_API_KEY."""

    def __init__(self, rate_per_s: float = 1.0):
        self._key = os.environ.get("FRED_API_KEY", "").strip()
        if not self._key:
            raise FredError("Set FRED_API_KEY in .env (free key from fred.stlouisfed.org)")
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._interval, self._next = 1.0 / rate_per_s, 0.0

    def _get(self, path: str, **params) -> dict:
        wait = self._next - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next = max(self._next, time.monotonic()) + self._interval
        # Errors are fixed text: the endpoint and the status or exception class only. A response
        # body or a requests message can echo the URL, and so the key. A redirect is refused,
        # so the key's query string never follows it to another host.
        try:
            r = self._session.get(API + path, params={**params, "api_key": self._key, "file_type": "json"},
                                  timeout=120, allow_redirects=False)
        except requests.RequestException as e:
            raise FredError(f"FRED {path} {type(e).__name__}") from None
        if r.status_code != 200:
            raise FredError(f"FRED {path} HTTP {r.status_code}")
        return r.json()

    def vintage_dates(self, series: str) -> list[date]:
        out, offset = [], 0
        while True:
            d = self._get("series/vintagedates", series_id=series, limit=10_000, offset=offset)
            out += [date.fromisoformat(v) for v in d["vintage_dates"]]
            offset += len(d["vintage_dates"])
            if not d["vintage_dates"] or offset >= d["count"]:
                return out

    def observations(self, series: str, start: date, end: date) -> list[tuple[date, date, float | None]]:
        """(obs date, realtime_start, value) for every vintage in [start, end]; realtime_start
        is clipped to `start` by FRED."""
        out, offset = [], 0
        while True:
            d = self._get("series/observations", series_id=series, realtime_start=start.isoformat(),
                          realtime_end=end.isoformat(), limit=PAGE, offset=offset)
            out += [(date.fromisoformat(o["date"]), date.fromisoformat(o["realtime_start"]),
                     None if o["value"] == "." else float(o["value"])) for o in d["observations"]]
            offset += len(d["observations"])
            if not d["observations"] or offset >= d["count"]:
                return out


@dataclass
class FredStats:
    rows: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _insert_changes(con: duckdb.DuckDBPyConnection, series: str, rows: list[tuple], now: datetime) -> int:
    """Insert rows whose value differs from the value stored for that obs at the time: FRED
    repeats unchanged values at the start of every request."""
    if not rows:
        return 0
    cols = list(zip(*[(series, *r, now) for r in rows]))
    con.register("fred_rows", pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(cols, ROW_SCHEMA)],
                                                   schema=ROW_SCHEMA))
    try:
        before = con.execute("SELECT count(*) FROM macro_vintages").fetchall()[0][0]
        con.execute("""
            INSERT OR IGNORE INTO macro_vintages
            SELECT n.* FROM fred_rows n
            WHERE n.value IS DISTINCT FROM (
                SELECT m.value FROM macro_vintages m
                WHERE m.series_id = n.series_id AND m.obs_date = n.obs_date AND m.realtime_start < n.realtime_start
                ORDER BY m.realtime_start DESC LIMIT 1)
               OR NOT EXISTS (SELECT 1 FROM macro_vintages m WHERE m.series_id = n.series_id
                              AND m.obs_date = n.obs_date AND m.realtime_start < n.realtime_start)
        """)
        return con.execute("SELECT count(*) FROM macro_vintages").fetchall()[0][0] - before
    finally:
        con.unregister("fred_rows")


def load_fred(con: duckdb.DuckDBPyConnection, client) -> FredStats:
    """Fetch every vintage newer than the newest stored, per series in one transaction. A
    failing series is reported and retried whole next run; it never stops the others."""
    s = FredStats()
    for series in SERIES:
        last = con.execute("SELECT max(realtime_start) FROM macro_vintages WHERE series_id = ?",
                           [series]).fetchall()[0][0]
        try:
            vintages = [v for v in client.vintage_dates(series) if last is None or v > last]
            if not vintages:
                continue
            now = datetime.now()
            con.begin()
            try:
                added = 0
                for i in range(0, len(vintages), MAX_VINTAGES_PER_REQUEST):
                    chunk = vintages[i:i + MAX_VINTAGES_PER_REQUEST]
                    # Rows are inserted in vintage order so each is compared with the one before.
                    rows = sorted(client.observations(series, chunk[0], chunk[-1]), key=lambda r: (r[1], r[0]))
                    for v in sorted({r[1] for r in rows}):
                        added += _insert_changes(con, series, [r for r in rows if r[1] == v], now)
                con.commit()
                s.rows += added
            except BaseException:
                con.rollback()
                raise
        except (FredError, requests.RequestException, KeyError, ValueError) as e:
            s.failed.append(series)
            s.errors.append(str(e))
    return s


CBOE_URL = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/{index}_History.csv"
CBOE_INDEXES = ("VIX", "VIX9D", "VVIX")
CBOE_HEADERS = (("DATE", "OPEN", "HIGH", "LOW", "CLOSE"), ("DATE", "VVIX"))


class CboeError(RuntimeError):
    pass


class CboeClient:
    """Cboe's daily index history CSVs: public, no key, generic User-Agent, 1 request a second."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})

    def history(self, index: str) -> bytes:
        time.sleep(1)
        r = self._session.get(CBOE_URL.format(index=index), timeout=120)
        if r.status_code != 200:
            raise CboeError(f"Cboe {index} HTTP {r.status_code}")
        return r.content


def parse_cboe(payload: bytes) -> list[tuple[date, float]]:
    """(trade date, close) per row; the close is the last column. Refuses an unknown header."""
    lines = payload.decode("utf-8-sig").splitlines()
    header = tuple(h.strip().upper() for h in lines[0].split(",")) if lines else ()
    if header not in CBOE_HEADERS:
        raise CboeError(f"unexpected Cboe header: {header!r:.100}")
    out = []
    for n, line in enumerate(lines[1:], start=2):
        f = line.split(",")
        if len(f) != len(header):
            continue
        try:
            out.append((datetime.strptime(f[0].strip(), "%m/%d/%Y").date(), float(f[-1])))
        except ValueError as e:
            raise CboeError(f"Cboe line {n}: {e}") from e
    return out


@dataclass
class CboeStats:
    rows: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def load_cboe(con: duckdb.DuckDBPyConnection, client, now: datetime | None = None) -> CboeStats:
    """Insert closes not stored yet, known at max(trade date, fetch date). Stored closes are
    never changed. A failing index warns and is retried next run."""
    s = CboeStats()
    for index in CBOE_INDEXES:
        try:
            rows = parse_cboe(client.history(index))
            if not rows:
                raise CboeError(f"Cboe {index}: no rows")
        except (CboeError, requests.RequestException) as e:
            s.failed.append(index)
            s.errors.append(str(e) if isinstance(e, CboeError) else f"Cboe {index} {type(e).__name__}")
            continue
        fetched = now or datetime.now()
        before = con.execute("SELECT count(*) FROM cboe_daily").fetchall()[0][0]
        _insert_cboe(con, index, rows, fetched)
        s.rows += con.execute("SELECT count(*) FROM cboe_daily").fetchall()[0][0] - before
    return s


def _insert_cboe(con: duckdb.DuckDBPyConnection, index: str, rows: list[tuple[date, float]], fetched: datetime) -> None:
    schema = pa.schema([("index_id", pa.string()), ("trade_date", pa.date32()), ("close", pa.float64()),
                        ("known_at", pa.date32()), ("fetched_at", pa.timestamp("us"))])
    cols = list(zip(*[(index, d, c, max(d, fetched.date()), fetched) for d, c in rows]))
    con.register("cboe_rows", pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(cols, schema)], schema=schema))
    try:
        con.execute("INSERT OR IGNORE INTO cboe_daily SELECT * FROM cboe_rows")
    finally:
        con.unregister("cboe_rows")


def cboe_regime_sql() -> str:
    """Newest known close per index, and VIX9D/VIX on the newest trade date both are known."""
    return """
    WITH k AS (SELECT * FROM cboe_daily WHERE known_at <= $as_of AND trade_date <= $as_of)
    SELECT index_id, trade_date, close FROM k
    QUALIFY row_number() OVER (PARTITION BY index_id ORDER BY trade_date DESC) = 1
    UNION ALL
    SELECT 'VIX9D_VIX', a.trade_date, a.close / nullif(b.close, 0) FROM k a JOIN k b
        ON b.trade_date = a.trade_date AND a.index_id = 'VIX9D' AND b.index_id = 'VIX'
    QUALIFY row_number() OVER (ORDER BY a.trade_date DESC) = 1
    """


def macro_regime_sql() -> str:
    """Per series: the newest observation with a value as known on $as_of, with its vintage."""
    return """
    WITH v AS (
        SELECT series_id, obs_date, value, realtime_start FROM macro_vintages
        WHERE realtime_start <= $as_of
        QUALIFY row_number() OVER (PARTITION BY series_id, obs_date ORDER BY realtime_start DESC) = 1
    )
    SELECT series_id, obs_date, value, realtime_start FROM v WHERE value IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY series_id ORDER BY obs_date DESC) = 1
    """


def macro_regime(con: duckdb.DuckDBPyConnection, as_of: date) -> dict:
    """As known on as_of: {FRED series: (obs date, value, vintage) or None, Cboe index and
    "VIX9D_VIX": (trade date, value) or None, "labels": [...]}."""
    got = {r[0]: tuple(r[1:]) for r in con.execute(macro_regime_sql(), {"as_of": as_of}).fetchall()}
    vol = {r[0]: tuple(r[1:]) for r in con.execute(cboe_regime_sql(), {"as_of": as_of}).fetchall()}
    out: dict = {k: got.get(k) for k in SERIES} | {k: vol.get(k) for k in (*CBOE_INDEXES, "VIX9D_VIX")}
    out["labels"] = [label for label, on in (
        ("curve_inverted", out["T10Y2Y"] is not None and out["T10Y2Y"][1] < 0),
        ("tight_financial_conditions", out["NFCI"] is not None and out["NFCI"][1] > 0),
        ("vol_backwardation", out["VIX9D_VIX"] is not None and out["VIX9D_VIX"][1] > 1),
    ) if on]
    return out
