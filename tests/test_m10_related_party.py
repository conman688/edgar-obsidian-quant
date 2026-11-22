"""M10 footnote checks, related-party only: the RelatedPartyTransactionsDisclosureTextBlock of
each vault company's latest two 10-Ks, against the prior 10-K with M7's redline.

Synthetic XBRL instances and a fake SEC client only: no network, no live database."""
import json
from argparse import Namespace
from datetime import date
from xml.sax.saxutils import escape

import pytest

from src import ops
from src.db.schema import connect
from src.ingestion.filing_text import fetch_sections, html_text
from src.ingestion.related_party import PARSER, SECTION, fetch_related_party, select_block
from src.redline import related_party_redline
from tests.test_failure_modes import CIK
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company
from tests.test_m7_text import RISKS_2023, SUBS, FakeSec, doc_url, submissions, tenk_html

ELSEWHERE = "related-party note found only for another period or entity; not compared"
CONFLICT = ("related-party note could not be uniquely identified: two different current company-wide notes; "
            "not compared")
UNTAGGED = "no related-party note tagged in this 10-k"

SHORT = ("<div><p><b>Note 9. Related Parties</b></p>"
         "<p>We lease our main office from our chief executive officer for $10,000 a month.</p></div>")
LONG = ("<div><p><b>Note 9. Related Parties</b></p>"
        "<p>We lease our main office from our chief executive officer for $10,000 a month.</p>"
        "<p>During the prior period we also bought raw materials from a company owned by a director, "
        "for a total of $2.4 million, on terms we believe were at arm's length.</p>"
        "<p>A subsidiary pays a management fee to its minority shareholder under a services agreement.</p></div>")
NEW = "In 2023 we lent $1.5 million to our chief financial officer, due in 2026 at 4% interest."

XBRLI = "http://www.xbrl.org/2003/instance"


def _context(cid: str, end: str, start: str | None, entity: int = CIK, member: bool = False,
             scenario: bool = False) -> str:
    seg = ('<xbrli:segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">acme:SubsidiaryMember'
           '</xbrldi:explicitMember></xbrli:segment>') if member else ""
    scen = ('<xbrli:scenario><xbrldi:explicitMember dimension="srt:RestatementAxis">srt:ScenarioPreviouslyReportedMember'
            '</xbrldi:explicitMember></xbrli:scenario>') if scenario else ""
    period = f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>" if start \
        else f"<xbrli:instant>{end}</xbrli:instant>"
    return (f'<xbrli:context id="{cid}"><xbrli:entity>'
            f'<xbrli:identifier scheme="http://www.sec.gov/CIK">{entity:010d}</xbrli:identifier>{seg}'
            f'</xbrli:entity><xbrli:period>{period}</xbrli:period>{scen}</xbrli:context>')


def instance(year: int, blocks: list[tuple[str, str]], dped: str | None = "default") -> bytes:
    """An instance for fiscal `year` (calendar). blocks: (context, html) with context one of
    cur (own, no dimensions, ends on DocumentPeriodEndDate), prior (prior year), sub (a
    LegalEntityAxis member), scen (a scenario), other (another entity's CIK), inst (an
    instant on the period end: own, no dimensions, also current)."""
    end = f"{year}-12-31"
    dped = end if dped == "default" else dped
    ctxs = {"cur": _context("FY", end, f"{year}-01-01"),
            "prior": _context("PY", f"{year - 1}-12-31", f"{year - 1}-01-01"),
            "sub": _context("FY_sub", end, f"{year}-01-01", member=True),
            "scen": _context("FY_scen", end, f"{year}-01-01", scenario=True),
            "other": _context("FY_other", end, f"{year}-01-01", entity=9999),
            "inst": _context("I", end, None)}
    ids = {"cur": "FY", "prior": "PY", "sub": "FY_sub", "scen": "FY_scen", "other": "FY_other", "inst": "I"}
    facts = "".join(f'<us-gaap:RelatedPartyTransactionsDisclosureTextBlock contextRef="{ids[c]}" id="rp{i}">'
                    f"{escape(h)}</us-gaap:RelatedPartyTransactionsDisclosureTextBlock>"
                    for i, (c, h) in enumerate(blocks))
    dped_fact = f'<dei:DocumentPeriodEndDate contextRef="FY">{dped}</dei:DocumentPeriodEndDate>' if dped else ""
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<xbrli:xbrl xmlns:xbrli="{XBRLI}" '
            'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:dei="http://xbrl.sec.gov/dei/2023" '
            'xmlns:us-gaap="http://fasb.org/us-gaap/2023" xmlns:srt="http://fasb.org/srt/2023" '
            'xmlns:acme="http://acme.example/2023">'
            + "".join(ctxs.values()) + dped_fact
            + '<us-gaap:Revenues contextRef="FY" unitRef="usd" decimals="-3">1000</us-gaap:Revenues>'
            + facts + "</xbrli:xbrl>").encode()


def text_of(html: str) -> str:
    return html_text(html.encode())


# --- context selection (review C5, C6) ----------------------------------------------

@pytest.mark.parametrize("other", ["prior", "sub", "scen", "other"])
def test_short_current_block_wins_over_a_longer_block_in_another_context(other):
    for blocks in ([("cur", SHORT), (other, LONG)], [(other, LONG), ("cur", SHORT)]):
        assert select_block(instance(2023, blocks), CIK) == ("ok", text_of(SHORT))


def test_identical_repeats_count_once():
    assert select_block(instance(2023, [("cur", SHORT), ("cur", SHORT), ("inst", SHORT)]), CIK) == \
        ("ok", text_of(SHORT))


@pytest.mark.parametrize("blocks", [[("prior", LONG)], [("sub", LONG)], [("scen", LONG)], [("other", LONG)],
                                    [("prior", LONG), ("sub", SHORT)]])
def test_blocks_only_in_other_contexts_are_ambiguous_elsewhere(blocks):
    assert select_block(instance(2023, blocks), CIK) == ("ambiguous", "elsewhere")


def test_two_different_current_blocks_are_a_conflict():
    assert select_block(instance(2023, [("cur", SHORT), ("cur", LONG)]), CIK) == ("ambiguous", "conflict")
    assert select_block(instance(2023, [("cur", SHORT), ("inst", LONG), ("prior", LONG)]), CIK) == \
        ("ambiguous", "conflict")


def test_no_block_in_any_context_is_not_found():
    assert select_block(instance(2023, []), CIK) == ("not_found", None)


def test_the_block_html_becomes_plain_text():
    status, text = select_block(instance(2023, [("cur", SHORT)]), CIK)
    assert status == "ok"
    assert text == "Note 9. Related Parties\nWe lease our main office from our chief executive officer for $10,000 a month."


def test_an_instance_without_a_document_period_end_date_is_an_error():
    with pytest.raises(ValueError, match="DocumentPeriodEndDate"):
        select_block(instance(2023, [("cur", SHORT)], dped=None), CIK)
    assert select_block(instance(2023, [], dped=None), CIK) == ("not_found", None)   # nothing to place


# --- fetch ---------------------------------------------------------------------------

A24, A23, A22 = "0000001234-24-000010", "0000001234-23-000010", "0000001234-22-000010"
FILINGS = [("10-K", A24, "2024-03-01", "2023-12-31", "k2023.htm"),
           ("10-Q", "0000001234-23-000020", "2023-11-01", "2023-09-30", "q3.htm"),
           ("10-K", A23, "2023-03-01", "2022-12-31", "k2022.htm"),
           ("10-K", A22, "2022-03-01", "2021-12-31", "k2021.htm")]
YEAR = {A24: 2023, A23: 2022, A22: 2021}
PRIMARY = {A24: "k2023.htm", A23: "k2022.htm", A22: "k2021.htm"}


def index_url(accn: str) -> str:
    return doc_url(accn, "index.json")


def inst_url(accn: str) -> str:
    return doc_url(accn, f"acme-{YEAR[accn]}1231_htm.xml")


def index_json(names: list[str]) -> bytes:
    return json.dumps({"directory": {"name": "/Archives/edgar/data/1234/x", "item": [
        {"name": n, "type": "text.gif", "size": "1"} for n in names]}}).encode()


def rp_docs(blocks: dict[str, list[tuple[str, str]]]) -> dict[str, bytes]:
    """Submissions, plus for each 10-K its index (primary document, R files, one instance) and
    its instance with `blocks[accn]`, plus the primary documents for risk factors."""
    docs = {SUBS: submissions(CIK, FILINGS)}
    for accn in (A24, A23, A22):
        name = inst_url(accn).rsplit("/", 1)[1]
        docs[index_url(accn)] = index_json([PRIMARY[accn], "R1.htm", name, f"acme-{YEAR[accn]}1231.xsd",
                                            "Financial_Report.xlsx"])
        docs[inst_url(accn)] = instance(YEAR[accn], blocks.get(accn, []))
        docs[doc_url(accn, PRIMARY[accn])] = tenk_html(RISKS_2023)
    return docs


CUR_TEXT = SHORT.replace("</div>", f"<p>{NEW}</p></div>")


def standard() -> dict[str, list[tuple[str, str]]]:
    return {A24: [("cur", CUR_TEXT)], A23: [("cur", SHORT), ("prior", LONG)], A22: [("cur", SHORT)]}


def rp_rows(con) -> list[tuple]:
    return con.execute("SELECT accn, form, filed, report_date, status, parser FROM filing_sections "
                       "WHERE section = ? ORDER BY filed", [SECTION]).fetchall()


def test_fetch_reads_only_10ks_filed_by_as_of_and_never_twice():
    con, sec = connect(":memory:"), FakeSec(rp_docs(standard()))
    assert fetch_related_party(con, sec, [CIK], date(2023, 12, 31)) == {"ok": 2}
    assert not any(A24.replace("-", "") in u for u in sec.calls)                 # filed after as_of
    assert sec.calls == [SUBS, index_url(A23), inst_url(A23), index_url(A22), inst_url(A22)]
    assert rp_rows(con) == [(A22, "10-K", date(2022, 3, 1), date(2021, 12, 31), "ok", PARSER),
                            (A23, "10-K", date(2023, 3, 1), date(2022, 12, 31), "ok", PARSER)]
    assert con.execute("SELECT text, url FROM filing_sections WHERE accn = ? AND section = ?",
                       [A23, SECTION]).fetchall() == [(text_of(SHORT), inst_url(A23))]
    sec.calls.clear()
    assert fetch_related_party(con, sec, [CIK], date(2023, 12, 31)) == {}
    assert sec.calls == [SUBS]                                                    # index only, no documents
    assert con.execute("SELECT count(*) FROM filing_sections WHERE section = 'risk_factors'").fetchall() == [(0,)]


def test_the_instance_is_found_by_index_whatever_its_name():
    docs = rp_docs(standard())
    odd = doc_url(A24, "darkpulse_i10k-123125_htm.xml")
    docs[index_url(A24)] = index_json(["k2023.htm", "darkpulse_i10k-123125_htm.xml", "R2.htm"])
    docs[odd] = docs.pop(inst_url(A24))
    con, sec = connect(":memory:"), FakeSec(docs)
    fetch_related_party(con, sec, [CIK], date(2024, 6, 30))
    assert odd in sec.calls
    assert con.execute("SELECT status, url FROM filing_sections WHERE accn = ? AND section = ?",
                       [A24, SECTION]).fetchall() == [("ok", odd)]


def test_comparison_uses_the_latest_two_10ks_filed_by_as_of():
    con = connect(":memory:")
    fetch_related_party(con, FakeSec(rp_docs(standard())), [CIK], date(2023, 12, 31))
    fetch_related_party(con, FakeSec(rp_docs(standard())), [CIK], date(2024, 6, 30))
    r = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert (r["accn"], r["prior_accn"], r["status"], r["compared"]) == (A24, A23, "ok", True)
    assert r["new"] == [NEW] and r["removed"] == [] and r["text"] == text_of(CUR_TEXT)
    early = related_party_redline(con, [CIK], date(2023, 12, 31))[CIK]          # A24 not filed yet
    assert (early["accn"], early["prior_accn"], early["new"]) == (A23, A22, [])
    assert related_party_redline(con, [CIK], date(2022, 1, 1)) == {}


def test_a_block_only_in_the_current_10k_shows_its_text_with_no_comparison():
    con = connect(":memory:")
    fetch_related_party(con, FakeSec(rp_docs({A24: [("cur", SHORT)]})), [CIK], date(2024, 6, 30))
    r = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert r["status"] == "ok" and r["text"] == text_of(SHORT)
    assert r["compared"] is False and r["prior_status"] == "not_found" and r["new"] == []


def test_a_10k_without_the_tag_is_not_found_shown_as_untagged_and_not_fetched_again():
    con, sec = connect(":memory:"), FakeSec(rp_docs({A23: [("cur", SHORT)]}))
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {"ok": 1, "not_found": 1}
    r = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert r["status"] == "not_found" and r["message"] == UNTAGGED.replace("10-k", "10-K")
    assert r["text"] is None and r["compared"] is False
    sec.calls.clear()
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {} and sec.calls == [SUBS]


@pytest.mark.parametrize("reason,blocks,message", [
    ("elsewhere", [("prior", LONG), ("sub", SHORT)], ELSEWHERE),
    ("conflict", [("cur", SHORT), ("cur", LONG)], CONFLICT)])
def test_ambiguous_current_10k_is_never_absent_and_has_its_own_message(reason, blocks, message):
    con = connect(":memory:")
    assert fetch_related_party(con, FakeSec(rp_docs({A24: blocks, A23: [("cur", SHORT)]})), [CIK],
                               date(2024, 6, 30)) == {"ambiguous": 1, "ok": 1}
    r = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert (r["status"], r["reason"], r["message"]) == ("ambiguous", reason, message)
    assert r["compared"] is False and r["text"] is None and r["new"] == []
    other = CONFLICT if reason == "elsewhere" else ELSEWHERE
    assert r["message"] != other and "tagged" not in r["message"]


def test_an_ambiguous_prior_10k_means_no_comparison():
    con = connect(":memory:")
    fetch_related_party(con, FakeSec(rp_docs({A24: [("cur", CUR_TEXT)], A23: [("cur", SHORT), ("cur", LONG)]})),
                        [CIK], date(2024, 6, 30))
    r = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert (r["status"], r["prior_status"], r["compared"], r["new"]) == ("ok", "ambiguous", False, [])


def test_ambiguous_is_final_for_the_parser_version_and_fetched_again_after_a_bump():
    con, sec = connect(":memory:"), FakeSec(rp_docs({A24: [("cur", SHORT), ("cur", LONG)], A23: [("cur", SHORT)]}))
    fetch_related_party(con, sec, [CIK], date(2024, 6, 30))
    sec.calls.clear()
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {} and sec.calls == [SUBS]
    con.execute("UPDATE filing_sections SET parser = ? WHERE accn = ? AND section = ?", [PARSER - 1, A24, SECTION])
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {"ambiguous": 1}
    assert sec.calls[1:] == [SUBS, index_url(A24), inst_url(A24)]


def _no_instance(docs):
    docs[index_url(A24)] = index_json(["k2023.htm", "R1.htm", "acme-20231231.xsd"])


def _two_instances(docs):
    docs[index_url(A24)] = index_json(["k2023.htm", "acme-20231231_htm.xml", "acme-20231231b_htm.xml"])


def _no_index(docs):
    del docs[index_url(A24)]


def _instance_fails(docs):
    del docs[inst_url(A24)]


def _malformed(docs):
    docs[inst_url(A24)] = docs[inst_url(A24)][:-40]


def _entities(docs):
    docs[inst_url(A24)] = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
                           + docs[inst_url(A24)].split(b"?>", 1)[1])


@pytest.mark.parametrize("break_it", [_no_instance, _two_instances, _no_index, _instance_fails, _malformed,
                                      _entities])
def test_failed_reads_leave_an_error_row_that_is_retried_and_reported(break_it):
    docs = rp_docs(standard())
    break_it(docs)
    con, sec = connect(":memory:"), FakeSec(docs)
    r = ops.run_steps(con, "weekly", [("fetch-text", lambda: fetch_related_party(con, sec, [CIK], date(2024, 6, 30)))],
                      date(2024, 6, 30))
    assert r["status"] == "ok"                                     # a bad instance never stops the run
    row = con.execute("SELECT status, text FROM filing_sections WHERE accn = ? AND section = ?",
                      [A24, SECTION]).fetchall()
    assert row[0][0] == "error" and row[0][1]                        # the reason is kept
    assert any("related-party" in w and "1 " in w and "retried next run" in w for w in r["warnings"]), r["warnings"]
    shown = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert shown["status"] == "error" and "could not be read" in shown["message"]
    assert "tagged" not in shown["message"] and shown["compared"] is False
    # Retried on the next run; once SEC serves it, the row is ok and the warning goes.
    sec.docs = rp_docs(standard())
    sec.calls.clear()
    r2 = ops.run_steps(con, "weekly", [("fetch-text", lambda: fetch_related_party(con, sec, [CIK], date(2024, 6, 30)))],
                       date(2024, 6, 30))
    assert index_url(A24) in sec.calls and index_url(A23) not in sec.calls
    assert not any("related-party" in w for w in r2["warnings"])
    assert related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]["new"] == [NEW]


def test_an_old_error_row_is_not_reported_by_a_run_that_did_not_retry_it():
    docs = rp_docs(standard())
    _no_instance(docs)
    con = connect(":memory:")
    fetch_related_party(con, FakeSec(docs), [CIK], date(2024, 6, 30))
    r = ops.run_steps(con, "daily", [], date(2024, 6, 30))                    # daily: no fetch-text
    assert not any("related-party" in w for w in r["warnings"])


class Crash(BaseException):
    """Stands in for a kill between the two families (not caught as a failed request)."""


class CrashingSec(FakeSec):
    def get(self, url):
        if url.endswith("index.json"):
            raise Crash(url)
        return super().get(url)


def test_a_crash_after_risk_factors_commit_fills_related_party_next_run_without_the_document():
    con = connect(":memory:")
    assert fetch_sections(con, FakeSec(rp_docs(standard())), [CIK], date(2024, 6, 30)) == {"ok": 2}
    with pytest.raises(Crash):
        fetch_related_party(con, CrashingSec(rp_docs(standard())), [CIK], date(2024, 6, 30))
    assert rp_rows(con) == []
    sec = FakeSec(rp_docs(standard()))
    assert fetch_sections(con, sec, [CIK], date(2024, 6, 30)) == {}
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {"ok": 2}
    assert not any(u.endswith((".htm",)) for u in sec.calls)                   # no primary document again
    assert [r[0] for r in rp_rows(con)] == [A23, A24]


def test_complete_related_party_rows_never_stop_missing_risk_factors_and_the_other_way_round():
    con = connect(":memory:")
    fetch_related_party(con, FakeSec(rp_docs(standard())), [CIK], date(2024, 6, 30))
    sec = FakeSec(rp_docs(standard()))
    assert fetch_sections(con, sec, [CIK], date(2024, 6, 30)) == {"ok": 2}          # not skipped
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {}
    assert not any(u.endswith("_htm.xml") or u.endswith("index.json") for u in sec.calls)
    con.execute("DELETE FROM filing_sections WHERE section = ? AND accn = ?", [SECTION, A24])
    sec.calls.clear()
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {"ok": 1}
    assert fetch_sections(con, sec, [CIK], date(2024, 6, 30)) == {}
    assert sec.calls == [SUBS, index_url(A24), inst_url(A24), SUBS]


def test_stale_when_the_report_date_is_more_than_470_days_before_as_of():
    con = connect(":memory:")
    fetch_related_party(con, FakeSec(rp_docs(standard())), [CIK], date(2024, 6, 30))
    assert related_party_redline(con, [CIK], date(2025, 4, 14))[CIK]["stale"] is False      # 2023-12-31 + 470
    assert related_party_redline(con, [CIK], date(2025, 4, 15))[CIK]["stale"] is True       # + 471


# --- fetch-text ----------------------------------------------------------------------

def test_fetch_text_runs_related_party_and_the_weekly_steps_do_not_change(monkeypatch, capsys):
    from src import cli
    con, sec = connect(":memory:"), FakeSec(rp_docs(standard()))
    monkeypatch.setattr(cli, "_client", lambda: sec)
    cli.cmd_fetch_text(Namespace(vault=None, cik=[CIK], as_of=date(2024, 6, 30)), con)
    assert [r[4] for r in rp_rows(con)] == ["ok", "ok"]
    assert con.execute("SELECT count(*) FROM filing_sections WHERE section = 'risk_factors'").fetchall() == [(2,)]
    assert "related-party notes" in capsys.readouterr().out
    assert cli.RUN_STEPS["weekly"] == [["ingest", "all"], ["screen"], ["fetch-text"], ["sync-vault"]]
    sec.calls.clear()
    cli.cmd_fetch_text(Namespace(vault=None, cik=[CIK], as_of=date(2024, 6, 30)), con)
    assert set(sec.calls) == {SUBS}                                                  # 0 documents


# --- vault ---------------------------------------------------------------------------

@pytest.fixture
def vcon():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())
    add_buy(c, CIK, "0001234-24-000001")
    return c


def put(con, accn: str, filed: date, report: date, status: str, text: str | None):
    con.execute("INSERT INTO filing_sections VALUES (?, ?, '10-K', ?, ?, ?, ?, ?, ?, now(), ?)",
                [CIK, accn, filed, report, SECTION, status, text, f"https://sec.example/{accn}_htm.xml", PARSER])


def fm(path):
    from tests.test_m6_vault import fm as _fm
    return _fm(path)


def test_company_note_shows_related_parties_and_resync_writes_nothing(vcon, tmp_path):
    from src.obsidian.sync import sync_vault
    put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), "ok", text_of(SHORT))
    put(vcon, "0001234-24-000010", date(2024, 3, 1), date(2023, 12, 31), "ok", text_of(CUR_TEXT))
    sync_vault(vcon, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    text = note.read_text()
    body = text.split("## Related parties", 1)[1].split("\n## ", 1)[0]
    assert "10-K only" in body and "2023-12-31" in body and "2024-03-01" in body
    assert "[[0001234-24-000010]]" in body and "[[0001234-23-000010]]" in body
    assert f"- {NEW}" in body and "**1 new**" in body
    assert "> We lease our main office from our chief executive officer" in body        # full text, folded
    assert "https://sec.example/0001234-24-000010_htm.xml" in body
    assert fm(note)["related_party_new"] == 1
    assert "related parties" in (tmp_path / "Filings" / "0001234-24-000010.md").read_text()
    assert not sum(sync_vault(vcon, tmp_path, AS_OF).written.values())


@pytest.mark.parametrize("prior", [None, ("not_found", None), ("ambiguous", "conflict"), ("error", "HTTP 500")])
def test_no_comparison_leaves_related_party_new_empty(vcon, tmp_path, prior):
    from src.obsidian.sync import sync_vault
    if prior:
        put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), *prior)
    put(vcon, "0001234-24-000010", date(2024, 3, 1), date(2023, 12, 31), "ok", text_of(SHORT))
    sync_vault(vcon, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    body = note.read_text().split("## Related parties", 1)[1].split("\n## ", 1)[0]
    assert "No comparison" in body and "> We lease our main office" in body
    assert fm(note)["related_party_new"] is None


@pytest.mark.parametrize("status,text,shown", [
    ("not_found", None, UNTAGGED),
    ("ambiguous", "elsewhere", ELSEWHERE.lower()),
    ("ambiguous", "conflict", CONFLICT.lower()),
    ("error", "HTTPError: 500", "could not be read")])
def test_note_never_shows_a_missing_or_unclear_note_as_no_dealings(vcon, tmp_path, status, text, shown):
    from src.obsidian.sync import sync_vault
    put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), "ok", text_of(SHORT))
    put(vcon, "0001234-24-000010", date(2024, 3, 1), date(2023, 12, 31), status, text)
    sync_vault(vcon, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    body = note.read_text().split("## Related parties", 1)[1].split("\n## ", 1)[0].lower()
    assert shown in body and "no related-party dealings" not in body
    assert "we lease our main office" not in body                              # the prior note is not current
    assert fm(note)["related_party_new"] is None


def test_note_without_any_related_party_row_says_not_fetched(vcon, tmp_path):
    from src.obsidian.sync import sync_vault
    sync_vault(vcon, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    body = note.read_text().split("## Related parties", 1)[1].split("\n## ", 1)[0]
    assert "not fetched yet" in body and "not covered" in body
    assert fm(note)["related_party_new"] is None


def test_stale_10k_is_marked_in_the_note(vcon, tmp_path):
    from src.obsidian.sync import sync_vault
    put(vcon, "0001234-22-000010", date(2022, 3, 1), date(2021, 12, 31), "ok", text_of(SHORT))
    put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), "ok", text_of(CUR_TEXT))
    sync_vault(vcon, tmp_path, AS_OF)                                           # 2022-12-31 is 612 days back
    body = (tmp_path / "Companies" / "ACME.md").read_text().split("## Related parties", 1)[1].split("\n## ", 1)[0]
    assert "Stale" in body and "470 days" in body


def test_screen_and_notes_status_rank_are_identical_with_and_without_m10_data(vcon, tmp_path):
    from src.obsidian.sync import sync_vault
    from src.screen import screen
    before = screen(vcon, AS_OF).to_arrow_table().to_pylist()
    sync_vault(vcon, tmp_path / "a", AS_OF)
    put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), "ok", text_of(SHORT))
    put(vcon, "0001234-24-000010", date(2024, 3, 1), date(2023, 12, 31), "ambiguous", "conflict")
    assert screen(vcon, AS_OF).to_arrow_table().to_pylist() == before
    sync_vault(vcon, tmp_path / "b", AS_OF)
    a, b = fm(tmp_path / "a" / "Companies" / "ACME.md"), fm(tmp_path / "b" / "Companies" / "ACME.md")
    assert (a["status"], a["rank"], a["issues"], a["flags"]) == (b["status"], b["rank"], b["issues"], b["flags"])
    assert a["status"] == "shortlist"


def test_sync_never_touches_journal_notes(vcon, tmp_path):
    from src.obsidian.sync import sync_vault, write_journal
    path = write_journal(vcon, tmp_path, "ACME", AS_OF)
    before = path.read_bytes()
    put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), "ok", text_of(SHORT))
    put(vcon, "0001234-24-000010", date(2024, 3, 1), date(2023, 12, 31), "ok", text_of(CUR_TEXT))
    s = sync_vault(vcon, tmp_path, AS_OF)
    assert path.read_bytes() == before and "Journal" not in s.written


# --- review round 4 (Codex C7-C10) ---------------------------------------------------

PENDING = "has not been read yet"


def _rf_and_rp(rp_dates: list[date], drop: str | None = None):
    """Risk-factor rows for A24, A23, A22 (the annual filings known locally); related-party rows
    only from fetches on `rp_dates`, minus `drop`."""
    con, docs = connect(":memory:"), rp_docs(standard())
    for d in (date(2023, 12, 31), date(2024, 6, 30)):
        fetch_sections(con, FakeSec(docs), [CIK], d)
    for d in rp_dates:
        fetch_related_party(con, FakeSec(docs), [CIK], d)
    if drop:
        con.execute("DELETE FROM filing_sections WHERE section = ? AND accn = ?", [SECTION, drop])
    return con


def test_a_newest_10k_without_a_related_party_row_is_pending_not_replaced_by_an_older_one():   # C7
    con = _rf_and_rp([date(2023, 12, 31)])                       # M10 rows for A23 and A22 only
    r = related_party_redline(con, [CIK], date(2024, 3, 2))[CIK]
    assert (r["accn"], r["status"], r["compared"], r["text"]) == (A24, "pending", False, None)
    assert PENDING in r["message"] and r["report_date"] == date(2023, 12, 31) and r["new"] == []
    before = related_party_redline(con, [CIK], date(2024, 2, 29))[CIK]          # A24 not filed yet
    assert (before["accn"], before["prior_accn"], before["status"], before["compared"]) == (A23, A22, "ok", True)


def test_a_missing_middle_related_party_row_means_no_comparison():                            # C7
    con = _rf_and_rp([date(2023, 12, 31), date(2024, 6, 30)], drop=A23)
    r = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert (r["accn"], r["prior_accn"], r["status"], r["prior_status"]) == (A24, A23, "ok", "pending")
    assert r["compared"] is False and PENDING in r["no_comparison"] and r["new"] == []
    before = related_party_redline(con, [CIK], date(2024, 2, 29))[CIK]
    assert (before["accn"], before["status"], before["compared"]) == (A23, "pending", False)


def test_note_shows_a_pending_newest_10k_and_not_the_older_note(vcon, tmp_path):              # C7
    from src.obsidian.sync import sync_vault
    vcon.execute("INSERT INTO filing_sections VALUES (?, '0001234-24-000010', '10-K', DATE '2024-03-01', "
                 "DATE '2023-12-31', 'risk_factors', 'ok', 'Risk.', 'u', now(), 2)", [CIK])
    put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), "ok", text_of(SHORT))
    sync_vault(vcon, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    body = note.read_text().split("## Related parties", 1)[1].split("\n## ", 1)[0]
    assert "[[0001234-24-000010]]" in body and PENDING in body
    assert "We lease our main office" not in body and fm(note)["related_party_new"] is None


def _html_response(docs):
    docs[inst_url(A24)] = b"<html><body>Response unavailable.</body></html>"


def test_a_well_formed_non_instance_response_is_an_error_and_retried():                      # C8
    docs = rp_docs(standard())
    _html_response(docs)
    con, sec = connect(":memory:"), FakeSec(docs)
    r = ops.run_steps(con, "weekly", [("fetch-text", lambda: fetch_related_party(con, sec, [CIK], date(2024, 6, 30)))],
                      date(2024, 6, 30))
    assert con.execute("SELECT status FROM filing_sections WHERE accn = ? AND section = ?",
                       [A24, SECTION]).fetchall() == [("error",)]
    assert any("related-party" in w for w in r["warnings"])
    sec.docs = rp_docs(standard())
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {"ok": 1}
    assert select_block(instance(2023, []), CIK) == ("not_found", None)                # control: real absence


NIL = '<us-gaap:RelatedPartyTransactionsDisclosureTextBlock contextRef="FY" xsi:nil="true"/>'


def _with_nil(raw: bytes) -> bytes:
    return (raw.replace(b"<xbrli:xbrl ", b'<xbrli:xbrl xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" ', 1)
            .replace(b"</xbrli:xbrl>", NIL.encode() + b"</xbrli:xbrl>"))


def test_a_tagged_but_empty_or_nil_block_is_not_untagged():                                 # C9
    assert select_block(instance(2023, []), CIK) == ("not_found", None)
    assert select_block(instance(2023, [("cur", "<div><p> &#160; </p></div>")]), CIK) == ("ambiguous", "empty")
    assert select_block(_with_nil(instance(2023, [])), CIK) == ("ambiguous", "empty")
    # An empty or nil repeat beside the real current text does not hide it.
    assert select_block(_with_nil(instance(2023, [("cur", SHORT), ("cur", "<p></p>")])), CIK) == \
        ("ok", text_of(SHORT))
    assert select_block(_with_nil(instance(2023, [("prior", LONG)])), CIK) == ("ambiguous", "empty")


def test_an_empty_tagged_block_is_shown_as_such_and_final(vcon, tmp_path):                   # C9
    from src.obsidian.sync import sync_vault
    docs = rp_docs(standard())
    docs[inst_url(A24)] = _with_nil(instance(2023, []))
    con, sec = connect(":memory:"), FakeSec(docs)
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {"ambiguous": 1, "ok": 1}
    r = related_party_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert (r["status"], r["reason"], r["compared"], r["text"]) == ("ambiguous", "empty", False, None)
    assert "tagged" in r["message"] and "no text" in r["message"] and "no related-party note tagged" not in r["message"]
    sec.calls.clear()
    assert fetch_related_party(con, sec, [CIK], date(2024, 6, 30)) == {} and sec.calls == [SUBS]
    put(vcon, "0001234-23-000010", date(2023, 3, 1), date(2022, 12, 31), "ok", text_of(SHORT))
    put(vcon, "0001234-24-000010", date(2024, 3, 1), date(2023, 12, 31), "ambiguous", "empty")
    sync_vault(vcon, tmp_path, AS_OF)
    body = (tmp_path / "Companies" / "ACME.md").read_text().split("## Related parties", 1)[1].split("\n## ", 1)[0]
    assert "no text" in body and "We lease our main office" not in body


def _utf16(raw: bytes) -> bytes:
    return raw.decode("utf-8").replace('encoding="utf-8"', 'encoding="utf-16"').encode("utf-16")


ENTITY = (b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE xbrli:xbrl [<!ENTITY lbl "Label">]>\n')


@pytest.mark.parametrize("encode", [lambda b: b, _utf16])
def test_entity_declarations_are_refused_in_any_encoding_the_parser_accepts(encode):          # C10
    body = instance(2023, [("cur", SHORT)]).split(b"?>", 1)[1].replace(b"Note 9.", b"&lbl; 9.", 1)
    with pytest.raises(ValueError, match="DTD"):
        select_block(encode(ENTITY + body.lstrip()), CIK)
    assert select_block(encode(instance(2023, [("cur", SHORT)])), CIK) == ("ok", text_of(SHORT))
