"""M12-B7: Schedule 13D/13G stakes of 5% or more, informational only.

Live facts (SEC, 2026-11-27, project User-Agent; ROADMAP item 7):
- form.idx: the form type is the first 17 characters ("SCHEDULE 13D/A   ", "SC 13D           ").
  Each filing is listed twice, under the subject company and under the filer.
- XML (SCHEDULE 13D, 13D/A, 13G, 13G/A; main form from 2025Q1, schema X0202): issuer CIK and
  CUSIPs, event date, amendmentNo, one block per reporting person with aggregate shares and
  percent of class. 13D uses issuerCIK / aggregateAmountOwned / percentOfClass, 13G issuerCik /
  reportingPersonBeneficiallyOwnedAggregateNumberOfShares / classPercent. Some files prefix the
  namespace (sch:) and some 13D files use issuerCUSIP. Group members repeat the same shares.
- Legacy SC 13D / SC 13D/A: HTML or text cover pages ("CUSIP No.", "(Date of Event which
  Requires Filing of this Statement)", rows 11 and 13). 2023 sample: event date 92-94%,
  percent 89-90%, CUSIP 79-84%.
- The submission header names SUBJECT COMPANY and FILED BY; the XML issuer CIK equals the
  subject CIK in 399 of 399 sampled filings.
"""
from datetime import date, datetime

import pytest

from src.db import queries
from src.db.schema import connect
from src.ingestion import sched13 as s13
from src.obsidian.sync import sync_vault
from src.obsidian.writer import split_note
from tests.test_failure_modes import CIK
from tests.test_m4_screen import AS_OF, add_buy, add_company, cheap_company, screen_rows

FILER, FILER2 = 1649553, 315066


def idx_line(form: str, name: str, cik: int, filed: date, accn: str) -> str:
    return f"{form:<17}{name:<62}{cik:<12}{filed.isoformat()}  edgar/data/{cik}/{accn}.txt"


def form_idx(lines: list[str]) -> str:
    head = ("Description:           Master Index of EDGAR Dissemination Feed by Form Type\n"
            "Last Data Received:    September 30, 2024\n\n\n\n"
            "Form Type   Company Name                                                  CIK         Date Filed  File Name\n"
            + "-" * 141)
    return head + "\n" + "\n".join(lines) + "\n"


def header(accn, form, filed: date, subject: int | None, filer: int | None, accepted="173216") -> str:
    h = (f"<SEC-DOCUMENT>{accn}.txt : {filed:%Y%m%d}\n<SEC-HEADER>{accn}.hdr.sgml : {filed:%Y%m%d}\n"
         f"<ACCEPTANCE-DATETIME>{filed:%Y%m%d}{accepted}\nACCESSION NUMBER:\t\t{accn}\n"
         f"CONFORMED SUBMISSION TYPE:\t{form}\nPUBLIC DOCUMENT COUNT:\t\t1\nFILED AS OF DATE:\t\t{filed:%Y%m%d}\n\n")
    if subject is not None:
        h += (f"SUBJECT COMPANY:\t\n\n\tCOMPANY DATA:\t\n\t\tCOMPANY CONFORMED NAME:\t\t\tACME Corp\n"
              f"\t\tCENTRAL INDEX KEY:\t\t\t{subject:010d}\n\n\tFILING VALUES:\n\t\tFORM TYPE:\t\t{form}\n\n")
    if filer is not None:
        h += (f"FILED BY:\t\t\n\n\tCOMPANY DATA:\t\n\t\tCOMPANY CONFORMED NAME:\t\t\tFUND {filer} LP\n"
              f"\t\tCENTRAL INDEX KEY:\t\t\t{filer:010d}\n\n\tFILING VALUES:\n\t\tFORM TYPE:\t\t{form}\n")
    return h + "</SEC-HEADER>\n"


def xml13g(issuer=CIK, cusip="38942Q202", event="06/30/2024", persons=((1_556_081, 5.1), (1_556_081, 5.1)),
           rule="Rule 13d-1(b)", amend_no=None, prefix="", cusip_tag="issuerCusipNumber", form="SCHEDULE 13G"):
    p = f"{prefix}:" if prefix else ""
    xmlns = f'xmlns{":" + prefix if prefix else ""}="http://www.sec.gov/edgar/schedule13g"'
    people = "".join(
        f"<{p}coverPageHeaderReportingPersonDetails><{p}reportingPersonName>PERSON {i}</{p}reportingPersonName>"
        f"<{p}reportingPersonBeneficiallyOwnedAggregateNumberOfShares>{sh}</{p}reportingPersonBeneficiallyOwnedAggregateNumberOfShares>"
        f"<{p}classPercent>{pc}</{p}classPercent></{p}coverPageHeaderReportingPersonDetails>"
        for i, (sh, pc) in enumerate(persons))
    amend = f"<{p}amendmentNo>{amend_no}</{p}amendmentNo>" if amend_no else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><{p}edgarSubmission {xmlns} xmlns:com="http://www.sec.gov/edgar/common">'
            f"<{p}schemaVersion>X0202</{p}schemaVersion><{p}headerData><{p}submissionType>{form}</{p}submissionType>"
            f"</{p}headerData><{p}formData><{p}coverPageHeader>{amend}"
            f"<{p}securitiesClassTitle>Common Stock</{p}securitiesClassTitle>"
            f"<{p}eventDateRequiresFilingThisStatement>{event}</{p}eventDateRequiresFilingThisStatement>"
            f"<{p}issuerInfo><{p}issuerCik>{issuer:010d}</{p}issuerCik><{p}issuerName>ACME Corp</{p}issuerName>"
            f"<{p}issuerCusips><{p}{cusip_tag}>{cusip}</{p}{cusip_tag}></{p}issuerCusips></{p}issuerInfo>"
            f"<{p}designateRulesPursuantThisScheduleFiled><{p}designateRulePursuantThisScheduleFiled>{rule}"
            f"</{p}designateRulePursuantThisScheduleFiled></{p}designateRulesPursuantThisScheduleFiled>"
            f"</{p}coverPageHeader>{people}</{p}formData></{p}edgarSubmission>")


def xml13d(issuer=CIK, cusip="653942102", event="07/20/2024", persons=((1_515_293.0, 51.56),), amend_no=None,
           cusip_tag="issuerCusipNumber", form="SCHEDULE 13D"):
    people = "".join(
        f"<reportingPersons><reportingPersonInfo><reportingPersonName>PERSON {i}</reportingPersonName>"
        f"<aggregateAmountOwned>{sh}</aggregateAmountOwned><percentOfClass>{pc}</percentOfClass>"
        f"</reportingPersonInfo></reportingPersons>" for i, (sh, pc) in enumerate(persons))
    amend = f"<amendmentNo>{amend_no}</amendmentNo>" if amend_no else ""
    return ('<?xml version="1.0" encoding="UTF-8"?><edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D" '
            'xmlns:com="http://www.sec.gov/edgar/common"><schemaVersion>X0202</schemaVersion><headerData>'
            f"<submissionType>{form}</submissionType></headerData><formData><coverPageHeader>{amend}"
            f"<securitiesClassTitle>Common Stock</securitiesClassTitle><dateOfEvent>{event}</dateOfEvent>"
            f"<issuerInfo><issuerCIK>{issuer:010d}</issuerCIK><issuerCUSIPs><{cusip_tag}>{cusip}</{cusip_tag}>"
            f"</issuerCUSIPs><issuerName>ACME Corp</issuerName></issuerInfo></coverPageHeader>{people}"
            "</formData></edgarSubmission>")


def submission_xml(accn, form, filed, body, subject=CIK, filer=FILER, accepted="173216") -> bytes:
    return (header(accn, form, filed, subject, filer, accepted)
            + f"<DOCUMENT>\n<TYPE>{form}\n<SEQUENCE>1\n<FILENAME>primary_doc.xml\n<TEXT>\n<XML>\n{body}\n</XML>\n"
              "</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n").encode()


def cover(name, shares, pct) -> str:
    return (f"<tr><td>1</td><td>NAMES OF REPORTING PERSONS<br>{name}</td></tr>"
            f"<tr><td>11</td><td>AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON</td></tr>"
            f"<tr><td>{shares}</td></tr>"
            f"<tr><td>13</td><td>PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11)</td></tr>"
            f"<tr><td>{pct}</td></tr>")


def legacy13d(accn, filed, covers=(("FUND A", "59,516,175", "8.5 % (1)"), ("FUND B", "59,516,175 (1)", "8.5%")),
              event="January&nbsp;3, 2023", cusip="00165C203", form="SC 13D", subject=CIK, filer=FILER) -> bytes:
    body = ("<html><body><p>SECURITIES AND EXCHANGE COMMISSION</p><p>SCHEDULE 13D</p>"
            "<p>Common Stock, par value $0.0001</p><p>(Title of Class of Securities)</p>"
            f"<p>{cusip}</p><p>(CUSIP Number)</p><p>{event}</p>"
            "<p>(Date of Event which Requires Filing of this Statement)</p>"
            f"<p>CUSIP No. {cusip}</p><table>{''.join(cover(*c) for c in covers)}</table></body></html>")
    return (header(accn, form, filed, subject, filer)
            + f"<DOCUMENT>\n<TYPE>{form}\n<SEQUENCE>1\n<FILENAME>sc13d.htm\n<TEXT>\n{body}\n</TEXT>\n</DOCUMENT>\n"
              "</SEC-DOCUMENT>\n").encode()


class FakeGet:
    def __init__(self, docs: dict[str, bytes], fail: set[str] = frozenset()):
        self.docs, self.fail, self.urls = docs, set(fail), []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        accn = url.rsplit("/", 1)[1].removesuffix(".txt")
        if accn in self.fail:
            raise ConnectionError("503")
        return self.docs[accn]


A1, A2, A3 = "0001649553-24-000074", "0001649553-24-000090", "0000315066-24-001988"
F1, F2, F3 = date(2024, 7, 20), date(2024, 8, 6), date(2024, 8, 20)


def quarter_of(d: date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def load(con, docs: dict[str, tuple[str, date, bytes]], subjects=None, fail=frozenset(), quarters=("2024q3",)):
    """docs: accn -> (form, filed, raw). Each is indexed under the subject and the filer, in the
    quarter of its filing date; `quarters` are indexed too (as empty if nothing was filed)."""
    lines: dict[str, list[str]] = {q: [] for q in quarters}
    for accn, (form, filed, _) in docs.items():
        lines.setdefault(quarter_of(filed), []).extend(
            [idx_line(form, "ACME Corp", (subjects or {}).get(accn, CIK), filed, accn),
             idx_line(form, "FUND LP", FILER, filed, accn)])
    for q, ls in lines.items():
        s13.load_index(con, q, form_idx(ls))
    get = FakeGet({a: raw for a, (_, _, raw) in docs.items()}, fail)
    return s13.fetch_filings(con, get), get


XML_13G_FROM = getattr(queries, "XML_13G_FROM", None)      # the real cutoff, read before any patch


@pytest.fixture(autouse=True)
def xml_13g_from_2024(monkeypatch):
    """The fixtures file XML 13Gs in 2024; the real 13G era starts 2024-12-18 (tested below)."""
    monkeypatch.setattr(queries, "XML_13G_FROM", date(2024, 1, 1), raising=False)


@pytest.fixture
def con():
    c = connect(":memory:")
    add_company(c, CIK, "ACME", cheap_company())             # 1,000,000 shares, close 12
    return c


def stake(con, accn):
    cols = ("form", "amendment", "source", "issuer_cik", "filer_cik", "cusip", "event_date", "filed", "rule",
            "persons", "shares", "pct")
    r = con.execute(f"SELECT {', '.join(cols)} FROM sched13_stakes WHERE accn = ?", [accn]).fetchall()
    return dict(zip(cols, r[0])) if r else None


# --- index ------------------------------------------------------------------------------

def test_the_index_keeps_13d_and_13g_forms_by_their_17_character_field():
    text = form_idx([
        idx_line("SCHEDULE 13D/A", "17 Education & Technology Group Inc.", 1821468, date(2026, 9, 8), "0001-26-1"),
        idx_line("SCHEDULE 13G", "CalciMedica, Inc.", 1534133, date(2026, 7, 20), "0001-26-2"),
        idx_line("SC 13D", "180 DEGREE CAPITAL CORP. /NY/", 893739, date(2023, 3, 6), "0001-23-3"),
        idx_line("SC 13D/A", "22NW Fund, LP", 1640809, date(2023, 1, 10), "0001-23-4"),
        idx_line("SC 13G", "OLD PASSIVE FUND", 1, date(2023, 1, 10), "0001-23-5"),      # legacy 13G: not loaded
        idx_line("SC TO-T/A", "TENDER CO", 2, date(2023, 1, 10), "0001-23-6"),
        idx_line("4", "SCHEDULE 13D HOLDER", 3, date(2023, 1, 10), "0001-23-7")])
    rows = s13.parse_form_index(text)
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("0001-26-1", 1821468, "SCHEDULE 13D/A"), ("0001-26-2", 1534133, "SCHEDULE 13G"),
        ("0001-23-3", 893739, "SC 13D"), ("0001-23-4", 1640809, "SC 13D/A")]
    assert rows[0][3] == date(2026, 9, 8) and rows[0][4] == "edgar/data/1821468/0001-26-1.txt"


def test_only_filings_naming_a_priced_company_are_fetched_once_and_failures_retried(con):
    other = 999_999
    docs = {A1: ("SCHEDULE 13G", F1, submission_xml(A1, "SCHEDULE 13G", F1, xml13g())),
            A2: ("SCHEDULE 13G", F2, submission_xml(A2, "SCHEDULE 13G", F2, xml13g(issuer=other), subject=other)),
            A3: ("SCHEDULE 13G/A", F3, submission_xml(A3, "SCHEDULE 13G/A", F3, xml13g(amend_no=1)))}
    # A2 names neither ACME nor a priced filer: never fetched. A3 fails once.
    lines = [idx_line("SCHEDULE 13G", "ACME Corp", CIK, F1, A1), idx_line("SCHEDULE 13G", "OTHER", other, F2, A2),
             idx_line("SCHEDULE 13G", "FUND", 7, F2, A2), idx_line("SCHEDULE 13G/A", "ACME Corp", CIK, F3, A3)]
    s13.load_index(con, "2024q3", form_idx(lines))
    get = FakeGet({a: raw for a, (_, _, raw) in docs.items()}, fail={A3})
    s = s13.fetch_filings(con, get)
    assert (s.pending, s.done, s.failed) == (2, 1, 1) and [u.rsplit("/", 1)[1] for u in get.urls] == [
        f"{A1}.txt", f"{A3}.txt"]
    assert get.urls[0] == f"https://www.sec.gov/Archives/edgar/data/{CIK}/{A1}.txt"
    get.fail.clear()
    s = s13.fetch_filings(con, get)
    assert (s.pending, s.done, s.failed) == (1, 1, 0)
    s = s13.fetch_filings(con, get)
    assert s.pending == 0 and con.execute("SELECT count(*) FROM sched13_stakes").fetchall()[0][0] == 2


def test_reloading_a_quarter_index_replaces_it_and_keeps_loaded_filings(con):
    docs = {A1: ("SCHEDULE 13G", F1, submission_xml(A1, "SCHEDULE 13G", F1, xml13g()))}
    load(con, docs)
    load(con, docs)
    assert con.execute("SELECT count(*) FROM sched13_index").fetchall()[0][0] == 2     # subject and filer
    assert con.execute("SELECT count(*) FROM sched13_stakes").fetchall()[0][0] == 1


# --- parsing --------------------------------------------------------------------------

def test_an_xml_13g_gives_issuer_cusip_event_rule_and_the_largest_person_not_the_sum(con):
    """Live CalciMedica (0001649553-26-000074): CVI Investments and Heights Capital each report the
    same 1,556,081 shares (5.1%). The filing's stake is 1,556,081, never 3.1M."""
    load(con, {A1: ("SCHEDULE 13G", F1, submission_xml(A1, "SCHEDULE 13G", F1, xml13g()))})
    assert stake(con, A1) == {"form": "13G", "amendment": False, "source": "xml", "issuer_cik": CIK,
                              "filer_cik": FILER, "cusip": "38942Q202", "event_date": date(2024, 6, 30),
                              "filed": F1, "rule": "13d-1(b)", "persons": 2, "shares": 1_556_081, "pct": 5.1}


def test_an_xml_13d_amendment_and_the_namespace_and_tag_variants_parse(con):
    """Live: 0001213900-26-101030 (13D/A no. 8, 0.7%); some 13G files prefix the namespace (sch:)
    and some 13D files use issuerCUSIP."""
    a13d = submission_xml(A1, "SCHEDULE 13D/A", F1,
                          xml13d(persons=((13_143.0, 0.7), (12_636.0, 0.7)), amend_no=8, cusip_tag="issuerCUSIP",
                                 form="SCHEDULE 13D/A"))
    a13g = submission_xml(A2, "SCHEDULE 13G", F2, xml13g(prefix="sch", rule="Rule 13d-1(c)"))
    load(con, {A1: ("SCHEDULE 13D/A", F1, a13d), A2: ("SCHEDULE 13G", F2, a13g)})
    d = stake(con, A1)
    assert (d["form"], d["amendment"], d["cusip"], d["event_date"], d["shares"], d["pct"], d["rule"]) == (
        "13D", True, "653942102", date(2024, 7, 20), 13_143, 0.7, None)
    g = stake(con, A2)
    assert (g["cusip"], g["rule"], g["pct"]) == ("38942Q202", "13d-1(c)", 5.1)


def test_a_legacy_13d_cover_page_gives_event_cusip_shares_and_percent(con):
    """Live 0001140361-23-000289: two cover pages, 59,516,175 shares and 8.5 % with footnote marks."""
    raw = legacy13d(A1, date(2023, 1, 4))
    load(con, {A1: ("SC 13D", date(2023, 1, 4), raw)})
    assert stake(con, A1) == {"form": "13D", "amendment": False, "source": "text", "issuer_cik": CIK,
                              "filer_cik": FILER, "cusip": "00165C203", "event_date": date(2023, 1, 3),
                              "filed": date(2023, 1, 4), "rule": None, "persons": 2, "shares": 59_516_175,
                              "pct": 8.5}


@pytest.mark.parametrize("event, expected", [
    ("January&nbsp;3, 2023", date(2023, 1, 3)), ("Jan. 3, 2023", date(2023, 1, 3)),
    ("01/03/2023", date(2023, 1, 3)), ("December 30 , 2022", date(2022, 12, 30)), ("not a date", None)])
def test_legacy_event_dates(con, event, expected):
    load(con, {A1: ("SC 13D", date(2023, 1, 4), legacy13d(A1, date(2023, 1, 4), event=event))})
    assert stake(con, A1)["event_date"] == expected


def test_a_legacy_13d_without_a_readable_percent_is_kept_with_nulls(con):
    raw = legacy13d(A1, date(2023, 1, 4), covers=(("FUND A", "see Item 5", "see Item 5"),), cusip="none given")
    load(con, {A1: ("SC 13D/A", date(2023, 1, 4), raw)})
    d = stake(con, A1)
    assert (d["amendment"], d["pct"], d["shares"], d["cusip"]) == (True, None, None, None)


def test_the_issuer_comes_from_the_xml_and_the_filer_from_the_header(con):
    """A legacy filing has no issuer field: its subject company is the issuer. A filing listed
    under one company only (live: 7 legacy SC 13D/A rows in 2025Q1) has no FILED BY."""
    raw = legacy13d(A1, date(2023, 1, 4), filer=None)
    load(con, {A1: ("SC 13D", date(2023, 1, 4), raw)})
    d = stake(con, A1)
    assert (d["issuer_cik"], d["filer_cik"]) == (CIK, None)


def test_a_submission_without_a_schedule_document_is_skipped(con):
    raw = header(A1, "SCHEDULE 13G", F1, CIK, FILER).encode() + b"<DOCUMENT>\n<TYPE>EX-99\n<TEXT>\nnothing\n</TEXT>\n</DOCUMENT>\n"
    s, _ = load(con, {A1: ("SCHEDULE 13G", F1, raw)})
    assert (s.done, s.skipped) == (0, 1) and stake(con, A1) is None
    assert s13.fetch_filings(con, FakeGet({})).pending == 0


# --- screen -----------------------------------------------------------------------------

def stakes_docs():
    """FILER holds 8% on 13D (later amended to 9%); FILER2 holds 6% on 13G then sells below 5%."""
    return {
        A1: ("SCHEDULE 13D", date(2024, 7, 1), submission_xml(A1, "SCHEDULE 13D", date(2024, 7, 1),
                                                              xml13d(persons=((80_000.0, 8.0),)))),
        A2: ("SCHEDULE 13D/A", date(2024, 8, 1), submission_xml(A2, "SCHEDULE 13D/A", date(2024, 8, 1),
                                                                xml13d(persons=((90_000.0, 9.0),), amend_no=1,
                                                                       form="SCHEDULE 13D/A"))),
        A3: ("SCHEDULE 13G", date(2024, 5, 1), submission_xml(A3, "SCHEDULE 13G", date(2024, 5, 1),
                                                              xml13g(persons=((60_000, 6.0),)), filer=FILER2)),
        "0000315066-24-002000": ("SCHEDULE 13G/A", date(2024, 9, 4), submission_xml(
            "0000315066-24-002000", "SCHEDULE 13G/A", date(2024, 9, 4),
            xml13g(persons=((40_000, 4.0),), amend_no=1, form="SCHEDULE 13G/A"), filer=FILER2))}


def test_active_stakes_are_each_filers_latest_filing_at_5pct_or_more(con):
    load(con, stakes_docs())
    r = screen_rows(con)["ACME"]                                   # AS_OF 2024-09-03
    assert (r["holders_13d"], r["max_pct_13d"], r["last_filed_13d"], r["top_filer_13d"]) == (
        1, 9.0, date(2024, 8, 1), "FUND 1649553 LP")
    assert (r["holders_13g"], r["max_pct_13g"]) == (1, 6.0)
    later = screen_rows(con, date(2024, 9, 4))["ACME"]           # FILER2's exit below 5% is known
    assert (later["holders_13g"], later["max_pct_13g"]) == (0, None)


def test_a_filing_after_as_of_is_never_read(con):
    load(con, stakes_docs())
    r = screen_rows(con, date(2024, 6, 30))["ACME"]
    assert (r["holders_13d"], r["holders_13g"]) == (0, 1)
    assert screen_rows(con, date(2024, 4, 30))["ACME"]["holders_13g"] == 0


def test_a_new_13d_within_90_days_is_a_signal(con):
    load(con, stakes_docs())
    assert "new_13d" in screen_rows(con)["ACME"]["signals"]
    assert "new_13d" not in screen_rows(con, date(2024, 10, 1))["ACME"]["signals"]


def test_a_filer_moving_from_13g_to_13d_counts_once_under_13d(con):
    docs = {A3: ("SCHEDULE 13G", date(2024, 5, 1), submission_xml(A3, "SCHEDULE 13G", date(2024, 5, 1),
                                                                  xml13g(persons=((60_000, 6.0),)))),
            A1: ("SCHEDULE 13D", date(2024, 7, 1), submission_xml(A1, "SCHEDULE 13D", date(2024, 7, 1),
                                                                  xml13d(persons=((70_000.0, 7.0),))))}
    load(con, docs)
    r = screen_rows(con)["ACME"]
    assert (r["holders_13d"], r["holders_13g"]) == (1, 0)


def test_no_13dg_data_loaded_is_unknown_not_zero_holders(con):
    """Review C1: the live DB after migration 14 has no filings until `ingest 13dg` runs."""
    r = screen_rows(con)["ACME"]
    assert (r["holders_13d"], r["holders_13g"]) == (None, None)


def test_an_indexed_quarter_without_filings_for_the_company_has_zero_holders(con):
    load(con, {})
    r = screen_rows(con)["ACME"]                                 # AS_OF 2024-09-03, 2024q3 indexed
    assert (r["holders_13d"], r["holders_13g"], r["max_pct_13d"]) == (0, 0, None)


def test_a_date_outside_the_indexed_quarters_is_unknown(con):
    load(con, {}, quarters=("2024q2", "2024q3"))
    assert screen_rows(con, date(2024, 10, 1))["ACME"]["holders_13d"] is None       # 2024q4 not indexed
    assert screen_rows(con, date(2024, 3, 29))["ACME"]["holders_13d"] is None       # before 2024q2
    assert screen_rows(con, date(2024, 5, 1))["ACME"]["holders_13d"] == 0


def test_13g_before_the_xml_era_is_unknown(con, monkeypatch):
    """Legacy 13G is not loaded (user, 2026-11-27): passive holders are known only from 2024-12-18."""
    assert XML_13G_FROM == date(2024, 12, 18)
    monkeypatch.setattr(queries, "XML_13G_FROM", XML_13G_FROM)
    load(con, stakes_docs())
    r = screen_rows(con)["ACME"]
    assert (r["holders_13d"], r["holders_13g"], r["max_pct_13g"]) == (1, None, None)


def test_the_note_says_when_13dg_is_not_loaded(con, tmp_path):
    add_buy(con, CIK, "b1")
    sync_vault(con, tmp_path / "vault", AS_OF)
    note = (tmp_path / "vault" / "Companies" / "ACME.md").read_text()
    assert "No Schedule 13D or 13G filings are loaded for 2024-09-03." in note
    assert dict(split_note(note)[0])["holders_13d"] is None


def test_13dg_data_never_changes_status_rank_or_shortlist():
    def build(with_13dg):
        c = connect(":memory:")
        add_company(c, CIK, "ACME", cheap_company())
        add_buy(c, CIK, "b1")
        if with_13dg:
            load(c, stakes_docs())
        return {t: (r["status"], r["rank"], r["failed_filters"], r["issues"], r["flags"],
                    [x for x in r["signals"] if x != "new_13d"])
                for t, r in screen_rows(c).items()}
    assert build(True) == build(False)


# --- vault and CLI ----------------------------------------------------------------------

def test_company_note_dashboard_and_source_note_show_13dg(con, tmp_path):
    add_buy(con, CIK, "b1")
    load(con, stakes_docs())
    vault = tmp_path / "vault"
    sync_vault(con, vault, AS_OF)
    note = (vault / "Companies" / "ACME.md").read_text()
    meta = dict(split_note(note)[0])
    assert (meta["holders_13d"], meta["holders_13g"]) == (1, 1)
    assert ("Holders of 5% or more, by their latest filing on or before 2024-09-03: 1 on Schedule 13D "
            "(largest FUND 1649553 LP, 9.0%, filed 2024-08-01) and 1 on Schedule 13G (largest 6.0%).") in note
    assert "13D/13G filed 2024-08-01" in (vault / "Dashboard.md").read_text()
    source = (vault / "Sources" / "SEC Schedules 13D and 13G.md").read_text()
    assert "five business days" in source and "2024-12-18" in source
    assert sum(sync_vault(con, vault, AS_OF).written.values()) == 0


def test_ingest_13dg_is_a_manual_job_outside_the_schedule(con, monkeypatch, capsys):
    from types import SimpleNamespace
    from src import cli
    docs = {A1: submission_xml(A1, "SCHEDULE 13G", F1, xml13g())}
    idx = form_idx([idx_line("SCHEDULE 13G", "ACME Corp", CIK, F1, A1)])

    class Client:
        def get(self, url):
            return idx.encode("latin-1") if url.endswith("form.idx") else FakeGet(docs)(url)
    monkeypatch.setattr(cli, "_client", Client)
    cli.ingest_13dg(con, SimpleNamespace(since="2024q3", until="2024q3", limit=None))
    out = capsys.readouterr().out
    assert "13dg 2024q3: index, 1 lines" in out and "13dg: 1 filings loaded, 0 skipped, 0 failed" in out
    assert cli.INGEST_JOBS["13dg"] == [cli.ingest_13dg]
    assert cli.ingest_13dg not in cli.INGEST_JOBS["all"]
    assert all("13dg" not in step for steps in cli.RUN_STEPS.values() for step in steps)


def test_an_xml_with_a_dtd_is_refused_and_retried(con):
    """Entity declarations are never expanded (billion laughs, external entities): the filing
    fails with the reason and is retried, as related_party does for XBRL instances."""
    body = xml13g().replace('<?xml version="1.0" encoding="UTF-8"?>',
                            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa">]>')
    s, _ = load(con, {A1: ("SCHEDULE 13G", F1, submission_xml(A1, "SCHEDULE 13G", F1, body))})
    assert (s.done, s.failed) == (0, 1) and stake(con, A1) is None
    err = con.execute("SELECT status, error FROM sched13_filings WHERE accn = ?", [A1]).fetchall()[0]
    assert err[0] == "failed" and "DTD" in err[1]


def test_a_percent_above_100_is_unreadable(con):
    """Live: 7 of 76,339 filings parse above 100%, e.g. 0001420506-24-000071 (XML 13G, 862,190 in
    classPercent: the share count) and 0001029574-02-000027 (legacy 13D/A, 723 %)."""
    x = submission_xml(A1, "SCHEDULE 13G", F1, xml13g(persons=((862_190, 862_190),)))
    t = legacy13d(A2, date(2023, 1, 4), covers=(("FUND A", "20,800", "723 %"),))
    load(con, {A1: ("SCHEDULE 13G", F1, x), A2: ("SC 13D/A", date(2023, 1, 4), t)})
    assert stake(con, A1)["pct"] is None and stake(con, A2)["pct"] is None
    assert stake(con, A2)["shares"] == 20_800


def test_a_stake_last_filed_over_three_years_ago_is_not_counted(con):
    """Live JOB (2026-11-20): 20 "active" 13D holders, 17 of them last filed 2005-2017 and never
    filed an exit. A stake counts only while its latest filing is at most 3 years old."""
    old = "0000000001-21-000001"
    docs = {old: ("SC 13D", date(2021, 6, 1), legacy13d(old, date(2021, 6, 1), filer=FILER2)),
            A1: ("SCHEDULE 13D", date(2024, 7, 1), submission_xml(A1, "SCHEDULE 13D", date(2024, 7, 1),
                                                                  xml13d(persons=((80_000.0, 8.0),))))}
    load(con, docs)
    assert screen_rows(con)["ACME"]["holders_13d"] == 1                       # 2024-09-03: 3.3 years
    assert screen_rows(con, date(2024, 5, 30))["ACME"]["holders_13d"] == 1    # 1,094 days: still counted



def test_a_row_number_after_the_shares_heading_is_not_read_as_shares():
    """Review C2. Live: 106 legacy stakes have shares exactly 11, 11 have 12 and 34 have 1: an
    empty row 11 followed by "12. CHECK BOX", or "1" backtracked out of "12"."""
    doc = ("<TEXT>CUSIP No. 037833100 (1) NAMES OF REPORTING PERSONS FUND LP "
           "11 AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON 12. CHECK BOX IF THE AGGREGATE "
           "AMOUNT IN ROW (11) EXCLUDES CERTAIN SHARES [ ] 13 PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11) "
           "7.5% 14 TYPE OF REPORTING PERSON PN</TEXT>")
    r = s13._parse_text(doc)
    assert r["pct"] == 7.5
    assert r["shares"] is None
