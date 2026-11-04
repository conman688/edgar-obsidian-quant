"""Each test pins one audited failure mode against synthetic data."""
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.db.pit import concept, ttm_flow_sql
from src.db.queries import cluster_buys, insider_holdings, share_count_trend, valuation
from src.db.schema import connect
from src.ingestion.companyfacts import iter_fact_rows, load_docs
from src.ingestion.form4_insider import parse_form4, store
from src.llm.schemas import Amount, Evidence
from src.obsidian.writer import AUTO_END, AUTO_START, PersonRegistry, upsert_note

CIK = 1234


def fact(start, end, val, filed, accn, form="10-K", fy=None, fp="FY"):
    d = {"end": end, "val": val, "filed": filed, "accn": accn, "form": form, "fy": fy, "fp": fp}
    if start:
        d["start"] = start
    return d


def doc(tags: dict) -> dict:
    facts = {}
    for (tax, tag, unit), rows in tags.items():
        facts.setdefault(tax, {})[tag] = {"units": {unit: rows}}
    return {"cik": CIK, "facts": facts}


@pytest.fixture
def con():
    return connect(":memory:")


def test_period_buckets_inclusive_days():
    rows = list(iter_fact_rows(doc({("us-gaap", "Revenues", "USD"): [
        fact("2023-01-01", "2023-12-31", 1, "2024-02-01", "a"),
        fact("2023-01-01", "2023-03-31", 1, "2023-05-01", "b", "10-Q"),
        fact("2022-01-02", "2022-12-31", 1, "2023-02-01", "c"),  # 52-week year
    ]})))
    assert [r[7] for r in rows] == ["annual", "quarterly", "annual"]


def test_no_lookahead_and_restatement_versioning(con):
    load_docs(con, [doc({("us-gaap", "Revenues", "USD"): [
        fact("2023-01-01", "2023-12-31", 100.0, "2024-02-15", "orig"),
        fact("2023-01-01", "2023-12-31", 80.0, "2024-09-01", "amend", form="10-K/A"),
    ]})])
    get = lambda as_of, pol: concept(con, "revenue", as_of, pol).fetchall()
    assert get(date(2024, 1, 31), "as_last_known") == []              # not yet filed
    assert get(date(2024, 3, 1), "as_last_known")[0][7] == 100.0      # original known
    assert get(date(2024, 12, 1), "as_last_known")[0][7] == 80.0      # restatement known
    assert get(date(2024, 12, 1), "as_first_reported")[0][7] == 100.0  # original preserved
    assert con.sql("select count(*) from xbrl_facts").fetchall()[0][0] == 2
    load_docs(con, [doc({("us-gaap", "Revenues", "USD"): [
        fact("2023-01-01", "2023-12-31", 100.0, "2024-02-15", "orig")]})])
    assert con.sql("select count(*) from xbrl_facts").fetchall()[0][0] == 2  # idempotent reload


def test_tag_ladder_prefers_asc606(con):
    load_docs(con, [doc({
        ("us-gaap", "Revenues", "USD"): [fact("2023-01-01", "2023-12-31", 999.0, "2024-02-15", "a")],
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"):
            [fact("2023-01-01", "2023-12-31", 500.0, "2024-02-15", "a")],
    })])
    r = concept(con, "revenue", date(2025, 1, 1)).fetchall()
    assert len(r) == 1 and r[0][7] == 500.0


def test_ttm_ytd_bridge_june_fiscal_year(con):
    tag = ("us-gaap", "OperatingIncomeLoss", "USD")
    load_docs(con, [doc({tag: [
        fact("2023-07-01", "2024-06-30", 400.0, "2024-08-20", "k"),                    # FY24
        fact("2024-07-01", "2024-12-31", 260.0, "2025-02-05", "q2", "10-Q", fp="Q2"),  # H1 FY25
        fact("2023-07-01", "2023-12-31", 200.0, "2025-02-05", "q2", "10-Q", fp="Q2"),  # H1 FY24
    ]})])
    sql = ttm_flow_sql("as_last_known")
    v = con.sql(sql, params={"as_of": date(2025, 3, 1), "concept": "operating_income"}).fetchall()[0]
    assert v[1] == 460.0 and v[3] == "ttm_ytd_bridge"
    v = con.sql(sql, params={"as_of": date(2024, 9, 1), "concept": "operating_income"}).fetchall()[0]
    assert v[1] == 400.0 and v[3] == "fy"


def test_reverse_split_not_a_buyback(con):
    tag = ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares")
    rows = [fact(f"{y}-01-01", f"{y}-12-31", v, f"{y+1}-02-15", f"a{y}")
            for y, v in [(2020, 1_000_000), (2021, 1_000_000), (2022, 1_000_000), (2023, 95_000)]]
    load_docs(con, [doc({tag: rows})])
    con.execute("INSERT INTO splits VALUES (?, DATE '2023-06-01', 0.1, 'test')", [CIK])
    rel = share_count_trend(con, date(2024, 6, 1))
    cols = rel.columns
    row = dict(zip(cols, rel.fetchall()[0]))
    assert row["cagr_1y"] == pytest.approx(-0.05, abs=0.002)  # real 5% shrink, not -90%
    assert row["quarantine"] is False
    con.execute("DELETE FROM splits")
    row = dict(zip(cols, share_count_trend(con, date(2024, 6, 1)).fetchall()[0]))
    assert row["quarantine"] is True                           # missing split gets caught


def form4_xml(owner_cik, name, title, txns, doc_flag=None, footnote=""):
    flag = f"<aff10b5One>{doc_flag}</aff10b5One>" if doc_flag is not None else ""
    body = "".join(f"""
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{d}</value></transactionDate>
      <transactionCoding><transactionCode>{code}</transactionCode>{'<footnoteId id="F1"/>' if fn else ''}</transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{sh}</value></transactionShares>
        <transactionPricePerShare><value>{px}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>{di}</value></directOrIndirectOwnership>
        {f'<natureOfOwnership><value>{nat}</value></natureOfOwnership>' if nat else ''}</ownershipNature>
    </nonDerivativeTransaction>""" for d, code, sh, px, ad, after, di, nat, fn in txns)
    return f"""<?xml version="1.0"?><ownershipDocument>{flag}
      <issuer><issuerCik>{CIK}</issuerCik></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>{owner_cik}</rptOwnerCik><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>{title}</officerTitle></reportingOwnerRelationship></reportingOwner>
      <nonDerivativeTable>{body}</nonDerivativeTable>
      <footnotes><footnote id="F1">{footnote}</footnote></footnotes>
    </ownershipDocument>""".encode()


def test_form4_conviction_vs_grants_plans_and_indirect(con):
    ceo = form4_xml(1, "SMITH JOHN A", "Chief Executive Officer", [
        ("2024-05-01", "P", 10000, 12.0, "A", 50000, "D", "", False),
        ("2024-05-01", "P", 5000, 12.0, "A", 20000, "I", "By Family Trust", False),
        ("2024-05-02", "A", 99999, 0, "A", 149999, "D", "", False),           # grant: code A
    ], doc_flag=0)
    cfo_plan = form4_xml(2, "DOE JANE", "Chief Financial Officer", [
        ("2024-05-03", "P", 8000, 12.5, "A", 8000, "D", "", True),
    ], doc_flag=0, footnote="Purchased pursuant to a Rule 10b5-1 trading plan adopted 2024-01-10.")
    dir_buy = form4_xml(3, "ROE RICHARD", "", [
        ("2024-05-04", "P", 1000, 13.0, "A", 1000, "D", "", False),
    ], doc_flag=0)
    for i, x in enumerate([ceo, cfo_plan, dir_buy]):
        store(con, parse_form4(x, f"acc{i}", date(2024, 5, 6)))
    store(con, parse_form4(ceo, "acc0", date(2024, 5, 6)))  # replay is a no-op
    assert con.sql("select count(*) from form4_transactions").fetchall()[0][0] == 5

    r = cluster_buys(con, date(2024, 5, 20)).fetchall()
    n_insiders, usd = r[0][1], r[0][3]
    assert n_insiders == 2                       # CEO + director; CFO plan trade excluded
    assert usd == pytest.approx(15000 * 12 + 1000 * 13)
    assert cluster_buys(con, date(2024, 5, 5)).fetchall() == []  # not yet filed

    h = {row[1]: row for row in insider_holdings(con, date(2024, 6, 1)).fetchall()}
    assert h[1][3] == 149999 and h[1][4] == 20000   # direct last value + trust, no double count


def test_llm_amount_rejects_hallucination_and_wrong_column():
    src = "Total long-term debt (in thousands) | 2024 | 1,250 | 2023 | (980)"
    ctx = {"source": src, "fiscal_year": 2024}
    a = Amount.model_validate({"quote": src, "number_text": "(980)", "scale": "thousands",
                               "fiscal_year": 2023}, context=ctx)
    assert a.value == -980_000
    with pytest.raises(ValidationError):  # number not in quote
        Amount.model_validate({"quote": src, "number_text": "1,350", "scale": "thousands",
                               "fiscal_year": 2024}, context=ctx)
    with pytest.raises(ValidationError):  # read a future column
        Amount.model_validate({"quote": src, "number_text": "1,250", "scale": "thousands",
                               "fiscal_year": 2025}, context=ctx)
    with pytest.raises(ValidationError):  # quote invented
        Evidence.model_validate({"quote": "Debt covenants were waived in Q3"}, context=ctx)


def test_writer_preserves_user_notes_and_is_idempotent(tmp_path: Path):
    p = tmp_path / "Companies" / "ACME.md"
    assert upsert_note(p, {"ticker": "ACME", "ev_ebit": 5.1, "aliases": ["Acme Corp"]}, "auto v1")
    text = p.read_text().replace(AUTO_END, AUTO_END + "\n\nMy thesis: they hate it, I like it.")
    text = text.replace("ticker: ACME", "ticker: ACME\nconviction: high")
    p.write_text(text)
    assert upsert_note(p, {"ticker": "ACME", "ev_ebit": 4.2, "aliases": ["ACME Inc"]}, "auto v2")
    out = p.read_text()
    assert "My thesis" in out and "conviction: high" in out and "ev_ebit: 4.2" in out
    assert "Acme Corp" in out and "ACME Inc" in out and "auto v1" not in out
    assert out.count(AUTO_START) == 1
    assert not upsert_note(p, {"ticker": "ACME", "ev_ebit": 4.2, "aliases": ["ACME Inc"]}, "auto v2")
    assert not list(p.parent.glob(".tmp-*"))


def test_person_registry_collapses_name_variants(con):
    reg = PersonRegistry(con)
    c1 = reg.register(1, "SMITH JOHN A", issuer_cik=CIK)
    c2 = reg.register(1, "Smith John A.", issuer_cik=CIK)
    assert c1 == c2 == "John A Smith"
    assert reg.resolve("J. Smith", CIK) == "John A Smith"
    assert reg.resolve("John Smith", CIK) == "John A Smith"
    assert reg.resolve("J. Smith", 999) is None        # other issuer: do not guess
    assert reg.register(2, "SMITH JOHN A", issuer_cik=55) == "John A Smith (2)"  # namesake


def test_valuation_runs_and_flags_missing_debt(con):
    def inst(tax, tag, unit, v, end="2023-12-31"):
        return {(tax, tag, unit): [fact(None, end, v, "2024-02-15", "k")]}
    tags = {}
    for t in [inst("dei", "EntityCommonStockSharesOutstanding", "shares", 1_000_000, "2024-02-01"),
              inst("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD", 5_000_000),
              inst("us-gaap", "LongTermDebt", "USD", 2_000_000)]:
        tags.update(t)
    tags[("us-gaap", "OperatingIncomeLoss", "USD")] = [fact("2023-01-01", "2023-12-31", 3_000_000, "2024-02-15", "k")]
    load_docs(con, [doc(tags)])
    con.execute("INSERT INTO prices (cik, ticker, price_date, close_raw, source) VALUES (?, 'ACME', DATE '2024-03-01', 10.0, 'test')", [CIK])
    rel = valuation(con, date(2024, 3, 5))
    row = dict(zip(rel.columns, rel.fetchall()[0]))
    assert row["mcap"] == 10_000_000 and row["ev"] == 7_000_000
    assert row["ev_ebit"] == pytest.approx(7 / 3)
    assert row["net_cash_to_mcap"] == pytest.approx(0.3)
