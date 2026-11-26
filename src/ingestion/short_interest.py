"""M12-A3: FINRA equity short interest, twice-monthly files from 2017-12-29.

Sources (checked 2026-11-25; LINE.md "M12-A3 design"):
- Settlement dates: FINRA's public API, POST .../otcMarket/name/consolidatedShortInterest
  (no key), one symbol's rows giving one date per cycle.
- Files: cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv, pipe-delimited, the
  header quoted from 2026 and unquoted before.
- Terms: FINRA Data is for non-commercial use. Requests carry a generic User-Agent: the
  project's SEC User-Agent (it names the user) is never sent to a non-SEC host.

Known-at: FINRA publishes each cycle 7 business days after settlement (its 2026-27
schedule) but posts no past publication dates. `published_est` is settlement + 9 weekdays:
one holiday allowed for, plus a day of cushion. FINRA revises rows in place (revisionFlag R),
and a cycle can be late, so a row is known no earlier than our first fetch of it:
known_at = max(published_est, fetch date). A backfill is therefore known only from the day
it ran. A file is fetched once and never refetched.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import duckdb
import pyarrow as pa
import requests

from src.db import ledger

STAGE = "finra_short_interest"
ROW_SCHEMA = pa.schema([
    ("settlement_date", pa.date32()), ("symbol", pa.string()), ("known_at", pa.date32()), ("published_est", pa.date32()),
    ("market", pa.string()), ("issue_name", pa.string()), ("short_shares", pa.int64()),
    ("prev_short_shares", pa.int64()), ("avg_daily_volume", pa.int64()), ("days_to_cover", pa.float64()),
    ("revision_flag", pa.string()), ("split_flag", pa.string()), ("fetched_at", pa.timestamp("us")),
])
SCHEMA_VERSION = 1
API_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
FILE_URL = "https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d:%Y%m%d}.csv"
USER_AGENT = "edgar-obsidian-quant research script"        # generic: never the SEC User-Agent
DATE_SYMBOL = "AA"                                         # listed throughout 2017-2026
KNOWN_AFTER_WEEKDAYS = 9
COLUMNS = ("accountingYearMonthNumber", "symbolCode", "issueName", "issuerServicesGroupExchangeCode",
           "marketClassCode", "currentShortPositionQuantity", "previousShortPositionQuantity", "stockSplitFlag",
           "averageDailyVolumeQuantity", "daysToCoverQuantity", "revisionFlag", "changePercent",
           "changePreviousNumber", "settlementDate")


class FinraError(RuntimeError):
    pass


def published_est(settlement: date) -> date:
    """FINRA's estimated publication date: settlement plus KNOWN_AFTER_WEEKDAYS weekdays."""
    d, n = settlement, 0
    while n < KNOWN_AFTER_WEEKDAYS:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return d


def _unquote(v: str) -> str:
    """A field without its surrounding quotes and with "" as ". A field that opens a quote
    and never closes it is kept as written."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1].replace('""', '"')
    return v


def _int(v: str) -> int | None:
    return int(float(v)) if v.strip() else None


def _float(v: str) -> float | None:
    return float(v) if v.strip() else None


def parse_file(payload: bytes) -> list[tuple]:
    """(settlement, symbol, market, name, short, previous short, ADV, days to cover,
    revision flag, split flag) per row. Refuses a header it does not know."""
    # Split each line on "|" rather than with csv quoting: FINRA fields never hold "|", but
    # some names open a quote and never close it (2018-03-29, DOD), which would make a CSV
    # reader swallow the rest of the file.
    lines = payload.decode("utf-8-sig").splitlines()
    header = [_unquote(h) for h in lines[0].split("|")] if lines else []
    if tuple(header) != COLUMNS:
        raise FinraError(f"unexpected FINRA header: {header!r:.200}")
    out = []
    for n, r in enumerate(([_unquote(f) for f in line.split("|")] for line in lines[1:]), start=2):
        if len(r) != len(COLUMNS) or not r[1].strip():
            continue
        try:
            out.append((date.fromisoformat(r[13].strip()), r[1].strip(), r[4].strip() or None, r[2].strip() or None,
                        _int(r[5]), _int(r[6]), _int(r[8]), _float(r[9]), r[10].strip() or None,
                        r[7].strip() or None))
        except ValueError as e:
            raise FinraError(f"FINRA line {n}: {e}") from e
    return out


class FinraClient:
    """FINRA's public API and CDN, 1 request a second, generic User-Agent."""

    def __init__(self, rate_per_s: float = 1.0):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        self._interval, self._next = 1.0 / rate_per_s, 0.0

    def _wait(self) -> None:
        wait = self._next - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next = max(self._next, time.monotonic()) + self._interval

    def settlement_dates(self) -> list[date]:
        self._wait()
        r = self._session.post(API_URL, timeout=60, headers={"Accept": "application/json"}, json={
            "fields": ["settlementDate"], "limit": 5000,
            "compareFilters": [{"compareType": "equal", "fieldName": "symbolCode", "fieldValue": DATE_SYMBOL}]})
        if r.status_code != 200:
            raise FinraError(f"FINRA API {r.status_code}")
        return sorted({date.fromisoformat(x["settlementDate"]) for x in r.json()})

    def file(self, settlement: date) -> bytes:
        self._wait()
        r = self._session.get(FILE_URL.format(d=settlement), timeout=120)
        if r.status_code != 200:
            raise FinraError(f"FINRA file {settlement}: HTTP {r.status_code}")
        return r.content


@dataclass
class LoadStats:
    files: int = 0
    rows: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _key(settlement: date) -> str:
    return f"finra-si@{settlement:%Y%m%d}"


def load(con: duckdb.DuckDBPyConnection, client, now: datetime | None = None) -> LoadStats:
    """Fetch every settlement file not loaded yet. A failed file (no response, a bad line, or
    no rows for its own settlement date) is reported and retried on the next run; it never
    stops the others. `now` fixes the fetch time (tests)."""
    s = LoadStats()
    for d in client.settlement_dates():
        if ledger.is_done(con, _key(d), STAGE, SCHEMA_VERSION):
            continue
        try:
            rows = [r for r in parse_file(client.file(d)) if r[0] == d]
            if not rows:
                raise FinraError(f"FINRA file {d}: no rows for its settlement date")
        except (FinraError, requests.RequestException) as e:
            s.failed += 1
            s.errors.append(str(e))
            continue
        fetched = now or datetime.now()
        known = max(published_est(d), fetched.date())
        con.begin()
        try:
            before = con.execute("SELECT count(*) FROM short_interest").fetchall()[0][0]
            full = [(*r[:2], known, published_est(d), *r[2:], fetched) for r in rows]
            con.register("si_rows", pa.Table.from_arrays(
                [pa.array(c, type=f.type) for c, f in zip(zip(*full), ROW_SCHEMA)], schema=ROW_SCHEMA))
            try:
                con.execute("INSERT OR IGNORE INTO short_interest SELECT * FROM si_rows")
            finally:
                con.unregister("si_rows")
            added = con.execute("SELECT count(*) FROM short_interest").fetchall()[0][0] - before
            ledger.mark(con, _key(d), STAGE, SCHEMA_VERSION, "done", output_ref=f"rows={added}")
            con.commit()
        except BaseException:
            con.rollback()
            raise
        s.files += 1
        s.rows += added
    return s
