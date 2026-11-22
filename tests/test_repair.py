"""CIK 835011 repair (MGPI -> CASK): restore preserved price history that the 11-07 weekly deleted.

Fixtures only: temporary DuckDB files and Parquet snapshots in tmp_path. No live database.
"""
import hashlib
import json
import os
from datetime import date

import duckdb
import pandas as pd
import pytest

from src import ops, repair
from src.db.schema import MIGRATIONS, connect, migrate
from src.ingestion import prices

CIK = 835011
D = date.fromisoformat


def write_source(dir_, prices_rows, dividend_rows, split_rows=()):
    """Preserved snapshot: Parquet files with the live tables' types, plus SHA256SUMS."""
    dir_.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect()
    c.execute("CREATE TABLE prices (cik INTEGER, ticker VARCHAR, price_date DATE, close_raw DOUBLE, "
              "volume_raw DOUBLE, source VARCHAR)")
    c.execute("CREATE TABLE dividends (cik INTEGER, ex_date DATE, amount_raw DOUBLE, source VARCHAR)")
    c.execute("CREATE TABLE splits (cik INTEGER, ex_date DATE, ratio DOUBLE, source VARCHAR)")
    if prices_rows:
        c.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?)", prices_rows)
    if dividend_rows:
        c.executemany("INSERT INTO dividends VALUES (?, ?, ?, ?)", dividend_rows)
    if split_rows:
        c.executemany("INSERT INTO splits VALUES (?, ?, ?, ?)", split_rows)
    for t in ("prices", "dividends", "splits"):
        c.execute(f"COPY (SELECT * FROM {t}) TO '{dir_ / (t + '_b07.parquet')}' (FORMAT parquet)")
    c.close()
    sums = [f"{hashlib.sha256((dir_ / n).read_bytes()).hexdigest()}  {n}"
            for n in ("prices_b07.parquet", "dividends_b07.parquet", "splits_b07.parquet")]
    (dir_ / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return {t: dir_ / f"{t}_b07.parquet" for t in ("prices", "dividends", "splits")}


SOURCE_PRICES = [  # the old security's rows, as preserved from the pre-loss backup
    (CIK, "MGPI", D("2026-10-21"), 10.0, 100.0, "yahoo"),
    (CIK, "MGPI", D("2026-10-22"), 11.0, None, "yahoo"),
    (CIK, "MGPI", D("2026-10-23"), 12.0, 300.0, "yahoo"),
    (CIK, "MGPI", D("2026-11-02"), 13.0, 400.0, "yahoo"),   # also live, under CASK: live wins
]
SOURCE_DIVIDENDS = [(CIK, D("2026-08-14"), 0.12, "yahoo"),
                    (CIK, D("2026-11-13"), 0.12, "yahoo")]    # live has 0.13 on this key: live wins


def seed_live(con):
    """CIK 835011 after the loss (CASK rows only) plus an unrelated company, CIK 2."""
    con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?)", [
        (CIK, "CASK", D("2026-10-26"), 14.0, 500.0, "yahoo"),
        (CIK, "CASK", D("2026-11-02"), 13.5, 600.0, "yahoo"),
        (CIK, "CASK", D("2026-11-13"), 15.0, 700.0, "yahoo"),
        (2, "NVDA", D("2026-10-22"), 120.0, 1e6, "yahoo"),
        (2, "NVDA", D("2026-10-23"), 121.0, 1e6, "yahoo")])
    con.executemany("INSERT INTO dividends VALUES (?, ?, ?, ?)", [
        (CIK, D("2026-11-13"), 0.13, "yahoo"), (2, D("2026-10-22"), 0.01, "yahoo")])
    con.execute("INSERT INTO splits VALUES (2, DATE '2024-06-10', 10.0, 'yahoo')")
    con.executemany("INSERT INTO price_status VALUES (?, ?, 'ok', ?, ?, NULL, TIMESTAMP '2026-11-14 07:08:45')", [
        (CIK, "CASK", D("2026-10-26"), D("2026-11-13")), (2, "NVDA", D("2024-06-06"), D("2026-10-23"))])


def live_db(tmp_path, schema=None):
    """A database file like data/edgar.duckdb. schema=8 stops before migration 9 (no held table)."""
    path = tmp_path / "data" / "edgar.duckdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    if schema is None:
        con = connect(str(path))
    else:
        con = duckdb.connect(str(path))
        migrate(con, [m for m in MIGRATIONS if m[0] <= schema])
    seed_live(con)
    con.close()
    return path


def snapshot(path):
    """Every row of every table, for an exact before/after comparison."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = [t for (t,) in con.execute("SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]
        return {t: sorted(map(repr, con.execute(f"SELECT * FROM {t}").fetchall())) for t in tables}
    finally:
        con.close()


def args(db, src, *extra):
    return ["--db", str(db), "--cik", str(CIK), "--prices", str(src["prices"]),
            "--dividends", str(src["dividends"]), "--splits", str(src["splits"]), *extra]


def run(argv, capsys):
    code = repair.main(argv)
    return code, capsys.readouterr().out


def canonical_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False)
                          .encode()).hexdigest()


def test_plan_inserts_only_missing_keys_and_keeps_every_live_row(tmp_path):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    m = repair.plan(con, CIK, repair.read_source(src, CIK))
    con.close()
    p = m["plan"]
    assert p["cik"] == CIK
    assert p["prices"]["insert"] == [["2026-10-21", "MGPI", 10.0, 100.0, "yahoo"],
                                     ["2026-10-22", "MGPI", 11.0, None, "yahoo"],
                                     ["2026-10-23", "MGPI", 12.0, 300.0, "yahoo"]]
    assert p["prices"]["keep_live"] == [["2026-11-02", ["MGPI", 13.0, 400.0, "yahoo"],
                                         ["CASK", 13.5, 600.0, "yahoo"]]]
    assert p["prices"]["already_present"] == []
    assert p["dividends"]["insert"] == [["2026-08-14", 0.12, "yahoo"]]
    assert p["dividends"]["keep_live"] == [["2026-11-13", [0.12, "yahoo"], [0.13, "yahoo"]]]
    assert p["splits"] == {"insert": [], "keep_live": [], "already_present": []}
    assert p["price_status"] == {"set_first_date": "2026-10-21"}   # least(), as prices._store keeps it
    assert p["blocked"] == []
    assert set(p["source"]) == {"prices_b07.parquet", "dividends_b07.parquet", "splits_b07.parquet"}
    assert m["hash"] == canonical_hash(p)
    assert m["context"]["schema_version"] == max(v for v, _ in MIGRATIONS)
    assert m["context"]["other_ciks"]["prices"][0] == 2       # rows outside the target, by count


def test_plan_is_deterministic(tmp_path):
    src = write_source(tmp_path / "src", list(reversed(SOURCE_PRICES)), SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    a = repair.plan(con, CIK, repair.read_source(src, CIK))
    b = repair.plan(con, CIK, repair.read_source(src, CIK))
    con.close()
    assert a["hash"] == b["hash"] and a["plan"] == b["plan"]


def test_dry_run_is_the_default_and_writes_nothing_nor_migrates(tmp_path, capsys):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path, schema=8)            # today's live schema: migration 9 not yet applied
    before, stat = snapshot(db), os.stat(db)
    out_file = tmp_path / "manifest.json"
    code, out = run(args(db, src, "--out", str(out_file)), capsys)
    assert code == 0
    assert snapshot(db) == before
    assert (os.stat(db).st_size, os.stat(db).st_mtime_ns) == (stat.st_size, stat.st_mtime_ns)
    assert not (db.parent / "edgar.duckdb.wal").exists()
    m = json.loads(out_file.read_text())
    assert m["hash"] == canonical_hash(m["plan"]) and m["hash"] in out
    assert m["context"]["schema_version"] == 8
    assert m["context"]["held_actions"] is None             # table absent before migration 9
    assert "dry run" in out


def test_write_refuses_without_the_reviewed_hash(tmp_path, capsys):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    before = snapshot(db)
    with pytest.raises(SystemExit):
        repair.main(args(db, src, "--write"))                   # --write needs --expect-hash
    code, out = run(args(db, src, "--write", "--expect-hash", "0" * 64), capsys)
    assert code == 2 and "does not match" in out
    assert snapshot(db) == before


@pytest.mark.parametrize("schema", [8, None])
def test_write_applies_exactly_the_plan_and_leaves_other_ciks_untouched(tmp_path, capsys, schema):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path, schema=schema)
    before = snapshot(db)
    con = duckdb.connect(str(db), read_only=True)
    h = repair.plan(con, CIK, repair.read_source(src, CIK))["hash"]
    con.close()
    code, out = run(args(db, src, "--write", "--expect-hash", h), capsys)
    assert code == 0, out
    after = snapshot(db)
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("SELECT ticker, price_date, close_raw, volume_raw FROM prices WHERE cik = ? "
                           "ORDER BY price_date", [CIK]).fetchall() == [
            ("MGPI", D("2026-10-21"), 10.0, 100.0), ("MGPI", D("2026-10-22"), 11.0, None),
            ("MGPI", D("2026-10-23"), 12.0, 300.0), ("CASK", D("2026-10-26"), 14.0, 500.0),
            ("CASK", D("2026-11-02"), 13.5, 600.0), ("CASK", D("2026-11-13"), 15.0, 700.0)]
        assert con.execute("SELECT ex_date, amount_raw FROM dividends WHERE cik = ? ORDER BY 1", [CIK]).fetchall() == [
            (D("2026-08-14"), 0.12), (D("2026-11-13"), 0.13)]
        assert con.execute("SELECT ticker, first_date, last_date FROM price_status WHERE cik = ?", [CIK]).fetchall() == [
            ("CASK", D("2026-10-21"), D("2026-11-13"))]
        assert max(v for (v,) in con.execute("SELECT version FROM schema_migrations").fetchall()) == \
            (schema or max(v for v, _ in MIGRATIONS))             # write mode never migrates
    finally:
        con.close()
    target = f"({CIK},"
    for t in before:            # unrelated CIKs, and every table the repair does not touch, are identical
        assert [r for r in after[t] if not r.startswith(target)] == \
               [r for r in before[t] if not r.startswith(target)], t
        if t in ("prices", "dividends"):
            assert set(before[t]) <= set(after[t]), t       # every live row of the target is kept
        elif t != "price_status":
            assert after[t] == before[t], t


def test_write_twice_is_a_no_op(tmp_path, capsys):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    h = repair.plan(con, CIK, repair.read_source(src, CIK))["hash"]
    con.close()
    assert run(args(db, src, "--write", "--expect-hash", h), capsys)[0] == 0
    once = snapshot(db)
    code, out = run(args(db, src, "--write", "--expect-hash", h), capsys)
    assert code == 0 and "nothing to write" in out
    assert snapshot(db) == once
    con = duckdb.connect(str(db), read_only=True)
    p = repair.plan(con, CIK, repair.read_source(src, CIK))["plan"]
    con.close()
    assert p["prices"]["insert"] == [] and p["dividends"]["insert"] == []
    assert p["prices"]["already_present"] == ["2026-10-21", "2026-10-22", "2026-10-23"]
    assert p["dividends"]["already_present"] == ["2026-08-14"]
    assert p["price_status"] == {"set_first_date": None}


def test_live_change_after_review_refuses_the_write(tmp_path, capsys):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    h = repair.plan(con, CIK, repair.read_source(src, CIK))["hash"]
    con.close()
    con = duckdb.connect(str(db))      # a run stores a correction on a reviewed conflict key
    con.execute("UPDATE prices SET close_raw = 13.6 WHERE cik = ? AND price_date = DATE '2026-11-02'", [CIK])
    con.close()
    before = snapshot(db)
    code, out = run(args(db, src, "--write", "--expect-hash", h), capsys)
    assert code == 2 and "does not match" in out
    assert snapshot(db) == before


def test_live_split_inside_the_restored_range_blocks(tmp_path, capsys):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("INSERT INTO splits VALUES (?, DATE '2026-10-22', 2.0, 'yahoo')", [CIK])
    con.close()
    con = duckdb.connect(str(db), read_only=True)
    m = repair.plan(con, CIK, repair.read_source(src, CIK))
    con.close()
    assert m["plan"]["blocked"] == ["live split 2026-10-22 (ratio 2.0) is inside the restored range "
                                    "and not in the source"]
    before = snapshot(db)
    code, out = run(args(db, src, "--write", "--expect-hash", m["hash"]), capsys)
    assert code == 2 and "blocked" in out
    assert snapshot(db) == before


def test_source_must_match_its_checksums_and_hold_only_the_target_cik(tmp_path):
    src = write_source(tmp_path / "a", SOURCE_PRICES, SOURCE_DIVIDENDS)
    with src["dividends"].open("ab") as f:
        f.write(b"x")
    with pytest.raises(repair.SourceError, match="SHA256"):
        repair.read_source(src, CIK)
    src = write_source(tmp_path / "b", SOURCE_PRICES + [(2, "NVDA", D("2026-10-21"), 1.0, 1.0, "yahoo")],
                       SOURCE_DIVIDENDS)
    with pytest.raises(repair.SourceError, match="other CIK"):
        repair.read_source(src, CIK)
    src = write_source(tmp_path / "c", SOURCE_PRICES + [SOURCE_PRICES[0]], SOURCE_DIVIDENDS)
    with pytest.raises(repair.SourceError, match="duplicate"):
        repair.read_source(src, CIK)


def test_dry_run_refuses_while_a_run_holds_the_lock(tmp_path, capsys):
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    with ops.run_lock(db.parent / ".run.lock"):
        code, out = run(args(db, src), capsys)
    assert code == 3 and "run.lock" in out


def test_restored_history_survives_the_next_refresh_with_its_own_ticker(tmp_path, capsys):
    """The weekly after the repair refreshes CASK; the restored MGPI rows and dividend stay, and
    keep their ticker, so PR #30's ticker_change guards still see two securities."""
    src = write_source(tmp_path / "src", SOURCE_PRICES, SOURCE_DIVIDENDS)
    db = live_db(tmp_path)
    con = duckdb.connect(str(db), read_only=True)
    h = repair.plan(con, CIK, repair.read_source(src, CIK))["hash"]
    con.close()
    assert run(args(db, src, "--write", "--expect-hash", h), capsys)[0] == 0
    con = connect(str(db))
    con.execute("INSERT INTO tickers VALUES (?, 'CASK', 'MGP Ingredients', 'Nasdaq', DATE '2026-11-21')", [CIK])
    con.execute("INSERT INTO companies VALUES (?, 'MGP Ingredients', 2080, NULL, NULL, NULL, NULL, [], ['CASK'], "
                "FALSE, FALSE)", [CIK])
    con.execute("INSERT INTO loaded_filings (cik, accn, n_facts) VALUES (?, 'a', 1)", [CIK])
    idx = pd.DatetimeIndex([pd.Timestamp("2026-11-13"), pd.Timestamp("2026-11-20")])
    cask = pd.DataFrame({"Close": [15.0, 16.0], "Volume": [700.0, 800.0], "Dividends": [0.13, 0.0],
                         "Stock Splits": [0.0, 0.0]}, index=idx)
    prices.load(con, tickers=["CASK"], pause_s=0,
                download=lambda ts, start: {t: cask[cask.index >= pd.Timestamp(start)] for t in ts if t == "CASK"})
    assert con.execute("SELECT ticker, count(*), min(price_date), max(price_date) FROM prices WHERE cik = ? "
                       "GROUP BY 1 ORDER BY 1", [CIK]).fetchall() == [
        ("CASK", 4, D("2026-10-26"), D("2026-11-20")), ("MGPI", 3, D("2026-10-21"), D("2026-10-23"))]
    assert con.execute("SELECT ex_date FROM dividends WHERE cik = ? ORDER BY 1", [CIK]).fetchall() == [
        (D("2026-08-14"),), (D("2026-11-13"),)]
    con.close()
