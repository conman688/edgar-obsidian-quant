"""M1 ingestion: bulk zip load, idempotency, crash resume, reference data, conditional download."""
import functools
import http.server
import json
import threading
import zipfile
from datetime import date
from pathlib import Path

import pytest

from src.db.schema import connect
from src.ingestion import companyfacts
from src.ingestion.companyfacts import load_zip
from src.ingestion.reference import load_companies_zip, load_tickers
from src.ingestion.sec import SecClient, sha256_file


def fact(start, end, val, filed, accn, form="10-K"):
    d = {"end": end, "val": val, "filed": filed, "accn": accn, "form": form, "fy": 2023, "fp": "FY"}
    if start:
        d["start"] = start
    return d


def company(cik: int, accns: list[str]) -> dict:
    rows = [fact("2023-01-01", "2023-12-31", 100.0 + i, "2024-02-15", a) for i, a in enumerate(accns)]
    rows.append(rows[0])  # SEC files occasionally repeat an identical fact
    rows.append(fact("2023-01-01", "2023-12-31", 1.0, "2024-02-15", accns[0], form="8-K"))  # dropped
    return {"cik": cik, "entityName": f"Co {cik}", "facts": {
        "us-gaap": {"Revenues": {"units": {"USD": rows}}},
        "srt": {"Ignored": {"units": {"USD": rows}}},
    }}


def write_zip(path: Path, docs: dict[str, dict]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, doc in docs.items():
            z.writestr(name, json.dumps(doc))
    return path


def count(con, table="xbrl_facts") -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]


@pytest.fixture
def con():
    return connect(":memory:")


def test_bulk_load_is_idempotent_across_versions(con, tmp_path):
    v1 = write_zip(tmp_path / "v1.zip", {"CIK0000000001.json": company(1, ["a1"]),
                                         "CIK0000000002.json": company(2, ["b1"])})
    s = load_zip(con, v1, sha256_file(v1))
    assert (s.files, s.facts_inserted) == (2, 2)          # duplicate, 8-K and srt rows dropped
    assert load_zip(con, v1, sha256_file(v1)).skipped     # same version: ledger skip

    v2 = write_zip(tmp_path / "v2.zip", {"CIK0000000001.json": company(1, ["a1", "a2"]),
                                         "CIK0000000002.json": company(2, ["b1"])})
    s = load_zip(con, v2, sha256_file(v2))
    assert s.facts_inserted == 1                          # only the new filing a2
    assert count(con) == 3 and count(con, "loaded_filings") == 3


def test_crash_mid_load_resumes_without_duplicates(con, tmp_path, monkeypatch):
    docs = {f"CIK{c:010d}.json": company(c, [f"x{c}"]) for c in range(1, 6)}
    z = write_zip(tmp_path / "f.zip", docs)
    real, calls = companyfacts._insert_batch, {"n": 0}

    def flaky(con_, rows):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("power cut")
        return real(con_, rows)

    monkeypatch.setattr(companyfacts, "_insert_batch", flaky)
    with pytest.raises(RuntimeError):
        _load_small_batches(con, z)
    assert count(con) == 2 and count(con, "loaded_filings") == 2   # whole filings only

    monkeypatch.setattr(companyfacts, "_insert_batch", real)
    _load_small_batches(con, z)
    assert count(con) == 5 and count(con, "loaded_filings") == 5


def _load_small_batches(con, z):
    return companyfacts.load_docs(con, companyfacts.iter_bulk_zip(z), batch_rows=1)


def test_cik_filter_and_paged_files_ignored(con, tmp_path):
    z = write_zip(tmp_path / "f.zip", {"CIK0000000001.json": company(1, ["a"]),
                                       "CIK0000000002.json": company(2, ["b"]),
                                       "CIK0000000002-submissions-001.json": {"cik": 2},
                                       "CIK0000000003.json": {},
                                       "CIK0000000004.json": {k: v for k, v in company(4, ["d"]).items()
                                                              if k != "cik"}})
    s = load_zip(con, z, sha256_file(z), ciks={2})
    assert s.files == 1 and count(con) == 1
    s = load_zip(con, z, sha256_file(z), ciks={3, 4})   # real SEC quirks: empty file, missing cik
    assert s.files == 1 and con.execute("SELECT cik FROM xbrl_facts WHERE accn = 'd'").fetchall()[0][0] == 4
    assert con.execute("SELECT count(*) FROM ledger").fetchall()[0][0] == 0  # partial loads not ledgered


def test_tickers_and_companies(con, tmp_path):
    payload = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
        [320193, "Apple Inc.", "AAPL", "Nasdaq"], [19617, "JPMorgan", "jpm", "NYSE"],
        [19617, "JPMorgan", "JPM", "NYSE"]]}).encode()
    assert load_tickers(con, payload, date(2026, 11, 3)) == 2
    with pytest.raises(ValueError):
        load_tickers(con, json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": []}).encode(),
                     date(2026, 11, 3))
    assert count(con, "tickers") == 2                     # empty feed never wipes the snapshot

    z = write_zip(tmp_path / "s.zip", {
        "CIK0000320193.json": {"cik": "320193", "name": "Apple Inc.", "sic": "3571", "fiscalYearEnd": "0927",
                               "exchanges": ["Nasdaq"], "tickers": ["AAPL"]},
        "CIK0000019617.json": {"cik": "19617", "name": "JPMorgan", "sic": "6021", "tickers": ["JPM"]},
        "CIK0000000099.json": {"cik": "99", "name": "Blank Check", "sic": "6770"},
        "CIK0000000098.json": {"cik": "98", "name": "No SIC", "sic": ""},
        "CIK0000320193-submissions-001.json": {"filings": []},
    })
    assert load_companies_zip(con, z, sha256_file(z)) == 3   # no-SIC, no-ticker filer skipped
    assert load_companies_zip(con, z, sha256_file(z)) == 0
    got = dict((r[0], r[1:]) for r in con.execute(
        "SELECT cik, sic, is_financial, is_shell, fiscal_year_end FROM companies").fetchall())
    assert got[320193] == (3571, False, False, "0927")
    assert got[19617][1] is True and got[99][2] is True and 98 not in got


def test_conditional_download_and_304(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    (root / "companyfacts.zip").write_bytes(b"x" * 300_000)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/companyfacts.zip"
        c = SecClient("test test@example.com", rate_per_s=100)
        dest = tmp_path / "raw" / "companyfacts.zip"
        first = c.download(url, dest)
        assert first["changed"] and first["bytes"] == 300_000 and dest.read_bytes() == b"x" * 300_000
        dest.unlink()                                     # raw file deleted after load
        second = c.download(url, dest)
        assert second["changed"] is False and second["sha256"] == first["sha256"]
        assert not list(dest.parent.glob("*.part"))
    finally:
        srv.shutdown()


def test_cli_facts_and_stats(tmp_path, monkeypatch, capsys):
    from src import cli, config
    db = tmp_path / "edgar.duckdb"
    monkeypatch.setattr(config, "DB_PATH", db)
    con = connect(str(db))
    z = write_zip(tmp_path / "f.zip", {"CIK0000000001.json": company(1, ["a1"])})
    load_zip(con, z, sha256_file(z))
    load_tickers(con, json.dumps({"fields": ["cik", "name", "ticker", "exchange"],
                                  "data": [[1, "Co 1", "COA", "NYSE"]]}).encode(), date(2026, 11, 3))
    con.close()
    cli.main(["facts", "coa", "revenue", "--as-of", "2024-03-01"])
    out = capsys.readouterr().out
    assert "annual" in out and "10-K" in out and "a1" in out
    cli.main(["facts", "1", "revenue", "--as-of", "2024-01-01"])
    assert "no facts" in capsys.readouterr().out          # not filed yet on that date
    cli.main(["stats"])
    assert "filings missing from ledger  0" in capsys.readouterr().out
