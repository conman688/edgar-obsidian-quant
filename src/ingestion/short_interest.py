"""M12-A3: FINRA equity short interest, twice-monthly files from 2017-12-29.

Sources (checked 2026-11-25; LINE.md "M12-A3 design"):
- Settlement dates: FINRA's public API, POST .../otcMarket/name/consolidatedShortInterest
  (no key), one symbol's rows giving one date per cycle.
- Files: cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv, pipe-delimited, the
  header quoted from 2026 and unquoted before.
- Terms: FINRA Data is for non-commercial use. Requests carry a generic User-Agent: the
  project's SEC User-Agent (it names the user) is never sent to a non-SEC host.

Known-at: FINRA publishes each cycle 7 business days after settlement (its 2026-27
schedule) but posts no past publication dates, so every row is known 9 weekdays after
settlement: one holiday allowed for, plus a day of cushion. FINRA revises rows in place
(revisionFlag R), so history holds revised values, not the first published ones; a file is
fetched once and never refetched.
"""
from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import duckdb
import requests

from src.db import ledger

STAGE = "finra_short_interest"
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


def known_at(settlement: date) -> date:
    """Settlement plus KNOWN_AFTER_WEEKDAYS weekdays."""
    d, n = settlement, 0
    while n < KNOWN_AFTER_WEEKDAYS:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return d


def _int(v: str) -> int | None:
    return int(float(v)) if v.strip() else None


def _float(v: str) -> float | None:
    return float(v) if v.strip() else None


def parse_file(payload: bytes) -> list[tuple]:
    """(settlement, symbol, market, name, short, previous short, ADV, days to cover,
    revision flag, split flag) per row. Refuses a header it does not know."""
    reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")), delimiter="|")
    header = next(reader, None)
    if tuple(h.strip() for h in header or ()) != COLUMNS:
        raise FinraError(f"unexpected FINRA header: {header!r:.200}")
    out = []
    for r in reader:
        if len(r) != len(COLUMNS) or not r[1].strip():
            continue
        out.append((date.fromisoformat(r[13].strip()), r[1].strip(), r[4].strip() or None, r[2].strip() or None,
                    _int(r[5]), _int(r[6]), _int(r[8]), _float(r[9]), r[10].strip() or None, r[7].strip() or None))
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


def load(con: duckdb.DuckDBPyConnection, client) -> LoadStats:
    """Fetch every settlement file not loaded yet. A failed file is reported and retried on
    the next run; it never stops the others."""
    s = LoadStats()
    for d in client.settlement_dates():
        if ledger.is_done(con, _key(d), STAGE, SCHEMA_VERSION):
            continue
        try:
            rows = parse_file(client.file(d))
        except (FinraError, requests.RequestException) as e:
            s.failed += 1
            s.errors.append(str(e))
            continue
        rows = [r for r in rows if r[0] == d]
        now = datetime.now()
        con.begin()
        try:
            before = con.execute("SELECT count(*) FROM short_interest").fetchall()[0][0]
            con.executemany("INSERT OR IGNORE INTO short_interest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [(*r[:2], known_at(d), *r[2:], now) for r in rows])
            added = con.execute("SELECT count(*) FROM short_interest").fetchall()[0][0] - before
            ledger.mark(con, _key(d), STAGE, SCHEMA_VERSION, "done", output_ref=f"rows={added}")
            con.commit()
        except BaseException:
            con.rollback()
            raise
        s.files += 1
        s.rows += added
    return s
