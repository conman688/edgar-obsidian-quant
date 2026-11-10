"""M11 Phase 2 parsers: filing index, XBRL instance, cover classes (2b), balance sheet (2c).

Synthetic documents in the shapes EDGAR serves: index.json, the one *_htm.xml instance,
FilingSummary.xml and an R page. No network.
"""
import json
from datetime import date

import pytest

from src.ingestion.quarantine_docs import (DocError, balance_sheet_report, cover_classes, instance_name,
                                           parse_instance, r_page, zero_debt)

NS = ('xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:dei="http://xbrl.sec.gov/dei/2024" '
      'xmlns:us-gaap="http://fasb.org/us-gaap/2024" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
      'xmlns:acme="http://acme.example/20240630"')


def ctx(cid, day, member=None, axis="us-gaap:StatementClassOfStockAxis"):
    seg = (f'<xbrli:segment><xbrldi:explicitMember dimension="{axis}">{member}</xbrldi:explicitMember>'
           f'</xbrli:segment>') if member else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">'
            f'0000001234</xbrli:identifier>{seg}</xbrli:entity><xbrli:period><xbrli:instant>{day}'
            f'</xbrli:instant></xbrli:period></xbrli:context>')


def instance(*parts) -> bytes:
    return f'<?xml version="1.0"?><xbrli:xbrl {NS}>{"".join(parts)}</xbrli:xbrl>'.encode()


def count(cid, v, decimals="INF"):
    return f'<dei:EntityCommonStockSharesOutstanding contextRef="{cid}" unitRef="shares" decimals="{decimals}">{v}</dei:EntityCommonStockSharesOutstanding>'


def symbol(cid, s):
    return f'<dei:TradingSymbol contextRef="{cid}">{s}</dei:TradingSymbol>'


def usd(tag, cid, v, decimals="-3", prefix="us-gaap"):
    d = f' decimals="{decimals}"' if decimals is not None else ""
    return f'<{prefix}:{tag} contextRef="{cid}" unitRef="usd"{d}>{v}</{prefix}:{tag}>'


# --- index ------------------------------------------------------------------------

def index(*names) -> bytes:
    return json.dumps({"directory": {"item": [{"name": n} for n in names]}}).encode()


def test_instance_name_is_the_one_htm_xml():
    assert instance_name(index("acme-20240630.htm", "acme-20240630_htm.xml", "R2.htm")) == "acme-20240630_htm.xml"


@pytest.mark.parametrize("names", [("acme.htm", "R2.htm"), ("a_htm.xml", "b_htm.xml")])
def test_zero_or_two_instances_is_an_error(names):
    with pytest.raises(DocError):
        instance_name(index(*names))


def test_malformed_index_is_an_error():
    with pytest.raises(DocError):
        instance_name(b"<html>not json")


# --- 2b cover classes ----------------------------------------------------------------

def test_cover_classes_per_member_with_symbols():
    doc = instance(ctx("e", "2024-07-25"), ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"),
                   ctx("b", "2024-07-25", "us-gaap:CommonClassBMember"),
                   count("a", 281792507), count("b", 70009005), symbol("a", "TSN"))
    got = cover_classes(parse_instance(doc))
    assert got["conflict"] is False
    assert got["classes"] == [
        {"member": "us-gaap:CommonClassAMember", "shares": 281792507.0, "symbol": "TSN", "dei_date": date(2024, 7, 25)},
        {"member": "us-gaap:CommonClassBMember", "shares": 70009005.0, "symbol": None, "dei_date": date(2024, 7, 25)}]
    assert got["symbols"] == ["TSN"]


def test_entity_level_symbol_is_counted_once():
    doc = instance(ctx("e", "2024-07-25"), ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"),
                   count("a", 1_000_000), symbol("e", "ACME"), symbol("a", "ACME"))
    got = cover_classes(parse_instance(doc))
    assert got["symbols"] == ["ACME"] and got["classes"][0]["symbol"] == "ACME"


def test_disagreeing_contexts_for_one_class_are_a_conflict():
    doc = instance(ctx("a1", "2024-07-25", "us-gaap:CommonClassAMember"),
                   ctx("a2", "2024-07-26", "us-gaap:CommonClassAMember"),
                   count("a1", 1_000_000), count("a2", 1_100_000))
    assert cover_classes(parse_instance(doc))["conflict"] is True


def test_agreeing_duplicate_contexts_are_one_class():
    doc = instance(ctx("a1", "2024-07-25", "us-gaap:CommonClassAMember"),
                   ctx("a2", "2024-07-25", "us-gaap:CommonClassAMember"),
                   count("a1", 1_000_000), count("a2", 1_000_000))
    got = cover_classes(parse_instance(doc))
    assert got["conflict"] is False and len(got["classes"]) == 1


def test_counts_on_other_axes_are_ignored():
    doc = instance(ctx("s", "2024-07-25", "acme:SegmentMember", axis="us-gaap:StatementBusinessSegmentsAxis"),
                   count("s", 5))
    assert cover_classes(parse_instance(doc))["classes"] == []


def test_malformed_instance_is_an_error():
    with pytest.raises(DocError):
        parse_instance(b"<xbrli:xbrl><unclosed>")


# --- 2c balance sheet ----------------------------------------------------------------

def filing_summary(*reports) -> bytes:
    body = "".join(f"<Report><ShortName>{s}</ShortName><HtmlFileName>{h}</HtmlFileName>"
                   f"<MenuCategory>{m}</MenuCategory></Report>" for s, h, m in reports)
    return f"<FilingSummary><MyReports>{body}</MyReports></FilingSummary>".encode()


def test_balance_sheet_report_is_the_one_statement():
    fs = filing_summary(("Cover", "R1.htm", "Cover"), ("Condensed Consolidated Balance Sheets", "R2.htm", "Statements"),
                        ("Condensed Consolidated Balance Sheets (Parenthetical)", "R3.htm", "Statements"),
                        ("Condensed Consolidated Statements of Operations", "R4.htm", "Statements"))
    assert balance_sheet_report(fs) == "R2.htm"


@pytest.mark.parametrize("reports", [
    [("Statements of Operations", "R4.htm", "Statements")],
    [("Consolidated Balance Sheets", "R2.htm", "Statements"), ("Balance Sheets - Parent Company", "R9.htm", "Statements")]])
def test_no_or_two_balance_sheets_is_an_error(reports):
    with pytest.raises(DocError):
        balance_sheet_report(filing_summary(*reports))


def rpage(dates, rows) -> bytes:
    head = "".join(f'<th class="th"><div>{d}</div></th>' for d in dates)
    body = "".join(f'<tr><td class="pl"><a onclick="showAr( this, \'defref_{e}\', window );">{e}</a></td>'
                   + "<td>1</td>" * len(dates) + "</tr>" for e in rows)
    return (f'<html><body><table class="report"><tr><th class="tl"><div>Balance Sheets - USD ($) $ in Thousands</div></th>'
            f'{head}</tr>{body}</table></body></html>').encode()


def test_r_page_gives_column_dates_and_row_elements_in_order():
    dates, rows = r_page(rpage(["Jun. 30, 2024", "Dec. 31, 2023"],
                               ["us-gaap_Assets", "us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]))
    assert dates == [date(2024, 6, 30), date(2023, 12, 31)]
    assert rows == ["us-gaap:Assets", "us-gaap:AccountsPayableCurrent", "us-gaap:Liabilities"]


BS = date(2024, 6, 30)
ASSETS = ["us-gaap_CashAndCashEquivalentsAtCarryingValue", "us-gaap_Assets"]


def bs_case(rows, facts, dates=("Jun. 30, 2024",), contexts=None):
    page = rpage(list(dates), ASSETS + rows + ["us-gaap_StockholdersEquity", "us-gaap_LiabilitiesAndStockholdersEquity"])
    inst = parse_instance(instance(*(contexts or [ctx("c", "2024-06-30")]), *facts))
    return zero_debt(*r_page(page), inst, BS)


SIX = ["AccountsPayableCurrent", "AccruedLiabilitiesCurrent", "ContractWithCustomerLiabilityCurrent",
       "OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent", "DeferredIncomeTaxLiabilitiesNet"]


def six_rows(values=(1000, 2000, 3000, 400, 500, 600), total=7500, decimals="-3"):
    rows = [f"us-gaap_{t}" for t in SIX] + ["us-gaap_Liabilities"]
    facts = [usd(t, "c", v * 1000, decimals) for t, v in zip(SIX, values)] + [usd("Liabilities", "c", total * 1000, decimals)]
    return rows, facts


def test_c3_positive_six_rows_in_thousands_reconcile_within_seven_halves():
    rows, facts = six_rows(values=(1000, 2000, 3000, 400, 500, 600), total=7500)
    facts[-1] = usd("Liabilities", "c", 7_503_000)              # 3k residual < 7 x 500
    got = bs_case(rows, facts)
    assert got["debt_zero"] is True and got["column"] == BS and got["method"] == "balance_sheet_no_debt"


def test_c3_residual_beyond_budget_keeps_quarantine():
    # $100M of Liabilities against $99.1M of listed rows: a $900k residual.
    rows, facts = six_rows(values=(99_100, 0, 0, 0, 0, 0), total=100_000)
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and "residual" in got["reason"]


def test_other_liabilities_line_keeps_quarantine():
    rows, facts = six_rows()
    rows.insert(0, "us-gaap_OtherLiabilitiesCurrent")
    facts.append(usd("OtherLiabilitiesCurrent", "c", 0))
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and "OtherLiabilitiesCurrent" in got["reason"]


def test_debt_like_row_is_a_candidate_and_keeps_quarantine():
    rows, facts = six_rows(total=8500)
    rows.insert(0, "us-gaap_ConvertibleNotesPayableCurrent")
    facts.append(usd("ConvertibleNotesPayableCurrent", "c", 1_000_000))
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and got["candidates"] == ["us-gaap:ConvertibleNotesPayableCurrent"]


def test_extension_element_keeps_quarantine():
    rows, facts = six_rows(total=8500)
    rows.insert(0, "acme_NotesPayableToBank")
    facts.append(usd("NotesPayableToBank", "c", 1_000_000, prefix="acme"))
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and "acme:NotesPayableToBank" in got["reason"]


def test_fact_without_decimals_keeps_quarantine():
    rows, facts = six_rows()
    facts[0] = usd(SIX[0], "c", 1_000_000, decimals=None)
    assert bs_case(rows, facts)["debt_zero"] is False


def test_subtotal_with_children_counts_once_and_must_reconcile():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_AccruedLiabilitiesCurrent", "us-gaap_LiabilitiesCurrent",
            "us-gaap_OperatingLeaseLiabilityNoncurrent", "us-gaap_Liabilities"]
    ok = [usd("AccountsPayableCurrent", "c", 1_000_000), usd("AccruedLiabilitiesCurrent", "c", 2_000_000),
          usd("LiabilitiesCurrent", "c", 3_000_000), usd("OperatingLeaseLiabilityNoncurrent", "c", 500_000),
          usd("Liabilities", "c", 3_500_000)]
    assert bs_case(rows, ok)["debt_zero"] is True                     # 3.0M counted once, not 6.0M
    bad = list(ok)
    bad[2] = usd("LiabilitiesCurrent", "c", 3_900_000)                # children miss the subtotal
    bad[4] = usd("Liabilities", "c", 4_400_000)
    assert bs_case(rows, bad)["debt_zero"] is False


def test_no_liabilities_total_keeps_quarantine():
    rows, facts = six_rows()
    rows.remove("us-gaap_Liabilities")
    facts = facts[:-1]
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and "Liabilities" in got["reason"]


def test_column_dated_before_bs_date_keeps_quarantine():
    rows, facts = six_rows()
    got = bs_case(rows, facts, dates=("Mar. 31, 2024",), contexts=[ctx("c", "2024-03-31")])
    assert got["debt_zero"] is False and "column" in got["reason"]


def test_fact_only_in_a_segment_or_vie_context_is_not_used():
    rows, facts = six_rows()
    seg = ctx("v", "2024-06-30", "acme:VariableInterestEntityMember", axis="srt:ConsolidatedEntitiesAxis")
    facts[0] = usd(SIX[0], "v", 1_000_000)                            # only the VIE has payables
    got = bs_case(rows, facts, contexts=[ctx("c", "2024-06-30"), seg])
    assert got["debt_zero"] is False


def test_repeated_element_keeps_quarantine():
    rows, facts = six_rows()
    rows.insert(1, f"us-gaap_{SIX[0]}")
    assert bs_case(rows, facts)["debt_zero"] is False
