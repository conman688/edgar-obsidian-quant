"""M11 Phase 2 wiring: fetch-text runs the checks, the health report lists failures, the vault
shows unverified class counts. Mocked SEC client, in-memory DB, temporary vault."""
import argparse
import json
from datetime import date, datetime

import pytest
import requests

from src import cli, ops
from src.db.schema import connect
from src.screen import screen_table
from tests.test_m11_fetch import URLS, FakeSec, both_families, good_docs, ledger
from tests.test_m11_quarantine import AS_OF, CIK, checks, quarantined, stale_share_company
from tests.test_m11_resolve import A, B, D, cover

EMPTY_SUBMISSIONS = json.dumps({"filings": {"recent": {"form": [], "accessionNumber": [], "filingDate": [],
                                                       "reportDate": [], "primaryDocument": []}}}).encode()


@pytest.fixture
def con():
    return connect(":memory:")


def fetch_text(con, sec, monkeypatch, tmp_path, cik=None):
    monkeypatch.setattr(cli, "_client", lambda: sec)
    args = argparse.Namespace(as_of=AS_OF, vault=str(tmp_path), cik=cik)
    cli.cmd_fetch_text(args, con)


def docs_with_submissions():
    d = good_docs()
    d[f"https://data.sec.gov/submissions/CIK{CIK:010d}.json"] = EMPTY_SUBMISSIONS
    return d


def test_fetch_text_runs_the_quarantine_checks(con, monkeypatch, tmp_path, capsys):
    both_families(con)
    sec = FakeSec(docs_with_submissions())
    fetch_text(con, sec, monkeypatch, tmp_path)
    assert ledger(con) == {"m11_balance_sheet": ("ok", "debt_zero"), "m11_cover": ("ok", "classes")}
    assert sum(u in sec.calls for u in URLS.values()) == 4
    assert "quarantine checks" in capsys.readouterr().out


def test_second_fetch_text_makes_no_m11_requests(con, monkeypatch, tmp_path):
    both_families(con)
    fetch_text(con, FakeSec(docs_with_submissions()), monkeypatch, tmp_path)
    sec = FakeSec(docs_with_submissions())
    fetch_text(con, sec, monkeypatch, tmp_path)
    assert not set(URLS.values()) & set(sec.calls)


def test_fetch_text_with_cik_checks_only_those(con, monkeypatch, tmp_path):
    both_families(con)
    docs = docs_with_submissions()
    docs["https://data.sec.gov/submissions/CIK0000000999.json"] = EMPTY_SUBMISSIONS
    sec = FakeSec(docs)
    fetch_text(con, sec, monkeypatch, tmp_path, cik=[999])
    assert not set(URLS.values()) & set(sec.calls)
    assert con.execute("SELECT count(*) FROM quarantine_checks").fetchone()[0] == 0


def test_resolution_counts_from_the_next_screen(con, monkeypatch, tmp_path):
    # Open question 1, as the plan recommends: fetch-text runs after screen, so a resolution counts
    # from the next screen (one week of lag in the weekly job). Not yet decided by the user.
    both_families(con)
    before = {r["ticker"]: r["status"] for r in screen_table(con, AS_OF).to_pylist()}
    fetch_text(con, FakeSec(docs_with_submissions()), monkeypatch, tmp_path)
    after = next(r for r in screen_table(con, AS_OF).to_pylist() if r["ticker"] == "MIR")
    assert before["MIR"] == "quarantine"
    assert "missing_debt" not in after["issues"] and after["debt"] == 0


def test_health_warning_lists_failed_quarantine_checks(con, monkeypatch, tmp_path):
    both_families(con)
    docs = docs_with_submissions()
    docs[URLS["fs"]] = requests.ConnectionError("dropped")
    fetch_text(con, FakeSec(docs), monkeypatch, tmp_path)
    w = ops._warnings(con, AS_OF, {}, {}, [], 0, 0, None)
    assert "1 quarantine checks failed (retried next run)" in w


def test_unverified_classes_show_their_counts_for_manual_review(con):
    quarantined(con, CIK, "MIR", stale_share_company())
    cover(con, [(A, 239_732_572, "MIR", D), (B, 5_759_555, None, D)], ["MIR"])
    r = next(r for r in screen_table(con, AS_OF).to_pylist() if r["ticker"] == "MIR")
    c = checks(r)["multi_class_unverified"]
    assert c["reason"] == "cover_classes"
    assert "CommonClassAMember 239,732,572 (MIR)" in c["message"] and "CommonClassBMember 5,759,555 (no symbol)" in c["message"]
    assert "q2" in c["message"]
