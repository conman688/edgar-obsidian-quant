"""M7 text forensics: Risk Factors redline, 10-K text fetch, and forensics from loaded facts."""
import json
from datetime import date

import pytest

from src.db.schema import connect
from src.forensics import assess, forensics
from src.ingestion.filing_text import fetch_sections, html_text, risk_factors
from src.redline import normalize, redline, risk_factor_redline, sentences
from tests.test_failure_modes import CIK, fact
from tests.test_m4_screen import AS_OF, GAAP, add_company, cheap_company

RISKS_2023 = """Our revenue depends on a few customers. In fiscal 2023 our largest customer was 31% of sales and the loss of that customer would hurt our results.

We may not be able to refinance our $120 million term loan due March 15, 2025, on acceptable terms.

Our suppliers are concentrated in one region, and a disruption there could delay our shipments."""


def tenk_html(risks: str) -> bytes:
    paras = "".join(f"<p>{p}</p>" for p in risks.split("\n\n"))
    return f"""<html><body>
<div style="display:none"><ix:header><ix:hidden>Item 1A Risk Factors hidden dei text</ix:hidden></ix:header></div>
<p>TABLE OF CONTENTS</p>
<table><tr><td>Item 1A.</td><td>Risk Factors</td><td>12</td></tr>
<tr><td>Item 1B.</td><td>Unresolved Staff Comments</td><td>20</td></tr></table>
<p>PART I</p><p><b>Item 1A. Risk Factors</b></p>{paras}
<p><b>Item 1B. Unresolved Staff Comments</b></p><p>None.</p>
<script>var x = "Item 1A";</script></body></html>""".encode()


# --- roadmap checks ------------------------------------------------------------------

def test_a_10k_diffed_against_itself_has_no_new_sentences():
    r = redline(RISKS_2023, RISKS_2023)
    assert r.new == [] and r.removed == [] and r.total == len(sentences(RISKS_2023)) == 4


def test_a_changed_number_or_date_alone_does_not_make_a_sentence_new():
    later = (RISKS_2023.replace("fiscal 2023", "fiscal 2024").replace("31%", "28%")
             .replace("$120 million", "$95.5 million").replace("March 15, 2025", "June 30, 2026"))
    assert normalize("due March 15, 2025") == normalize("due June 30, 2026")
    assert redline(RISKS_2023, later).new == []


# --- redline -------------------------------------------------------------------------

def test_new_and_removed_sentences_are_found():
    later = RISKS_2023.replace(
        "Our suppliers are concentrated in one region, and a disruption there could delay our shipments.",
        "A cyberattack on our order systems could stop sales for weeks and expose customer data.")
    r = redline(RISKS_2023, later)
    assert r.new == ["A cyberattack on our order systems could stop sales for weeks and expose customer data."]
    assert r.removed == ["Our suppliers are concentrated in one region, and a disruption there could delay our shipments."]


def test_a_one_word_edit_is_not_new_but_a_rewrite_is():
    old = "We may not be able to refinance our term loan on acceptable terms when it comes due next year."
    assert redline(old, old.replace("acceptable", "favorable")).new == []
    rewrite = "Lenders have told us they will not extend the term loan, and we expect to default next year."
    assert len(redline(old, rewrite).new) == 1


def test_sentences_keep_abbreviations_and_drop_fragments():
    s = sentences("Risks Related to the U.S. Economy\n\nSales in the U.S. fell 10%. Costs rose. 23\n"
                  "Table of Contents\nWe sell through Acme Inc. and others in many markets.")
    assert s == ["Risks Related to the U.S. Economy", "Sales in the U.S. fell 10%.",
                 "We sell through Acme Inc. and others in many markets."]


# --- extraction ----------------------------------------------------------------------

def test_html_text_skips_hidden_xbrl_header_and_scripts():
    text = html_text(tenk_html(RISKS_2023))
    assert "hidden dei text" not in text and "var x" not in text
    assert "Our revenue depends on a few customers." in text


def test_risk_factors_takes_the_section_body_not_the_table_of_contents():
    rf = risk_factors(html_text(tenk_html(RISKS_2023)))
    assert rf.startswith("Our revenue depends on a few customers.")
    assert rf.endswith("could delay our shipments.")
    assert "Unresolved" not in rf and "None." not in rf


def test_risk_factors_missing_returns_none():
    assert risk_factors("PART I\nItem 1. Business\nWe make things.\nItem 2. Properties\nOne plant.") is None


# --- fetch ---------------------------------------------------------------------------

class FakeSec:
    def __init__(self, docs: dict[str, bytes]):
        self.docs, self.calls = docs, []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        return self.docs[url]


def submissions(cik: int, filings: list[tuple[str, str, str, str, str]]) -> bytes:
    """filings: (form, accn, filed, report_date, primary document)."""
    cols = list(zip(*filings))
    return json.dumps({"cik": str(cik), "filings": {"recent": {
        "form": cols[0], "accessionNumber": cols[1], "filingDate": cols[2], "reportDate": cols[3],
        "primaryDocument": cols[4]}}}).encode()


SUBS = f"https://data.sec.gov/submissions/CIK{CIK:010d}.json"


def doc_url(accn: str, name: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accn.replace('-', '')}/{name}"


def fake_sec() -> FakeSec:
    filings = [("10-K", "0000001234-24-000010", "2024-03-01", "2023-12-31", "k2023.htm"),
               ("10-Q", "0000001234-23-000020", "2023-11-01", "2023-09-30", "q3.htm"),
               ("10-K", "0000001234-23-000010", "2023-03-01", "2022-12-31", "k2022.htm"),
               ("10-K", "0000001234-22-000010", "2022-03-01", "2021-12-31", "k2021.htm")]
    return FakeSec({SUBS: submissions(CIK, filings),
                    doc_url("0000001234-24-000010", "k2023.htm"): tenk_html(RISKS_2023),
                    doc_url("0000001234-23-000010", "k2022.htm"): tenk_html(RISKS_2023.replace("31%", "28%")),
                    doc_url("0000001234-22-000010", "k2021.htm"): b"<html><body><p>No risk section.</p></body></html>"})


def test_fetch_reads_only_10ks_filed_by_as_of_and_never_twice():
    con, sec = connect(":memory:"), fake_sec()
    got = fetch_sections(con, sec, [CIK], date(2023, 12, 31))
    assert got == {"ok": 1, "not_found": 1}
    assert doc_url("0000001234-24-000010", "k2023.htm") not in sec.calls           # filed after as_of
    rows = con.execute("SELECT accn, form, filed, report_date, status FROM filing_sections ORDER BY filed").fetchall()
    assert rows == [("0000001234-22-000010", "10-K", date(2022, 3, 1), date(2021, 12, 31), "not_found"),
                    ("0000001234-23-000010", "10-K", date(2023, 3, 1), date(2022, 12, 31), "ok")]
    sec.calls.clear()
    assert fetch_sections(con, sec, [CIK], date(2023, 12, 31)) == {}
    assert sec.calls == [SUBS]                                                       # index only, no documents


def test_redline_compares_the_latest_two_10ks_known_on_as_of():
    con = connect(":memory:")
    fetch_sections(con, fake_sec(), [CIK], date(2023, 12, 31))
    fetch_sections(con, fake_sec(), [CIK], date(2024, 6, 30))
    r = risk_factor_redline(con, [CIK], date(2024, 6, 30))[CIK]
    assert r["status"] == "ok" and r["new"] == [] and r["total"] == 4
    assert (r["accn"], r["prior_accn"]) == ("0000001234-24-000010", "0000001234-23-000010")
    early = risk_factor_redline(con, [CIK], date(2023, 6, 30))[CIK]                 # prior 10-K has no section
    assert early["status"] == "no_prior_section" and early["accn"] == "0000001234-23-000010"
    assert risk_factor_redline(con, [CIK], date(2022, 1, 1)) == {}


# --- forensics from facts ------------------------------------------------------------

def fy(v, start="2023-01-01", end="2023-12-31", filed="2024-02-15", accn="k"):
    return [fact(start, end, v, filed, accn)]


def due(v, end="2023-12-31", filed="2024-02-15", accn="k"):
    return [fact(None, end, v, filed, accn)]


@pytest.fixture
def fcon():
    c = connect(":memory:")
    tags = cheap_company()
    tags |= {
        (GAAP, "PaymentsForRepurchaseOfCommonStock", "USD"): fy(600_000),
        (GAAP, "PaymentsToAcquireBusinessesNetOfCashAcquired", "USD"): fy(2_000_000),
        (GAAP, "GoodwillImpairmentLoss", "USD"): fy(900_000, "2022-01-01", "2022-12-31", "2023-02-15", "k22"),
        (GAAP, "AssetImpairmentCharges", "USD"): fy(700_000),
        (GAAP, "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths", "USD"): due(4_000_000),
        (GAAP, "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo", "USD"): due(3_000_000),
        (GAAP, "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree", "USD"): due(9_000_000, "2022-12-31", "2023-02-15", "k22"),
    }
    tags[(GAAP, "NetCashProvidedByUsedInOperatingActivities", "USD")] += fy(2_000_000, "2022-01-01", "2022-12-31",
                                                                            "2023-02-15", "k22")
    add_company(c, CIK, "ACME", tags)
    return c


def test_forensics_reads_the_latest_fiscal_year_only(fcon):
    f = forensics(fcon, [CIK], AS_OF)[CIK]
    assert f["fy_end"] == date(2023, 12, 31)
    assert (f["buybacks"], f["acquisitions"], f["asset_impairment"]) == (600_000, 2_000_000, 700_000)
    assert f["goodwill_impairment"] is None                       # FY2022's impairment is not this year's
    assert f["debt_due"] == [4_000_000, 3_000_000, None, None, None, None]   # FY2022 schedule ignored
    assert "k" in f["accns"] and "k22" not in f["accns"]


def test_forensics_is_point_in_time(fcon):
    f = forensics(fcon, [CIK], date(2023, 6, 30))[CIK]           # FY2023 10-K not filed yet
    assert f["fy_end"] == date(2022, 12, 31) and f["goodwill_impairment"] == 900_000
    assert f["debt_due"][2] == 9_000_000 and f["buybacks"] is None


def test_forensics_flags(fcon):
    f = forensics(fcon, [CIK], AS_OF)[CIK]
    a = assess(f, {"mcap": 12_000_000, "cash_like": 6_000_000})
    assert a["buyback_yield"] == pytest.approx(0.05)
    assert a["flags"] == ["debt_wall", "acquisitive", "impairment"]     # 7M due in 2y > 6M cash; 2M >= 10%; 0.7M >= 5%
    assert assess(f, {"mcap": 100_000_000, "cash_like": 50_000_000})["flags"] == []


def test_stale_fiscal_year_is_not_shown(fcon):
    assert forensics(fcon, [CIK], date(2025, 9, 1))[CIK]["stale"] is True


def test_html_source_line_breaks_are_spaces_not_new_lines():
    assert html_text(b"<p>Our debt\n   is large.</p><p>Second.</p>") == "Our debt is large.\nSecond."


# --- vault ---------------------------------------------------------------------------

def test_company_note_shows_the_redline_and_forensics_and_resync_writes_nothing(fcon, tmp_path):
    from src.obsidian.sync import sync_vault
    from tests.test_m4_screen import add_buy
    from tests.test_m6_vault import fm
    add_buy(fcon, CIK, "0001234-24-000001")
    new = "A cyberattack on our order systems could stop sales for weeks and expose customer data."
    for accn, filed, text in [("0001234-23-000010", date(2023, 3, 1), RISKS_2023),
                              ("0001234-24-000010", date(2024, 3, 1), RISKS_2023 + "\n" + new)]:
        fcon.execute("INSERT INTO filing_sections VALUES (?, ?, '10-K', ?, NULL, 'risk_factors', 'ok', ?, 'u', now())",
                     [CIK, accn, filed, text])
    sync_vault(fcon, tmp_path, AS_OF)
    note = tmp_path / "Companies" / "ACME.md"
    text = note.read_text()
    assert f"- {new}" in text and "**1 new** of 5 sentences, 0 removed" in text
    assert "[[0001234-24-000010]]" in text and "[[0001234-23-000010]]" in text
    assert "Check by hand: debt_wall" in text and "| Buybacks | $600k" in text
    assert fm(note)["rf_new_sentences"] == 1 and fm(note)["forensic_flags"] == ["debt_wall", "acquisitive", "impairment"]
    tenk = fm(tmp_path / "Filings" / "0001234-24-000010.md")
    assert (tenk["form"], tenk["filed"]) == ("10-K", date(2024, 3, 1))
    assert not sum(sync_vault(fcon, tmp_path, AS_OF).written.values())


def test_a_sentence_broken_across_lines_is_one_sentence():
    whole = ("These payment delays have occurred in the past and can deprive us of working capital until the "
             "matter is resolved, which could hinder our ability to grow.")
    broken = ("These payment delays have occurred in the past and can deprive us of working capital until the\n"
              "matter is resolved,\nwhich could hinder our ability to grow.")
    assert sentences(broken) == [whole]
    assert redline(whole, broken).new == [] and redline(broken, whole).removed == []
    assert sentences("Risks Related to Our Debt\nWe have a lot of debt today.") == [
        "Risks Related to Our Debt", "We have a lot of debt today."]


def test_page_breaks_inside_a_sentence_are_dropped():
    broken = ("Delays can deprive us of working capital until the matter is resolved,\n18\nTable of Contents\n"
              "which could hinder our ability to grow.\n- 19 -\nPage 20\nThe school could repay funds.")
    assert sentences(broken) == ["Delays can deprive us of working capital until the matter is resolved, "
                                 "which could hinder our ability to grow.", "The school could repay funds."]


def test_an_empty_risk_section_says_so(fcon, tmp_path):
    from src.obsidian.sync import sync_vault
    for accn, filed in [("0001234-23-000010", date(2023, 3, 1)), ("0001234-24-000010", date(2024, 3, 1))]:
        fcon.execute("INSERT INTO filing_sections VALUES (?, ?, '10-K', ?, NULL, 'risk_factors', 'ok', "
                     "'Not applicable.', 'u', now())", [CIK, accn, filed])
    from tests.test_m4_screen import add_buy
    add_buy(fcon, CIK, "0001234-24-000001")
    sync_vault(fcon, tmp_path, AS_OF)
    assert "lists no risk factors" in (tmp_path / "Companies" / "ACME.md").read_text()


def test_a_new_sentence_repeated_in_the_summary_is_listed_once():
    new = "Our chief executive officer has taken a leave of absence from the company."
    r = redline(RISKS_2023, f"•{new}\n{RISKS_2023}\n{new}")
    assert r.new == [f"•{new}"] and r.total == 6
