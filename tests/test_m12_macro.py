"""M12-A4: macro-regime overlay from FRED/ALFRED vintages, informational only.

Live facts (api.stlouisfed.org, 2026-11-26): NFCI has 806 vintages from 2011-05-25 and is
revised every week (the 2020-03-20 value was -0.17, then -0.13, -0.17, -0.20 ...); T10Y2Y has
vintages from 2014-01-27 and is almost never revised. One request holds at most 2,000 vintages.
Known-at is the vintage date (realtime_start).
"""
from datetime import date

import pytest

from src.db.schema import connect
from src.ingestion import macro


class FakeFred:
    """Stands in for FredClient. vintages: series -> {vintage date: {obs date: value or None}},
    each vintage the whole series as published that day."""
    def __init__(self, vintages: dict[str, dict[date, dict[date, float | None]]]):
        self.vintages, self.calls = vintages, []

    def vintage_dates(self, series: str) -> list[date]:
        return sorted(self.vintages.get(series, {}))

    def observations(self, series: str, start: date, end: date) -> list[tuple[date, date, float | None]]:
        """(obs date, realtime_start, value) like ALFRED: one row per obs per change of value,
        realtime_start clipped to `start`."""
        self.calls.append((series, start, end))
        vs = [v for v in sorted(self.vintages[series]) if start <= v <= end]
        assert len(vs) <= macro.MAX_VINTAGES_PER_REQUEST
        out, last = [], {}
        for v in vs:
            for obs, val in self.vintages[series][v].items():
                if obs not in last or last[obs] != val:
                    out.append((obs, max(v, start), val))
                    last[obs] = val
        return out


W1, W2, W3 = date(2020, 3, 25), date(2020, 4, 1), date(2020, 4, 8)      # NFCI release Wednesdays
F1, F2 = date(2020, 3, 20), date(2020, 3, 27)                           # weeks ending Friday


def nfci():
    return {"NFCI": {W1: {F1: -0.17}, W2: {F1: -0.13, F2: -0.10}, W3: {F1: -0.17, F2: -0.12}}}


@pytest.fixture
def con():
    return connect(":memory:")


def regime(con, as_of):
    return macro.macro_regime(con, as_of)


def test_a_revised_value_is_read_as_it_was_known_on_each_date(con):
    macro.load_fred(con, FakeFred(nfci()))
    assert regime(con, date(2020, 3, 24))["NFCI"] is None                # before the first vintage
    assert regime(con, W1)["NFCI"] == (F1, -0.17, W1)
    assert regime(con, date(2020, 3, 31))["NFCI"] == (F1, -0.17, W1)
    assert regime(con, W2)["NFCI"] == (F2, -0.10, W2)                     # newest obs, first print
    assert regime(con, W3)["NFCI"] == (F2, -0.12, W3)                     # revised


def test_replay_inserts_nothing_and_a_new_vintage_adds_only_its_rows(con):
    fake = FakeFred(nfci())
    assert macro.load_fred(con, fake).rows > 0
    fake.calls.clear()
    assert macro.load_fred(con, fake).rows == 0 and fake.calls == []     # nothing newer: no request
    W4 = date(2020, 4, 15)
    fake.vintages["NFCI"][W4] = {F1: -0.17, F2: -0.12, date(2020, 4, 3): -0.05}
    assert macro.load_fred(con, fake).rows == 1 and fake.calls == [("NFCI", W4, W4)]


def test_vintages_are_fetched_in_chunks_of_at_most_the_api_limit(con, monkeypatch):
    monkeypatch.setattr(macro, "MAX_VINTAGES_PER_REQUEST", 2)
    fake = FakeFred({"T10Y2Y": {date(2024, 1, d): {date(2024, 1, d): d / 100} for d in range(2, 8)}})
    macro.load_fred(con, fake)
    assert [(c[1].day, c[2].day) for c in fake.calls] == [(2, 3), (4, 5), (6, 7)]
    assert regime(con, date(2024, 1, 7))["T10Y2Y"] == (date(2024, 1, 7), 0.07, date(2024, 1, 7))


def test_a_missing_value_falls_back_to_the_last_obs_with_one(con):
    # FRED marks holidays "." (no value); the newest real value is the one in force.
    macro.load_fred(con, FakeFred({"T10Y2Y": {date(2024, 7, 5): {date(2024, 7, 3): 0.31, date(2024, 7, 4): None}}}))
    assert regime(con, date(2024, 7, 5))["T10Y2Y"] == (date(2024, 7, 3), 0.31, date(2024, 7, 5))


def test_labels(con):
    macro.load_fred(con, FakeFred({"T10Y2Y": {W1: {F1: -0.4}}, **nfci()}))
    assert regime(con, W1)["labels"] == ["curve_inverted"]
    macro.load_fred(con, FakeFred({"NFCI": {date(2020, 4, 22): {date(2020, 4, 17): 0.3}}}))
    assert regime(con, date(2020, 4, 22))["labels"] == ["curve_inverted", "tight_financial_conditions"]


def test_a_failure_in_one_series_keeps_the_other_and_stores_nothing_partial(con, monkeypatch):
    class Broken(FakeFred):
        def observations(self, series, start, end):
            if series == "NFCI" and start > W1:
                raise macro.FredError("FRED 500")
            return super().observations(series, start, end)
    monkeypatch.setattr(macro, "MAX_VINTAGES_PER_REQUEST", 1)          # NFCI: W1 loads, then W2 fails
    s = macro.load_fred(con, Broken({"T10Y2Y": {W1: {F1: 0.5}}, **nfci()}))
    assert s.failed == ["NFCI"] and "FRED 500" in s.errors[0]
    assert con.sql("SELECT DISTINCT series_id FROM macro_vintages").fetchall() == [("T10Y2Y",)]


def test_the_api_key_never_appears_in_an_error(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)
    c = macro.FredClient()

    class R:
        status_code, text, url = 400, "Bad Request", f"https://api.stlouisfed.org/fred/x?api_key={'k' * 32}"
    monkeypatch.setattr(c._session, "get", lambda *a, **k: R())
    with pytest.raises(macro.FredError) as e:
        c.vintage_dates("NFCI")
    assert "k" * 32 not in str(e.value)
    assert "@" not in c._session.headers["User-Agent"]


def test_no_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(macro.FredError, match="FRED_API_KEY"):
        macro.FredClient()
