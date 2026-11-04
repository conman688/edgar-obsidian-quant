"""Paths and SEC endpoints. Only the User-Agent comes from the environment."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("EOQ_DATA_DIR", "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "edgar.duckdb"
VAULT_DIR = Path(os.environ["EOQ_VAULT_DIR"]) if os.environ.get("EOQ_VAULT_DIR") else None

# DuckDB defaults to 80% of RAM and may spill until the disk is nearly full. On an
# 8 GB laptop with little free disk that can exhaust swap and crash the machine.
DB_MEMORY_LIMIT = os.environ.get("EOQ_DB_MEMORY", "2GB")
DB_TEMP_MAX = os.environ.get("EOQ_DB_TEMP_MAX", "4GB")
DB_THREADS = int(os.environ.get("EOQ_DB_THREADS", "4"))

COMPANYFACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SUBMISSIONS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


def sec_user_agent() -> str:
    """SEC fair-access policy requires a User-Agent naming you and a contact email."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        raise SystemExit("Set SEC_USER_AGENT, e.g. 'Your Name you@example.com' (see .env.example)")
    return ua
