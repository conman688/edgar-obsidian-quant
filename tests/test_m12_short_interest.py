"""M12-A3: FINRA equity short interest (twice-monthly files), informational only.

File lines below are live FINRA values (cdn.finra.org, fetched 2026-11-25): the 2026 file
quotes every field, the 2018 file quotes none. Known-at is settlement + 9 weekdays, a
conservative stand-in for FINRA's publication date (settlement + 7 business days on its
2026-27 schedule) that allows one holiday plus a day of cushion.
"""
from datetime import date
from pathlib import Path

import pytest

from src.db.schema import connect
from src.ingestion import short_interest as si
from src.obsidian.sync import sync_vault
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company, screen_rows

HEAD_2026 = ('"accountingYearMonthNumber"|"symbolCode"|"issueName"|"issuerServicesGroupExchangeCode"|'
             '"marketClassCode"|"currentShortPositionQuantity"|"previousShortPositionQuantity"|"stockSplitFlag"|'
             '"averageDailyVolumeQuantity"|"daysToCoverQuantity"|"revisionFlag"|"changePercent"|'
             '"changePreviousNumber"|"settlementDate"')
LINE_2026 = '"20261113"|"A"|"Agilent Technologies Inc."|"A"|"NYSE"|"5050890"|"4892390"||"1964501"|"2.57"||"3.24"|"158500"|"2026-11-13"'
REVISED_2026 = '"20261113"|"CECO"|"Ceco Environmental Corp"|"F"|"TXSE"|"2578563"|"1541145"||"1987011"|"1.30"|"R"|"67.31"|"1037418"|"2026-11-13"'
HEAD_2018 = HEAD_2026.replace('"', "")
LINE_2018 = "20180112|A|Agilent Technologies|A|NYSE|5137321|4197300||1867541|2.75||22.40|940021|2018-01-12"


def file_for(settlement: date, rows: list[tuple[str, int, float]]) -> bytes:
    """A FINRA file with (symbol, short shares, days to cover) rows for one settlement date."""
    d = settlement.isoformat()
    lines = [HEAD_2018] + [f"{d.replace('-', '')}|{s}|{s} Corp|A|NYSE|{q}|{q}||{q}|{dtc}||0|0|{d}" for s, q, dtc in rows]
    return ("\n".join(lines) + "\n").encode()


class FakeFinra:
    """Stands in for FinraClient: a date list from the API and one file per date."""
    def __init__(self, files: dict[date, bytes], missing: set[date] = frozenset()):
        self.files, self.missing, self.fetched = files, set(missing), []

    def settlement_dates(self) -> list[date]:
        return sorted(self.files) + sorted(self.missing)

    def file(self, settlement: date) -> bytes:
        self.fetched.append(settlement)
        if settlement in self.missing:
            raise si.FinraError(f"403 for {settlement}")
        return self.files[settlement]


@pytest.fixture
def con():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())             # 1,000,000 shares, close 12
    return c


def row(con, as_of=AS_OF):
    return screen_rows(con, as_of)["ACME"]


# --- parsing and dates ---------------------------------------------------------------

def test_parses_quoted_2026_and_unquoted_2018_files():
    rows = si.parse_file(f"{HEAD_2026}\n{LINE_2026}\n{REVISED_2026}\n".encode())
    assert rows[0][:7] == (date(2026, 11, 13), "A", "NYSE", "Agilent Technologies Inc.", 5_050_890, 4_892_390, 1_964_501)
    assert rows[0][7] == 2.57 and rows[0][8] is None              # days to cover, revision flag
    assert rows[1][8] == "R"
    [old] = si.parse_file(f"{HEAD_2018}\n{LINE_2018}\n".encode())
    assert old[:3] == (date(2018, 1, 12), "A", "NYSE") and old[4] == 5_137_321 and old[7] == 2.75


def test_a_file_with_an_unknown_header_is_refused():
    with pytest.raises(si.FinraError):
        si.parse_file(b"symbol|short\nA|1\n")


@pytest.mark.parametrize("settlement,known", [
    (date(2026, 11, 13), date(2026, 11, 26)),      # FINRA publishes Nov 24 (7 business days)
    (date(2026, 12, 31), date(2027, 1, 13)),       # publishes Jan 12, after the New Year holiday
    (date(2026, 11, 30), date(2026, 12, 11)),      # publishes Dec 9
])
def test_known_at_is_nine_weekdays_after_settlement(settlement, known):
    assert si.known_at(settlement) == known


# --- ingest --------------------------------------------------------------------------

def test_ingest_loads_each_file_once_and_a_replay_fetches_nothing(con):
    d1, d2 = date(2024, 7, 31), date(2024, 8, 15)
    fake = FakeFinra({d1: file_for(d1, [("ACME", 100_000, 2.0)]), d2: file_for(d2, [("ACME", 150_000, 3.0)])})
    s = si.load(con, fake)
    assert (s.files, s.rows, s.failed) == (2, 2, 0)
    assert con.sql("SELECT known_at FROM short_interest WHERE settlement_date = DATE '2024-08-15'").fetchall() == \
        [(date(2024, 8, 28),)]
    fake.fetched.clear()
    s = si.load(con, fake)
    assert (s.files, s.rows, fake.fetched) == (0, 0, [])


def test_a_missing_file_is_retried_next_run_and_does_not_stop_the_others(con):
    d1, d2 = date(2024, 7, 31), date(2024, 8, 15)
    fake = FakeFinra({d1: file_for(d1, [("ACME", 100_000, 2.0)])}, missing={d2})
    s = si.load(con, fake)
    assert (s.files, s.failed) == (1, 1)
    fake.files[d2], fake.missing = file_for(d2, [("ACME", 150_000, 3.0)]), set()
    fake.fetched.clear()
    s = si.load(con, fake)
    assert (s.files, s.failed, fake.fetched) == (1, 0, [d2])


def test_the_finra_client_never_sends_the_sec_user_agent(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Someone someone@example.com")
    ua = si.FinraClient()._session.headers["User-Agent"]
    assert "@" not in ua and "example.com" not in ua


# --- screen --------------------------------------------------------------------------

def load_cycles(con, cycles):
    si.load(con, FakeFinra({d: file_for(d, [("ACME", q, dtc)]) for d, q, dtc in cycles}))


def test_latest_known_cycle_gives_short_interest_and_days_to_cover(con):
    load_cycles(con, [(date(2024, 7, 31), 100_000, 2.0), (date(2024, 8, 15), 150_000, 3.0)])
    r = row(con)                                              # Aug 15 known Aug 28 <= Sep 3
    assert r["short_interest_pct"] == pytest.approx(0.15)
    assert r["days_to_cover"] == 3.0
    assert r["short_settlement_date"] == date(2024, 8, 15) and r["short_known_at"] == date(2024, 8, 28)
    assert r["signals"] == ["high_short_interest"]


def test_a_cycle_not_yet_published_is_never_read(con):
    load_cycles(con, [(date(2024, 8, 15), 50_000, 1.0), (date(2024, 8, 30), 300_000, 6.0)])
    r = row(con)                                              # Aug 30 is known Sep 12 > Sep 3
    assert r["short_settlement_date"] == date(2024, 8, 15)
    assert r["short_interest_pct"] == pytest.approx(0.05) and r["signals"] == []
    assert row(con, date(2024, 9, 12))["short_settlement_date"] == date(2024, 8, 30)


def test_unmatched_symbols_and_companies_without_files_are_null(con):
    load_cycles(con, [(date(2024, 8, 15), 0, 0.0)])
    si.load(con, FakeFinra({date(2024, 7, 31): file_for(date(2024, 7, 31), [("OTHER", 1, 1.0)])}))
    r = row(con)
    assert r["short_interest_pct"] == 0 and r["signals"] == []


def test_short_interest_never_changes_status_rank_or_shortlist():
    def build(with_si):
        c = connect(":memory:")
        add_company(c, CIK, "ACME", cheap_company())
        add_buy(c, CIK, "b1")
        if with_si:
            load_cycles(c, [(date(2024, 8, 15), 900_000, 30.0)])
        return {t: (r["status"], r["rank"], r["failed_filters"], r["issues"], r["flags"])
                for t, r in screen_rows(c).items()}
    assert build(True) == build(False)


# --- vault ---------------------------------------------------------------------------

def test_company_note_and_dashboard_show_short_interest(con, tmp_path: Path):
    add_buy(con, CIK, "b1")
    load_cycles(con, [(date(2024, 8, 15), 150_000, 3.0)])
    sync_vault(con, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    meta = dict(split_note(note.read_text())[0])
    assert meta["short_interest_pct"] == 0.15 and meta["days_to_cover"] == 3.0
    assert "high_short_interest" in meta["signals"]
    assert "Short interest (FINRA, settled 2024-08-15, known 2024-08-28): 15.0% of shares, 3.0 days to cover." \
        in note.read_text()
    assert "short interest settled 2024-08-15" in (tmp_path / "Dashboard.md").read_text()
    source = (tmp_path / "Sources" / "FINRA short interest.md").read_text()
    assert "non-commercial" in source and "2017-12-29" in source
    assert sum(sync_vault(con, tmp_path, AS_OF).written.values()) == 0
