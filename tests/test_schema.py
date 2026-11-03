"""Schema migrations: fresh databases, pre-migration databases, new and failing migrations."""

import duckdb
import pytest

from src.db.schema import BASELINE_DDL, CONCEPT_SEED, MIGRATIONS, connect, migrate, sync_concept_map

ADD_COUNTRY = (2, "ALTER TABLE companies ADD COLUMN country VARCHAR")


def versions(con) -> list[int]:
    return [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]


def columns(con, table: str) -> set[str]:
    return {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}


def test_fresh_database_gets_every_migration():
    con = connect(":memory:")
    assert versions(con) == [v for v, _ in MIGRATIONS]
    assert {"person", "person_issuer", "xbrl_facts"} <= {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    con.execute("INSERT INTO splits VALUES (1, DATE '2024-06-01', 0.1, 't')")
    assert con.execute("SELECT split_factor(1, DATE '2024-01-01')").fetchall()[0][0] == pytest.approx(0.1)


def test_pre_migration_database_is_adopted_without_data_loss(tmp_path):
    db = str(tmp_path / "legacy.duckdb")
    legacy = duckdb.connect(db)   # M1 layout: no schema_migrations, no person tables
    legacy.execute(BASELINE_DDL.split("CREATE TABLE IF NOT EXISTS person (")[0])
    legacy.execute("INSERT INTO companies VALUES (7, 'Acme', 3571, NULL, '1231', NULL, NULL, [], ['ACM'], FALSE, FALSE)")
    legacy.close()

    con = connect(db)
    assert versions(con) == [1]
    assert con.execute("SELECT name FROM companies WHERE cik = 7").fetchall() == [("Acme",)]
    assert "person" in {r[0] for r in con.execute("SHOW TABLES").fetchall()}


def test_new_migration_runs_once_on_existing_data(tmp_path):
    db = str(tmp_path / "e.duckdb")
    con = connect(db)
    con.execute("INSERT INTO companies VALUES (7, 'Acme', 3571, NULL, '1231', NULL, NULL, [], ['ACM'], FALSE, FALSE)")
    assert migrate(con, MIGRATIONS + [ADD_COUNTRY]) == [2]
    assert migrate(con, MIGRATIONS + [ADD_COUNTRY]) == []
    assert "country" in columns(con, "companies")
    assert con.execute("SELECT name, country FROM companies").fetchall() == [("Acme", None)]


def test_failing_migration_rolls_back_and_is_not_recorded():
    con = connect(":memory:")
    bad = (2, "ALTER TABLE companies ADD COLUMN half_done INTEGER; SELECT * FROM no_such_table")
    with pytest.raises(duckdb.Error):
        migrate(con, MIGRATIONS + [bad])
    assert versions(con) == [1]
    assert "half_done" not in columns(con, "companies")
    assert migrate(con, MIGRATIONS + [ADD_COUNTRY]) == [2]   # fixed migration still applies


def test_database_newer_than_code_is_refused(tmp_path):
    db = str(tmp_path / "n.duckdb")
    con = connect(db)
    con.execute("INSERT INTO schema_migrations (version) VALUES (99)")
    con.close()
    with pytest.raises(RuntimeError, match="version 99"):
        connect(db)


def test_concept_map_follows_code():
    con = connect(":memory:")
    con.execute("DELETE FROM concept_map WHERE concept = 'revenue' AND priority = 1")
    con.execute("UPDATE concept_map SET priority = 9 WHERE concept = 'cash' AND priority = 1")
    con.execute("INSERT INTO concept_map VALUES ('stale', 'us-gaap', 'Old', 'USD', 1)")
    sync_concept_map(con)
    assert {tuple(r) for r in con.execute("SELECT * FROM concept_map").fetchall()} == set(CONCEPT_SEED)
