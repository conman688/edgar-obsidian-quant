"""M12-A4: macro-regime overlay from FRED/ALFRED vintages, informational only.

Live facts (api.stlouisfed.org, 2026-11-26): NFCI has 806 vintages from 2011-05-25 and is
revised every week (the 2020-03-20 value was -0.17, then -0.13, -0.17, -0.20 ...); T10Y2Y has
vintages from 2014-01-27 and is almost never revised. One request holds at most 2,000 vintages.
Known-at is the vintage date (realtime_start).
"""
from datetime import date, datetime

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


KEY = "abcdefghij" + "k" * 22                     # a fixture key, never the real one


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


@pytest.mark.parametrize("make", [
    # Codex: the body echoed with the key at offset 190; slicing before redacting kept "abcdefghij".
    lambda kw: Resp(400, "x" * 190 + f"api_key={KEY}"),
    lambda kw: (_ for _ in ()).throw(__import__("requests").ConnectionError(
        f"HTTPSConnectionPool: Max retries exceeded with url: /fred/series?api_key={KEY}")),
    lambda kw: Resp(302, f"<a href='https://elsewhere.example/?api_key={KEY}'>"),
])
def test_errors_name_the_endpoint_and_status_only_and_redirects_are_refused(monkeypatch, make):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    c = macro.FredClient()
    seen = []

    def get(url, **kw):
        seen.append(kw)
        return make(kw)
    monkeypatch.setattr(c._session, "get", get)
    with pytest.raises(macro.FredError) as e:
        c.vintage_dates("NFCI")
    msg = str(e.value)
    assert KEY[:6] not in msg and "api_key" not in msg and "elsewhere" not in msg
    assert msg.startswith("FRED series/vintagedates ")
    assert seen[0]["allow_redirects"] is False


def test_no_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(macro.FredError, match="FRED_API_KEY"):
        macro.FredClient()


# --- vault and CLI -------------------------------------------------------------------

def test_dashboard_shows_the_regime_as_known_on_as_of_and_a_second_sync_writes_nothing(con, tmp_path):
    from src.obsidian.sync import sync_vault
    macro.load_fred(con, FakeFred({"T10Y2Y": {W1: {F1: -0.4}}, **nfci()}))
    sync_vault(con, tmp_path, W2)
    dash = (tmp_path / "Dashboard.md").read_text()
    assert "## Macro" in dash
    assert "10y-2y Treasury spread -0.40 pp (2020-03-20, FRED vintage 2020-03-25)" in dash
    assert "NFCI -0.10 (week ending 2020-03-27, vintage 2020-04-01)" in dash
    assert "Regime: curve_inverted." in dash
    source = (tmp_path / "Sources" / "FRED macro series.md").read_text()
    assert "FRED_API_KEY" in source and "2011-05-25" in source and "BAMLH0A0HYM2" in source
    assert sum(sync_vault(con, tmp_path, W2).written.values()) == 0


def test_dashboard_without_macro_data_says_so(con, tmp_path):
    from src.obsidian.sync import sync_vault
    sync_vault(con, tmp_path, W2)
    assert "No macro series stored" in (tmp_path / "Dashboard.md").read_text()


def test_weekly_ingest_all_includes_macro_and_a_fred_failure_only_warns(con, monkeypatch, capsys):
    from types import SimpleNamespace
    from src import cli

    def no_key():
        raise macro.FredError("Set FRED_API_KEY in .env")
    monkeypatch.setattr(macro, "FredClient", no_key)
    cli.ingest_macro(con, SimpleNamespace())                     # no SystemExit: weekly carries on
    assert "macro: failed (retried next run): Set FRED_API_KEY" in capsys.readouterr().out
    monkeypatch.setattr(macro, "FredClient", lambda: FakeFred(nfci()))
    cli.ingest_macro(con, SimpleNamespace())
    assert "macro: 5 rows" in capsys.readouterr().out
    assert cli.ingest_macro in cli.INGEST_JOBS["all"]
    assert ["ingest", "macro"] not in cli.RUN_STEPS["daily"]


# --- Cboe volatility indexes -----------------------------------------------------------
# Live headers (cdn-api.cboe.com, 2026-11-26): VIX and VIX9D "DATE,OPEN,HIGH,LOW,CLOSE",
# VVIX "DATE,VVIX"; dates MM/DD/YYYY. Cboe backdated the modern VIX to 1990 after its 2003
# redesign (Codex, LINE.md), so a close is known no earlier than our first fetch of it.

def ohlc(*rows):
    return ("DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(f"{d},1,1,1,{c}\n" for d, c in rows)).encode()


class FakeCboe:
    def __init__(self, files):
        self.files = files

    def history(self, index):
        if isinstance(self.files[index], Exception):
            raise self.files[index]
        return self.files[index]


def cboe(vix=(("11/19/2026", 20.0), ("11/20/2026", 22.0)), vix9d=(("11/19/2026", 21.0), ("11/20/2026", 23.0)),
         vvix=b"DATE,VVIX\n11/20/2026,115.38\n"):
    return FakeCboe({"VIX": ohlc(*vix), "VIX9D": ohlc(*vix9d), "VVIX": vvix})


def test_parses_ohlc_and_single_value_files():
    assert macro.parse_cboe(b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17.24,17.24,17.24,17.24\n") == [(date(1990, 1, 2), 17.24)]
    assert macro.parse_cboe(b"DATE,VVIX\n03/06/2006,71.73\n") == [(date(2006, 3, 6), 71.73)]
    with pytest.raises(macro.CboeError):
        macro.parse_cboe(b"day,level\n1,2\n")


def test_a_close_is_known_no_earlier_than_its_first_fetch(con):
    macro.load_cboe(con, cboe(), now=datetime(2026, 11, 26, 9))
    assert macro.macro_regime(con, date(2026, 11, 25))["VIX"] is None     # backfilled: not known yet
    r = macro.macro_regime(con, date(2026, 11, 26))
    assert r["VIX"] == (date(2026, 11, 20), 22.0) and r["VVIX"] == (date(2026, 11, 20), 115.38)


def test_a_close_fetched_the_same_evening_is_known_that_day_and_replay_inserts_nothing(con):
    f = cboe()
    assert macro.load_cboe(con, f, now=datetime(2026, 11, 20, 18)).rows == 5
    assert macro.macro_regime(con, date(2026, 11, 20))["VIX"] == (date(2026, 11, 20), 22.0)
    assert macro.load_cboe(con, f, now=datetime(2026, 11, 27, 9)).rows == 0
    assert con.sql("SELECT DISTINCT known_at FROM cboe_daily").fetchall() == [(date(2026, 11, 20),)]


def test_term_ratio_uses_matching_trade_dates_only(con):
    # VIX9D has no 11-20 row: the ratio is 11-19's (21/20), never 11-19's VIX9D over 11-20's VIX.
    macro.load_cboe(con, cboe(vix9d=(("11/19/2026", 21.0),)), now=datetime(2026, 11, 20, 18))
    r = macro.macro_regime(con, date(2026, 11, 20))
    assert r["VIX9D_VIX"] == (date(2026, 11, 19), pytest.approx(1.05))
    assert r["labels"] == ["vol_backwardation"]


def test_a_failing_index_warns_and_the_others_load(con):
    f = cboe()
    f.files["VVIX"] = macro.CboeError("Cboe VVIX HTTP 403")
    s = macro.load_cboe(con, f, now=datetime(2026, 11, 20, 18))
    assert s.failed == ["VVIX"] and s.rows == 4
    assert macro.load_cboe(con, cboe(), now=datetime(2026, 11, 21, 9)).rows == 1   # retried next run


def test_dashboard_shows_volatility_and_the_cboe_source_note(con, tmp_path):
    from src.obsidian.sync import sync_vault
    macro.load_cboe(con, cboe(), now=datetime(2026, 11, 20, 18))
    sync_vault(con, tmp_path, date(2026, 11, 20))
    dash = (tmp_path / "Dashboard.md").read_text()
    assert "VIX 22.00 (2026-11-20), VIX9D/VIX 1.05 (2026-11-20), VVIX 115.38 (2026-11-20)" in dash
    assert "Regime: vol_backwardation." in dash
    src = (tmp_path / "Sources" / "Cboe volatility indexes.md").read_text()
    assert "backdated" in src and "first fetch" in src
