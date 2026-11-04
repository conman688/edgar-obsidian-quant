"""Regression tests for review findings on M1 ingestion recovery."""
import duckdb

from src import cli, config
from src.db.schema import connect
from src.ingestion import companyfacts
from src.ingestion.sec import sha256_file
from tests.test_ingest import company, count, fact, write_zip


class FakeSec:
    """Serves one zip. Answers 304 to conditional requests, like SEC after a prior download."""

    def __init__(self, zip_path):
        self.payload, self.sha, self.forced = zip_path.read_bytes(), sha256_file(zip_path), []
        self.served_once = False

    def download(self, url, dest, force=False):
        self.forced.append(force)
        if self.served_once and not force:
            return {"changed": False, "sha256": self.sha, "path": None}
        self.served_once = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        return {"changed": True, "sha256": self.sha, "path": str(dest), "bytes": len(self.payload)}


def _setup(tmp_path, monkeypatch, sec):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "edgar.duckdb")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(cli, "_client", lambda: sec)


def _facts(tmp_path) -> int:
    con = connect(str(tmp_path / "edgar.duckdb"))
    try:
        return count(con)
    finally:
        con.close()


def test_304_after_failed_load_downloads_again(tmp_path, monkeypatch):
    z = write_zip(tmp_path / "src.zip", {"CIK0000000001.json": company(1, ["a1"])})
    sec = FakeSec(z)
    sec.served_once = True                      # an earlier run downloaded, then the load crashed
    _setup(tmp_path, monkeypatch, sec)
    cli.main(["ingest", "facts"])
    assert sec.forced == [False, True] and _facts(tmp_path) == 1


def test_filtered_load_then_full_load_of_same_version(tmp_path, monkeypatch):
    z = write_zip(tmp_path / "src.zip", {"CIK0000000001.json": company(1, ["a1"]),
                                         "CIK0000000002.json": company(2, ["b1"])})
    sec = FakeSec(z)
    _setup(tmp_path, monkeypatch, sec)
    cli.main(["ingest", "facts", "--cik", "1"])
    assert _facts(tmp_path) == 1
    cli.main(["ingest", "facts"])               # SEC answers 304: same version
    assert _facts(tmp_path) == 2
    cli.main(["ingest", "facts"])               # now fully loaded: skipped, zip deleted
    assert sec.forced == [False, False, False] and not (tmp_path / "raw" / "companyfacts.zip").exists()


def test_m0_database_replay_does_not_hit_duplicate_key(tmp_path):
    db = str(tmp_path / "m0.duckdb")
    m0 = duckdb.connect(db)                     # M0 layout: PRIMARY KEY on xbrl_facts, no loaded_filings
    m0.execute("""CREATE TABLE xbrl_facts (
        cik INTEGER NOT NULL, taxonomy VARCHAR NOT NULL, tag VARCHAR NOT NULL, unit VARCHAR NOT NULL,
        period_start DATE NOT NULL, period_end DATE NOT NULL, period_days INTEGER NOT NULL,
        period_kind VARCHAR NOT NULL, fy INTEGER, fp VARCHAR, form VARCHAR NOT NULL, accn VARCHAR NOT NULL,
        filed DATE NOT NULL, frame VARCHAR, value DOUBLE NOT NULL,
        PRIMARY KEY (cik, taxonomy, tag, unit, period_start, period_end, accn))""")
    m0.executemany("INSERT INTO xbrl_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   list(dict.fromkeys(companyfacts.iter_fact_rows(company(1, ["a1"])))))  # M0 PK: no repeats
    m0.close()

    con = connect(db)
    assert count(con, "loaded_filings") == 1
    assert companyfacts.load_docs(con, [company(1, ["a1"])]).facts_inserted == 0


def test_fact_added_to_existing_filing_is_loaded():
    con = connect(":memory:")
    v1 = company(1, ["a1"])
    companyfacts.load_docs(con, [v1])
    v2 = company(1, ["a1"])
    v2["facts"]["us-gaap"]["OperatingIncomeLoss"] = {"units": {"USD": [
        fact("2023-01-01", "2023-12-31", 42.0, "2024-02-15", "a1")]}}
    s = companyfacts.load_docs(con, [v2])
    assert s.facts_inserted == 1 and s.filings_grown == 1
    assert companyfacts.load_docs(con, [v2]).facts_inserted == 0
    assert count(con) == 2
