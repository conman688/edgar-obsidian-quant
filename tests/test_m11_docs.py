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
      'xmlns:acme="http://acme.example/20240630" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"')
UNITS = ('<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
         '<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>')


def ctx(cid, day, member=None, axis="us-gaap:StatementClassOfStockAxis", cik="0000001234"):
    seg = (f'<xbrli:segment><xbrldi:explicitMember dimension="{axis}">{member}</xbrldi:explicitMember>'
           f'</xbrli:segment>') if member else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">'
            f'{cik}</xbrli:identifier>{seg}</xbrli:entity><xbrli:period><xbrli:instant>{day}'
            f'</xbrli:instant></xbrli:period></xbrli:context>')


def instance(*parts, units=UNITS) -> bytes:
    return f'<?xml version="1.0"?><xbrli:xbrl {NS}>{units}{"".join(parts)}</xbrli:xbrl>'.encode()


def count(cid, v, decimals="INF"):
    return f'<dei:EntityCommonStockSharesOutstanding contextRef="{cid}" unitRef="shares" decimals="{decimals}">{v}</dei:EntityCommonStockSharesOutstanding>'


def symbol(cid, s):
    return f'<dei:TradingSymbol contextRef="{cid}">{s}</dei:TradingSymbol>'


def usd(tag, cid, v, decimals="-3", prefix="us-gaap", unit="usd"):
    d = f' decimals="{decimals}"' if decimals is not None else ""
    u = f' unitRef="{unit}"' if unit is not None else ""
    return f'<{prefix}:{tag} contextRef="{cid}"{u}{d}>{v}</{prefix}:{tag}>'


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
    assert balance_sheet_report(fs) == {"html": "R2.htm", "name": "Condensed Consolidated Balance Sheets",
                                        "consolidated": True}


@pytest.mark.parametrize("reports", [
    [("Statements of Operations", "R4.htm", "Statements")],
    [("Consolidated Balance Sheets", "R2.htm", "Statements"), ("Consolidated Balance Sheets - Other", "R9.htm", "Statements")]])
def test_no_or_two_balance_sheets_is_an_error(reports):
    with pytest.raises(DocError):
        balance_sheet_report(filing_summary(*reports))


LABELS = {"us-gaap_AccruedLiabilitiesCurrent": "Accrued liabilities"}


def rpage(dates, rows, title="Consolidated Balance Sheets") -> bytes:
    """rows: element names (with a plain label) or (element, label) pairs."""
    head = "".join(f'<th class="th"><div>{d}</div></th>' for d in dates)
    pairs = [r if isinstance(r, tuple) else (r, LABELS.get(r, r.split("_", 1)[1])) for r in rows]
    body = "".join(f'<tr><td class="pl"><a onclick="showAr( this, \'defref_{e}\', window );">{lab}</a></td>'
                   + "<td>1</td>" * len(dates) + "</tr>" for e, lab in pairs)
    return (f'<html><body><table class="report"><tr><th class="tl"><div>{title} - USD ($) $ in Thousands</div></th>'
            f'{head}</tr>{body}</table></body></html>').encode()


def test_r_page_gives_column_dates_and_row_elements_in_order():
    dates, rows = r_page(rpage(["Jun. 30, 2024", "Dec. 31, 2023"],
                               ["us-gaap_Assets", "us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]))
    assert dates == [date(2024, 6, 30), date(2023, 12, 31)]
    assert rows == [("us-gaap:Assets", "Assets"), ("us-gaap:AccountsPayableCurrent", "AccountsPayableCurrent"),
                    ("us-gaap:Liabilities", "Liabilities")]


BS = date(2024, 6, 30)
ASSETS = ["us-gaap_CashAndCashEquivalentsAtCarryingValue", "us-gaap_Assets"]


def bs_case(rows, facts, dates=("Jun. 30, 2024",), contexts=None, cik=1234, units=UNITS):
    page = rpage(list(dates), ASSETS + rows + ["us-gaap_StockholdersEquity", "us-gaap_LiabilitiesAndStockholdersEquity"])
    inst = parse_instance(instance(*(contexts or [ctx("c", "2024-06-30")]), *facts, units=units))
    return zero_debt(*r_page(page), inst, BS, cik)


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


# --- review round 6 ----------------------------------------------------------------------

def test_c11_current_subtotal_without_children_keeps_quarantine():
    rows = ["us-gaap_LiabilitiesCurrent", "us-gaap_Liabilities"]
    facts = [usd("LiabilitiesCurrent", "c", 100, "INF"), usd("Liabilities", "c", 100, "INF")]
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and "LiabilitiesCurrent" in got["reason"]


def test_c11_noncurrent_subtotal_without_children_keeps_quarantine():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_LiabilitiesCurrent", "us-gaap_LiabilitiesNoncurrent",
            "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 60, "INF"), usd("LiabilitiesCurrent", "c", 60, "INF"),
             usd("LiabilitiesNoncurrent", "c", 40, "INF"), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is False


def test_c11_partial_children_of_a_subtotal_keep_quarantine():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_LiabilitiesCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 50, "INF"), usd("LiabilitiesCurrent", "c", 100, "INF"),
             usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is False


def test_c12_and_other_aggregate_on_an_allowed_tag_keeps_quarantine():
    rows = [("us-gaap_AccruedLiabilitiesCurrent", "Accrued expenses and other"), "us-gaap_Liabilities"]
    facts = [usd("AccruedLiabilitiesCurrent", "c", 100, "INF"), usd("Liabilities", "c", 100, "INF")]
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and "Accrued expenses and other" in got["reason"]


def test_c12_specific_accrual_label_and_headings_pass():
    rows = [("us-gaap_LiabilitiesCurrentAbstract", "Current liabilities:"),
            ("us-gaap_AccruedLiabilitiesCurrent", "Accrued liabilities"), "us-gaap_Liabilities"]
    facts = [usd("AccruedLiabilitiesCurrent", "c", 100, "INF"), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is True


def test_c12_real_cava_page_keeps_the_aggregate_label():
    page = open("tests/fixtures/m11/cava-0001628280-26-070344-R2.htm", "rb").read()
    dates, rows = r_page(page)
    assert date(2026, 10, 4) in dates
    assert ("us-gaap:AccruedLiabilitiesCurrent", "Accrued expenses and other") in rows
    assert ("us-gaap:LiabilitiesCurrentAbstract", "Current liabilities:") in rows


def test_c13_lone_parent_company_balance_sheet_is_never_proof():
    got = balance_sheet_report(filing_summary(("Parent Company Balance Sheets", "parent.htm", "Statements")))
    assert got["consolidated"] is False


@pytest.mark.parametrize("name, ok", [("Consolidated Balance Sheets", True), ("Condensed Consolidated Balance Sheets", True),
                                      ("Combined Balance Sheets", False),              # C13: combined scope not proved
                                      ("Unconsolidated Balance Sheets", False), ("Nonconsolidated Balance Sheets", False),
                                      ("Non-consolidated Balance Sheets", False),
                                      ("Balance Sheets", False), ("Condensed Balance Sheets - Registrant", False),
                                      ("Consolidated Balance Sheets - Guarantor Subsidiaries", False)])
def test_c13_scope_must_be_positively_consolidated(name, ok):
    assert balance_sheet_report(filing_summary((name, "R2.htm", "Statements")))["consolidated"] is ok


def test_c13_another_entitys_undimensioned_context_is_not_used():
    rows, facts = six_rows()
    got = bs_case(rows, facts, contexts=[ctx("c", "2024-06-30", cik="0000009999")])
    assert got["debt_zero"] is False


# --- review round 7 ----------------------------------------------------------------------

def nil_count(cid):
    return (f'<dei:EntityCommonStockSharesOutstanding contextRef="{cid}" unitRef="shares" decimals="INF" '
            f'xsi:nil="true" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>')


def test_c14_nil_class_count_is_unusable_never_zero():
    doc = instance(ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"), ctx("b", "2024-07-25", "us-gaap:CommonClassBMember"),
                   nil_count("a"), count("b", 1_000_000), symbol("b", "ACME"))
    got = cover_classes(parse_instance(doc))
    assert got["conflict"] is True
    assert all(c["shares"] != 0 for c in got["classes"]) and "us-gaap:CommonClassAMember" not in [c["member"] for c in got["classes"]]


def test_c14_non_numeric_class_count_is_a_doc_error():
    doc = instance(ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"), count("a", "lots"))
    with pytest.raises(DocError):
        cover_classes(parse_instance(doc))


def test_c14_bad_context_date_is_a_doc_error():
    with pytest.raises(DocError):
        parse_instance(instance(ctx("a", "2024-13-45")))


def test_c14_bad_decimals_or_nil_value_keeps_quarantine():
    rows, facts = six_rows()
    facts[0] = usd(SIX[0], "c", 1_000_000, decimals="three")
    assert bs_case(rows, facts)["debt_zero"] is False
    rows, facts = six_rows()
    facts[0] = (f'<us-gaap:{SIX[0]} contextRef="c" unitRef="usd" decimals="-3" xsi:nil="true" '
                f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>')
    assert bs_case(rows, facts)["debt_zero"] is False


def test_c15_rollup_counted_with_its_own_children_keeps_quarantine():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_AccruedLiabilitiesCurrent",
            "us-gaap_AccountsPayableAndAccruedLiabilitiesCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 50, "INF"), usd("AccruedLiabilitiesCurrent", "c", 50, "INF"),
             usd("AccountsPayableAndAccruedLiabilitiesCurrent", "c", 100, "INF"), usd("Liabilities", "c", 200, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is False


def test_c15_lease_total_with_children_keeps_quarantine_when_double_counted():
    rows = ["us-gaap_OperatingLeaseLiabilityCurrent", "us-gaap_OperatingLeaseLiabilityNoncurrent",
            "us-gaap_OperatingLeaseLiability", "us-gaap_Liabilities"]
    facts = [usd("OperatingLeaseLiabilityCurrent", "c", 20, "INF"), usd("OperatingLeaseLiabilityNoncurrent", "c", 80, "INF"),
             usd("OperatingLeaseLiability", "c", 100, "INF"), usd("Liabilities", "c", 200, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is False


def test_c15_rollup_replaces_reconciled_children_once():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_AccruedLiabilitiesCurrent",
            "us-gaap_AccountsPayableAndAccruedLiabilitiesCurrent", "us-gaap_OperatingLeaseLiabilityNoncurrent",
            "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 50, "INF"), usd("AccruedLiabilitiesCurrent", "c", 50, "INF"),
             usd("AccountsPayableAndAccruedLiabilitiesCurrent", "c", 100, "INF"),
             usd("OperatingLeaseLiabilityNoncurrent", "c", 30, "INF"), usd("Liabilities", "c", 130, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is True


def test_c15_rollup_whose_children_do_not_reconcile_keeps_quarantine():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_AccountsPayableAndAccruedLiabilitiesCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 50, "INF"),
             usd("AccountsPayableAndAccruedLiabilitiesCurrent", "c", 100, "INF"), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is False


def test_c15_two_tags_for_one_concept_keep_quarantine():
    rows = ["us-gaap_DeferredRevenueCurrent", "us-gaap_ContractWithCustomerLiabilityCurrent", "us-gaap_Liabilities"]
    facts = [usd("DeferredRevenueCurrent", "c", 50, "INF"), usd("ContractWithCustomerLiabilityCurrent", "c", 50, "INF"),
             usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is False


# --- review round 8 ----------------------------------------------------------------------

@pytest.mark.parametrize("broad, part", [
    ("AccruedLiabilitiesCurrent", "EmployeeRelatedLiabilitiesCurrent"),      # Codex's case
    ("AccruedLiabilitiesCurrent", "AccruedSalariesCurrent"),
    ("AccruedLiabilitiesCurrent", "TaxesPayableCurrent"),
    ("AccruedLiabilitiesCurrent", "AccruedIncomeTaxesCurrent"),
    ("AccruedLiabilitiesCurrent", "DividendsPayableCurrent"),
    ("AccruedLiabilitiesNoncurrent", "AccruedIncomeTaxesNoncurrent"),
    ("AccountsPayableAndAccruedLiabilitiesCurrent", "TaxesPayableCurrent")])
def test_c15_broad_accrual_beside_a_possible_component_keeps_quarantine(broad, part):
    rows = [f"us-gaap_{part}", f"us-gaap_{broad}", "us-gaap_Liabilities"]
    facts = [usd(part, "c", 100, "INF"), usd(broad, "c", 100, "INF"), usd("Liabilities", "c", 200, "INF")]
    got = bs_case(rows, facts)
    assert got["debt_zero"] is False and "ambiguous" in got["reason"]


def eur_units():
    return UNITS + '<xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>'


def test_c16_mixed_currencies_keep_quarantine():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 100, "INF", unit="eur"), usd("Liabilities", "c", 100, "INF")]
    got = bs_case(rows, facts, units=eur_units())
    assert got["debt_zero"] is False and "unit" in got["reason"]


def test_c16_all_eur_is_not_a_usd_proof():
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 100, "INF", unit="eur"), usd("Liabilities", "c", 100, "INF", unit="eur")]
    assert bs_case(rows, facts, units=eur_units())["debt_zero"] is False


@pytest.mark.parametrize("unit", [None, "undefined", "shares"])
def test_c16_missing_unresolved_or_nonmonetary_unit_keeps_quarantine(unit):
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 100, "INF", unit=unit), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts)["debt_zero"] is False


def test_c16_divide_unit_keeps_quarantine():
    units = UNITS + ('<xbrli:unit id="usdps"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD'
                     '</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares'
                     '</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>')
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 100, "INF", unit="usdps"), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts, units=units)["debt_zero"] is False


def test_c16_distinct_unit_ids_that_are_both_usd_pass():
    units = UNITS + '<xbrli:unit id="U_USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]
    facts = [usd("AccountsPayableCurrent", "c", 100, "INF", unit="U_USD"), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, facts, units=units)["debt_zero"] is True


# --- review round 9 ----------------------------------------------------------------------

G = 'xmlns:g="http://fasb.org/us-gaap/2024"'


def ctx_raw(cid, day, axis, member, decl="", cik="0000001234"):
    return (f'<xbrli:context id="{cid}" {decl}><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}'
            f'</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="{axis}">{member}</xbrldi:explicitMember>'
            f'</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>{day}</xbrli:instant></xbrli:period></xbrli:context>')


def two_classes(b_ctx):
    return instance(ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"), b_ctx,
                    count("a", 1_000_000), count("b", 500_000), symbol("a", "ACME"))


BASE_CLASSES = [("us-gaap:CommonClassAMember", 1_000_000.0), ("us-gaap:CommonClassBMember", 500_000.0)]


def members(doc, cik=1234):
    got = cover_classes(parse_instance(doc), cik)
    return [(c["member"], c["shares"]) for c in got["classes"]], got


def test_c17_alias_prefix_declared_locally_keeps_the_class():
    doc = two_classes(ctx_raw("b", "2024-07-25", "g:StatementClassOfStockAxis", "g:CommonClassBMember", decl=G))
    got, full = members(doc)
    assert got == BASE_CLASSES and full["conflict"] is False


def test_c17_alias_prefix_declared_on_the_root_keeps_the_class():
    doc = two_classes(ctx_raw("b", "2024-07-25", "g:StatementClassOfStockAxis", "g:CommonClassBMember"))
    doc = doc.replace(b"<xbrli:xbrl ", b"<xbrli:xbrl " + G.encode() + b" ", 1)
    assert members(doc)[0] == BASE_CLASSES


def test_c17_alias_for_both_classes_is_one_class_set():
    a = ctx_raw("a", "2024-07-25", "g:StatementClassOfStockAxis", "g:CommonClassAMember", decl=G)
    b = ctx_raw("b", "2024-07-25", "g:StatementClassOfStockAxis", "g:CommonClassBMember", decl=G)
    doc = instance(a, b, count("a", 1_000_000), count("b", 500_000), symbol("a", "ACME"))
    assert members(doc)[0] == BASE_CLASSES


def test_c17_aliased_duplicate_context_still_detects_a_conflict():
    a2 = ctx_raw("a2", "2024-07-25", "g:StatementClassOfStockAxis", "g:CommonClassAMember", decl=G)
    doc = instance(ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"), a2, count("a", 1_000_000), count("a2", 900_000))
    assert members(doc)[1]["conflict"] is True


def test_c17_rebound_prefix_is_not_the_class_axis():
    # 'us-gaap' bound locally to another namespace: not the us-gaap share-class axis.
    b = ctx_raw("b", "2024-07-25", "us-gaap:StatementClassOfStockAxis", "us-gaap:CommonClassBMember",
                decl='xmlns:us-gaap="http://other.example/2024"')
    assert members(two_classes(b))[0] == BASE_CLASSES[:1]


def test_c17_business_segment_axis_stays_ignored_under_an_alias():
    b = ctx_raw("b", "2024-07-25", "g:StatementBusinessSegmentsAxis", "g:CommonClassBMember", decl=G)
    assert members(two_classes(b))[0] == BASE_CLASSES[:1]


def test_c17_iso_currency_alias_and_rebinding():
    units = UNITS + ('<xbrli:unit id="u2" xmlns:cur="http://www.xbrl.org/2003/iso4217"><xbrli:measure>cur:USD'
                     '</xbrli:measure></xbrli:unit>'
                     '<xbrli:unit id="fake" xmlns:iso4217="http://not-iso.example/"><xbrli:measure>iso4217:USD'
                     '</xbrli:measure></xbrli:unit>')
    rows = ["us-gaap_AccountsPayableCurrent", "us-gaap_Liabilities"]
    ok = [usd("AccountsPayableCurrent", "c", 100, "INF", unit="u2"), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, ok, units=units)["debt_zero"] is True
    bad = [usd("AccountsPayableCurrent", "c", 100, "INF", unit="fake"), usd("Liabilities", "c", 100, "INF")]
    assert bs_case(rows, bad, units=units)["debt_zero"] is False


def test_c18_other_entity_cover_facts_are_not_the_filers():
    doc = instance(ctx("a", "2024-07-25", "us-gaap:CommonClassAMember", cik="0000009999"),
                   count("a", 1_000_000), symbol("a", "ACME"))
    got, full = members(doc)
    assert got == [] and full["symbols"] == []


def test_c18_mixed_entities_do_not_contaminate_or_conflict():
    doc = instance(ctx("a", "2024-07-25", "us-gaap:CommonClassAMember"),
                   ctx("x", "2024-07-25", "us-gaap:CommonClassAMember", cik="0000009999"),
                   count("a", 1_000_000), count("x", 7_000_000), symbol("a", "ACME"), symbol("x", "OTHR"))
    got, full = members(doc)
    assert got == [("us-gaap:CommonClassAMember", 1_000_000.0)]
    assert full["conflict"] is False and full["symbols"] == ["ACME"]
