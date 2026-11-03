"""Run ledger: claim a unit of work, then mark it done. Survives crashes and restarts."""
from __future__ import annotations

import duckdb


def is_done(con: duckdb.DuckDBPyConnection, key: str, stage: str, schema_version: int) -> bool:
    row = next(iter(con.execute(
        "SELECT status FROM ledger WHERE accn = ? AND stage = ? AND schema_version = ? AND model = ''",
        [key, stage, schema_version],
    ).fetchall()), None)
    return row is not None and row[0] == "done"


def mark(con: duckdb.DuckDBPyConnection, key: str, stage: str, schema_version: int, status: str,
         input_sha256: str | None = None, output_ref: str | None = None, error: str | None = None) -> None:
    con.execute(
        "INSERT OR REPLACE INTO ledger VALUES (?, ?, ?, '', ?, ?, ?, ?, current_timestamp)",
        [key, stage, schema_version, input_sha256, status, output_ref, error],
    )
