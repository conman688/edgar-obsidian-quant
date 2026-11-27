"""M12-A5: factor attribution of the backtest. Factor files explain past returns only; they never
feed the screen.

Live facts (2026-11-26, generic User-Agent, all 200):
- French: F-F_Research_Data_Factors_CSV.zip (Mkt-RF, SMB, HML, RF), the 5-factor 2x3 zip (adds
  RMW, CMA) and F-F_Momentum_Factor_CSV.zip (Mom), monthly to 2026-09, percent, CRLF, a header
  block, then monthly rows "YYYYMM, ...", then an "Annual Factors" block. Missing is -99.99 or -999.
- global-q q5_factors_monthly_2025.csv: year, month, R_F, R_MKT, R_ME, R_IA, R_ROE, R_EG, percent,
  1967-01 to 2025-12.
- AQR QMJ and BAB xlsx: first sheet, a "DATE" header row (row 19) with a USA column, dates as
  shared strings MM/DD/YYYY, values in decimals.
"""
import hashlib
import io
import json
import random
import zipfile
from datetime import date

import pytest

from src import attribution as at
from src import backtest as bt
from src.db.schema import connect
from src.ingestion import factors as fx


# ---- synthetic source files -------------------------------------------------------

def french_csv(cols: list[str], rows: dict[tuple[int, int], list[float]], note: str = "Mom") -> str:
    head = ["This file was created using the 202609 CRSP database.", f"It contains {note}.", "",
            "Missing data are indicated by -99.99 or -999.", "", "," + ",".join(cols)]
    body = [f"{y}{m:02d}," + ",".join(f"{v:8.2f}" for v in vals) for (y, m), vals in sorted(rows.items())]
    annual = ["", " Annual Factors: January-December ", "," + ",".join(cols),
              "  2025," + ",".join("   99.00" for _ in cols), "",
              "Copyright 2026 Eugene F. Fama and Kenneth R. French", ""]
    return "\r\n".join(head + body + annual)


def zipped(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def q5_csv(rows: dict[tuple[int, int], list[float]]) -> bytes:
    lines = ["year,month,R_F,R_MKT,R_ME,R_IA,R_ROE,R_EG"]
    lines += [f"{y},{m}," + ",".join(f"{v:.4f}" for v in vals) for (y, m), vals in sorted(rows.items())]
    return ("\n".join(lines) + "\n").encode()


def aqr_xlsx(rows: dict[tuple[int, int], float], usa: bool = True, first_sheet: str = "QMJ Factors") -> bytes:
    """A minimal workbook shaped like AQR's: notes above, a DATE header, countries, USA in Y."""
    import calendar
    strings = ["Quality Minus Junk: Factors, Monthly", "DATE", "AUS", "USA" if usa else "CAN"]
    cells = [f'<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
             f'<row r="19"><c r="A19" t="s"><v>1</v></c><c r="B19" t="s"><v>2</v></c>'
             f'<c r="Y19" t="s"><v>3</v></c></row>']
    for i, ((y, m), v) in enumerate(sorted(rows.items())):
        strings.append(f"{m:02d}/{calendar.monthrange(y, m)[1]:02d}/{y}")
        r = 20 + i
        cells.append(f'<row r="{r}"><c r="A{r}" t="s"><v>{len(strings) - 1}</v></c>'
                     f'<c r="B{r}"><v>0.5</v></c><c r="Y{r}"><v>{v!r}</v></c></row>')
    cells.append(f'<row r="{20 + len(rows) + 2}"><c r="A{20 + len(rows) + 2}" s="3"/></row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                   f'<sheet name="{first_sheet}" sheetId="24" r:id="rId9"/>'
                   f'<sheet name="Definition" sheetId="35" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId2" Type="{rel}/worksheet" Target="worksheets/sheet2.xml"/>'
                   f'<Relationship Id="rId9" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
                   f'<Relationship Id="rId14" Type="{rel}/sharedStrings" Target="/xl/sharedStrings.xml"/>'
                   '</Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}">'
                   + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{main}"><sheetData>'
                   + "".join(cells) + "</sheetData></worksheet>")
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{main}"><sheetData/></worksheet>')
    return buf.getvalue()


def months(start: tuple[int, int], n: int) -> list[tuple[int, int]]:
    y, m = start
    out = []
    for _ in range(n):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_end(ym: tuple[int, int]) -> date:
    import calendar
    return date(ym[0], ym[1], calendar.monthrange(*ym)[1])


def prev(ym):
    y, m = ym
    return (y - 1, 12) if m == 1 else (y, m - 1)


class FakeWeb:
    """Stands in for FactorClient: url -> bytes, or an exception to raise."""
    def __init__(self, files: dict[str, bytes | Exception]):
        self.files, self.calls = dict(files), []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        v = self.files[url]
        if isinstance(v, Exception):
            raise v
        return v


def web_for(fac: dict[tuple[int, int], dict[str, float]], q_end: tuple[int, int] | None = None) -> FakeWeb:
    """All six source files from one table of decimal factor values. French and q5 files are
    written in percent, AQR in decimals, as the real files are."""
    p = lambda k: {ym: [100 * f[c] for c in k] for ym, f in fac.items()}
    q = {ym: f for ym, f in fac.items() if q_end is None or ym <= q_end}
    files = {
        "ff3": zipped("F-F_Research_Data_Factors.csv", french_csv(["Mkt-RF", "SMB", "HML", "RF"],
                                                                  p(["MKT", "SMB", "HML", "RF"]))),
        "ff5": zipped("F-F_Research_Data_5_Factors_2x3.csv",
                      french_csv(["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"],
                                 p(["MKT", "SMB5", "HML", "RMW", "CMA", "RF"]))),
        "mom": zipped("F-F_Momentum_Factor.csv", french_csv(["Mom"], p(["MOM"]))),
        "q5": q5_csv({ym: [100 * f[c] for c in ["QRF", "QMKT", "ME", "IA", "ROE", "EG"]] for ym, f in q.items()}),
        "qmj": aqr_xlsx({ym: f["QMJ"] for ym, f in fac.items()}),
        "bab": aqr_xlsx({ym: f["BAB"] for ym, f in fac.items()}, first_sheet="BAB Factors"),
    }
    return FakeWeb({fx.SOURCES[k].url: v for k, v in files.items()})


def factor_table(n: int = 60, start=(2015, 1), seed: int = 7) -> dict[tuple[int, int], dict[str, float]]:
    rnd = random.Random(seed)
    out = {}
    for ym in months(start, n):
        out[ym] = {"MKT": rnd.gauss(0.008, 0.04), "SMB": rnd.gauss(0, 0.02), "SMB5": rnd.gauss(0, 0.02),
                   "HML": rnd.gauss(0, 0.02), "RMW": rnd.gauss(0, 0.015), "CMA": rnd.gauss(0, 0.015),
                   "MOM": rnd.gauss(0.005, 0.03), "RF": round(rnd.uniform(0.0, 0.004), 4),
                   "QRF": round(rnd.uniform(0.0, 0.004), 4), "QMKT": rnd.gauss(0.008, 0.04),
                   "ME": rnd.gauss(0, 0.02), "IA": rnd.gauss(0, 0.02), "ROE": rnd.gauss(0, 0.02),
                   "EG": rnd.gauss(0, 0.02), "QMJ": rnd.gauss(0.003, 0.02), "BAB": rnd.gauss(0.005, 0.03)}
        # The files hold two decimals of a percent: round so the parsed value is exact.
        for k in ("MKT", "SMB", "SMB5", "HML", "RMW", "CMA", "MOM"):
            out[ym][k] = round(out[ym][k], 4)
        for k in ("QMKT", "ME", "IA", "ROE", "EG"):        # q5 holds four decimals of a percent
            out[ym][k] = round(out[ym][k], 6)
    return out


# ---- parsing ----------------------------------------------------------------------

def test_french_monthly_rows_are_decimals_and_the_annual_block_is_ignored():
    text = french_csv(["Mkt-RF", "SMB", "HML", "RF"],
                      {(2026, 8): [2.56, -0.52, -3.54, 0.29], (2026, 9): [-1.07, -99.99, -1.55, 0.31]})
    rows = fx.parse_french(zipped("F-F_Research_Data_Factors.csv", text), ["Mkt-RF", "SMB", "HML", "RF"])
    assert rows == {(2026, 8): {"Mkt-RF": pytest.approx(0.0256), "SMB": pytest.approx(-0.0052),
                                "HML": pytest.approx(-0.0354), "RF": pytest.approx(0.0029)},
                    (2026, 9): {"Mkt-RF": pytest.approx(-0.0107), "HML": pytest.approx(-0.0155),
                                "RF": pytest.approx(0.0031)}}          # -99.99 is missing, not -100%


def test_french_file_without_the_expected_columns_is_refused():
    text = french_csv(["Mkt-RF", "SMB"], {(2026, 9): [1.0, 2.0]})
    with pytest.raises(fx.FactorError, match="columns"):
        fx.parse_french(zipped("x.csv", text), ["Mkt-RF", "SMB", "HML", "RF"])


def test_q5_rows_are_decimals():
    rows = fx.parse_q5(q5_csv({(2025, 12): [0.3127, -0.3337, 0.0065, 0.7384, 0.0127, 0.5577]}))
    assert rows[(2025, 12)] == {"R_F": pytest.approx(0.003127), "R_MKT": pytest.approx(-0.003337),
                                "R_ME": pytest.approx(0.000065), "R_IA": pytest.approx(0.007384),
                                "R_ROE": pytest.approx(0.000127), "R_EG": pytest.approx(0.005577)}


def test_aqr_usa_column_is_read_from_the_first_sheet_in_decimals():
    rows = fx.parse_aqr(aqr_xlsx({(1957, 7): 0.0111539791294, (2026, 9): -0.02}))
    assert rows == {(1957, 7): {"USA": 0.0111539791294}, (2026, 9): {"USA": -0.02}}


def test_aqr_workbook_without_a_usa_column_is_refused():
    with pytest.raises(fx.FactorError, match="USA"):
        fx.parse_aqr(aqr_xlsx({(2026, 9): 0.01}, usa=False))


# ---- snapshots --------------------------------------------------------------------

def test_fetch_keeps_raw_files_with_a_manifest_and_load_returns_every_factor(tmp_path):
    fac = factor_table(3, start=(2026, 7))
    web = web_for(fac)
    snap = fx.fetch(web, tmp_path, date(2026, 11, 26))
    assert snap == tmp_path / "2026-11-26"
    manifest = json.loads((snap / "manifest.json").read_text())
    assert manifest["snapshot_date"] == "2026-11-26" and set(manifest["files"]) == set(fx.SOURCES)
    for name, f in manifest["files"].items():
        raw = (snap / f["file"]).read_bytes()
        assert f["sha256"] == hashlib.sha256(raw).hexdigest() and f["url"] == fx.SOURCES[name].url
        assert f["newest_month"] == "2026-09"
    s = fx.load(snap)
    assert s.snapshot_date == date(2026, 11, 26)
    assert s.months[(2026, 9)]["ff3.Mkt-RF"] == pytest.approx(fac[(2026, 9)]["MKT"])
    assert s.months[(2026, 9)]["bab.USA"] == pytest.approx(fac[(2026, 9)]["BAB"])
    assert s.months[(2026, 9)]["q5.R_F"] == pytest.approx(fac[(2026, 9)]["QRF"])
    assert fx.latest(tmp_path) == snap


def test_a_same_date_retry_never_mixes_file_versions(tmp_path):
    """Codex 11-26: pin one checked manifest per snapshot. A second fetch on the same date keeps the
    first snapshot whole and downloads nothing."""
    first = fx.fetch(web_for(factor_table(3, start=(2026, 7))), tmp_path, date(2026, 11, 26))
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    web2 = web_for(factor_table(3, start=(2026, 7), seed=99))
    again = fx.fetch(web2, tmp_path, date(2026, 11, 26))
    assert again == first and web2.calls == []
    assert {p.name: p.read_bytes() for p in again.iterdir()} == before


def test_a_failed_source_leaves_no_snapshot_and_the_retry_is_complete(tmp_path):
    web = web_for(factor_table(3, start=(2026, 7)))
    good = dict(web.files)
    web.files[fx.SOURCES["qmj"].url] = fx.FactorError("aqr.com: HTTP 503")
    with pytest.raises(fx.FactorError, match="503"):
        fx.fetch(web, tmp_path, date(2026, 11, 26))
    assert list(tmp_path.iterdir()) == [] and fx.latest(tmp_path) is None
    web.files[fx.SOURCES["qmj"].url] = b"not a workbook"           # a bad file fails the same way
    with pytest.raises(fx.FactorError, match="qmj"):
        fx.fetch(web, tmp_path, date(2026, 11, 26))
    assert list(tmp_path.iterdir()) == []
    snap = fx.fetch(FakeWeb(good), tmp_path, date(2026, 11, 26))
    assert set(json.loads((snap / "manifest.json").read_text())["files"]) == set(fx.SOURCES)


def test_a_changed_raw_file_is_refused_on_load(tmp_path):
    snap = fx.fetch(web_for(factor_table(3, start=(2026, 7))), tmp_path, date(2026, 11, 26))
    f = snap / json.loads((snap / "manifest.json").read_text())["files"]["q5"]["file"]
    f.write_bytes(f.read_bytes() + b"2026,10,0.3,1,1,1,1,1\n")
    with pytest.raises(fx.FactorError, match="sha256"):
        fx.load(snap)


@pytest.mark.parametrize("source", ["ff3", "q5", "qmj"])
def test_a_non_finite_value_fails_the_fetch(tmp_path, source):
    """Codex 11-26: a "nan" cell parsed by float() must not reach the regression."""
    web = web_for(factor_table(3, start=(2026, 7)))
    url = fx.SOURCES[source].url
    if source == "ff3":
        text = french_csv(["Mkt-RF", "SMB", "HML", "RF"], {(2026, 8): [1, 1, 1, 0.3], (2026, 9): [1, 1, 1, 0.3]})
        web.files[url] = zipped("F-F_Research_Data_Factors.csv", text.replace("    1.00", "     nan", 1))
    elif source == "q5":
        web.files[url] = b"year,month,R_F,R_MKT,R_ME,R_IA,R_ROE,R_EG\n2025,1,0.3,inf,1,1,1,1\n"
    else:
        web.files[url] = aqr_xlsx({(2026, 8): float("nan")})
    with pytest.raises(fx.FactorError, match=f"{source}.*not a finite number"):
        fx.fetch(web, tmp_path, date(2026, 11, 26))
    assert list(tmp_path.iterdir()) == []


def test_a_same_date_retry_checks_the_existing_snapshot(tmp_path):
    """Codex 11-26: a same-date fetch verifies the kept snapshot before reporting success."""
    snap = fx.fetch(web_for(factor_table(3, start=(2026, 7))), tmp_path, date(2026, 11, 26))
    files = json.loads((snap / "manifest.json").read_text())["files"]
    (snap / files["mom"]["file"]).write_bytes(b"changed")
    web = web_for(factor_table(3, start=(2026, 7)))
    with pytest.raises(fx.FactorError, match="sha256"):
        fx.fetch(web, tmp_path, date(2026, 11, 26))
    (snap / files["mom"]["file"]).unlink()
    with pytest.raises(fx.FactorError, match="missing"):
        fx.fetch(web, tmp_path, date(2026, 11, 26))
    assert web.calls == []


# ---- the backtest's monthly series ---------------------------------------------------

def panel(con, rows):
    con.execute("CREATE OR REPLACE TABLE ranked (as_of DATE, cik INTEGER, status VARCHAR, "
                "ret_1m DOUBLE, ret_3m DOUBLE, ret_6m DOUBLE, ret_12m DOUBLE, exit_gap_3m BOOLEAN)")
    con.executemany("INSERT INTO ranked VALUES (?, ?, ?, ?, 0, NULL, NULL, FALSE)", rows)


def test_evaluate_returns_the_after_cost_monthly_series_it_compounds():
    con = connect(":memory:")
    d1, d2, d3 = date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)
    panel(con, [(d1, 1, "shortlist", 0.10), (d1, 2, "fail", 0.00),
                (d2, 1, "fail", 0.02), (d2, 2, "fail", 0.04),            # cash month: sells stock 1
                (d3, 1, "shortlist", None), (d3, 2, "fail", 0.01)])      # unmeasured: not in the series
    s = bt.evaluate(con, "ranked", cost_bps=50)["series"]
    assert s == [{"as_of": d1, "strategy": pytest.approx(0.10 - 0.005), "universe": pytest.approx(0.05), "picks": 1},
                 {"as_of": d2, "strategy": pytest.approx(-0.005), "universe": pytest.approx(0.03), "picks": 0}]


def test_the_series_stops_at_the_first_unmeasured_month():
    """Codex 11-26: after an unmeasured month the held weights are stale, so a later month's cost
    would be a guess. The series ends there, as the CAGR is withheld."""
    con = connect(":memory:")
    d1, d2, d3 = date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)
    panel(con, [(d1, 1, "shortlist", 0.10), (d1, 2, "fail", 0.00),
                (d2, 1, "shortlist", None), (d2, 2, "fail", 0.01),       # unmeasured
                (d3, 2, "shortlist", 0.03), (d3, 1, "fail", 0.02)])      # measured again
    r = bt.evaluate(con, "ranked", cost_bps=50)
    assert [s["as_of"] for s in r["series"]] == [d1]
    assert r["monthly"]["months_unmeasured"] == 1


# ---- attribution ---------------------------------------------------------------------

def test_ols_matches_numpy():
    np = pytest.importorskip("numpy")
    rnd = random.Random(3)
    X = [[rnd.gauss(0, 1), rnd.gauss(0, 1)] for _ in range(50)]
    y = [0.2 + 1.5 * a - 0.7 * b + rnd.gauss(0, 0.5) for a, b in X]
    r = at.ols(y, X)
    A = np.column_stack([np.ones(50), np.array(X)])
    beta, *_ = np.linalg.lstsq(A, np.array(y), rcond=None)
    resid = np.array(y) - A @ beta
    se = np.sqrt(np.diag(np.linalg.inv(A.T @ A)) * (resid @ resid) / (50 - 3))
    assert r["coef"] == pytest.approx(list(beta))
    assert r["t"] == pytest.approx(list(beta / se))
    assert r["r2"] == pytest.approx(1 - (resid @ resid) / ((np.array(y) - np.mean(y)) ** 2).sum())
    assert r["n"] == 50


def series_from(fac, rule, start=(2015, 1), n=48, picks=lambda i: 3):
    """One measured strategy month per as_of month end. The month realized after as_of M is
    M+1, so the return is built from M+1's factors."""
    out = []
    for i, ym in enumerate(months(start, n)):
        f = fac[ym]
        strat, univ = rule(f)
        out.append({"as_of": month_end(prev(ym)), "strategy": strat if picks(i) else -0.001,
                    "universe": univ, "picks": picks(i)})
    return out


def test_realized_month_alignment_units_and_rf_counted_once(tmp_path):
    """Codex 11-26: an April 30 entry is explained by May's factors; percent files become
    decimals; RF is subtracted once for strategy-minus-RF and never for strategy-minus-universe."""
    fac = factor_table(60, start=(2014, 12))
    snap = fx.load(fx.fetch(web_for(fac), tmp_path, date(2026, 11, 26)))
    alpha = 0.004
    ser = series_from(fac, lambda f: (f["RF"] + alpha + 1.2 * f["MKT"], f["RF"] + 1.0 * f["MKT"]))
    assert ser[4]["as_of"] == date(2015, 4, 30)               # explained by 2015-05 below
    r = at.attribute(ser, snap)
    capm = r["invested"]["CAPM"]
    assert capm["n"] == 48 and capm["alpha"] == pytest.approx(alpha, abs=1e-9)
    assert capm["loadings"]["MKT"][0] == pytest.approx(1.2)
    active = r["active"]["CAPM"]                               # RF cancels; not subtracted again
    assert active["alpha"] == pytest.approx(alpha, abs=1e-9)
    assert active["loadings"]["MKT"][0] == pytest.approx(0.2)
    assert r["invested"]["CAPM"]["alpha_annual"] == pytest.approx(12 * alpha)
    # A one-month shift (as_of month's own factors) must not fit.
    shifted = [dict(s, as_of=month_end(nxt)) for s, nxt in zip(ser, months((2015, 1), 48))]
    assert at.attribute(shifted, snap)["invested"]["CAPM"]["r2"] < 0.5


def test_each_model_uses_its_own_factors_and_risk_free_rate(tmp_path):
    fac = factor_table(60, start=(2014, 12))
    snap = fx.load(fx.fetch(web_for(fac), tmp_path, date(2026, 11, 26)))
    ser = series_from(fac, lambda f: (f["QRF"] + 0.002 + 0.9 * f["QMKT"] + 0.5 * f["IA"] - 0.3 * f["ROE"], 0.0))
    q = at.attribute(ser, snap)["invested"]["q5"]
    assert q["alpha"] == pytest.approx(0.002, abs=1e-9)
    assert q["loadings"]["MKT"][0] == pytest.approx(0.9) and q["loadings"]["IA"][0] == pytest.approx(0.5)
    assert q["loadings"]["ROE"][0] == pytest.approx(-0.3) and q["loadings"]["ME"][0] == pytest.approx(0, abs=1e-9)
    ser = series_from(fac, lambda f: (f["RF"] + 0.001 + f["MKT"] + 0.4 * f["QMJ"] + 0.2 * f["BAB"] - 0.1 * f["MOM"], 0.0))
    full = at.attribute(ser, snap)["invested"]["FF5+Mom+QMJ+BAB"]
    assert full["alpha"] == pytest.approx(0.001, abs=1e-9)
    assert {k: v[0] for k, v in full["loadings"].items()} == pytest.approx(
        {"MKT": 1.0, "SMB": 0, "HML": 0, "RMW": 0, "CMA": 0, "MOM": -0.1, "QMJ": 0.4, "BAB": 0.2}, abs=1e-9)
    assert set(at.attribute(ser, snap)["invested"]) == {"CAPM", "FF3", "FF5+Mom", "q5", "FF5+Mom+QMJ+BAB"}


def test_cash_months_are_left_out_of_the_invested_regression_only(tmp_path):
    fac = factor_table(60, start=(2014, 12))
    snap = fx.load(fx.fetch(web_for(fac), tmp_path, date(2026, 11, 26)))
    ser = series_from(fac, lambda f: (f["RF"] + f["MKT"], f["RF"] + f["MKT"]), picks=lambda i: 0 if i % 4 == 0 else 2)
    r = at.attribute(ser, snap)
    assert r["invested"]["CAPM"]["n"] == 36 and r["counts"]["cash_months"] == 12
    assert r["active"]["CAPM"]["n"] == 48


def test_the_invested_regression_keeps_the_cost_of_each_exit_to_cash(tmp_path):
    """Review C1 (M12-A5): every 4th month is cash; its -0.001 is the cost of selling the previous
    month's picks. The invested sample keeps each entry cost, so it must keep each exit cost too."""
    fac = factor_table(60, start=(2014, 12))
    snap = fx.load(fx.fetch(web_for(fac), tmp_path, date(2026, 11, 26)))
    ser = series_from(fac, lambda f: (f["RF"] + f["MKT"], f["RF"] + f["MKT"]),
                      picks=lambda i: 0 if i % 4 == 0 else 2)
    exits = sum(1 for a, b in zip(ser, ser[1:]) if a["picks"] and not b["picks"])     # 11
    capm = at.attribute(ser, snap)["invested"]["CAPM"]
    assert capm["n"] == 36 and exits == 11
    # Excess return = MKT + the exit cost booked on the month it sells. The costs are not
    # orthogonal to MKT in a sample, so alpha is the OLS intercept, not the mean cost.
    import numpy as np
    rows = [(s, n) for s, n in zip(ser, ser[1:] + [None]) if s["picks"]]
    mkt = [snap.months[at.realized_month(s["as_of"])]["ff3.Mkt-RF"] for s, _ in rows]
    y = [m + (n["strategy"] if n and not n["picks"] else 0.0) for m, (s, n) in zip(mkt, rows)]
    want, *_ = np.linalg.lstsq(np.column_stack([np.ones(36), mkt]), np.array(y), rcond=None)
    assert capm["alpha"] == pytest.approx(want[0], abs=1e-12)
    assert capm["loadings"]["MKT"][0] == pytest.approx(want[1], abs=1e-12)
    assert sum(y) - sum(mkt) == pytest.approx(-0.001 * exits, abs=1e-12)


def test_months_after_the_newest_factor_month_are_dropped_and_counted(tmp_path):
    fac = factor_table(60, start=(2014, 12))
    snap = fx.load(fx.fetch(web_for(fac, q_end=(2016, 12)), tmp_path, date(2026, 11, 26)))
    ser = series_from(fac, lambda f: (f["RF"] + f["MKT"], 0.0), start=(2015, 1), n=48)
    ser.append({"as_of": date(2019, 12, 31), "strategy": 0.01, "universe": 0.0, "picks": 1})   # 2020-01: no factors
    r = at.attribute(ser, snap)
    assert r["invested"]["CAPM"]["n"] == 48 and r["invested"]["CAPM"]["dropped"] == 1
    assert r["invested"]["q5"]["n"] == 24 and r["invested"]["q5"]["dropped"] == 25
    assert r["invested"]["q5"]["short"] is True and r["invested"]["CAPM"]["short"] is False


def test_too_few_months_gives_no_regression(tmp_path):
    fac = factor_table(60, start=(2014, 12))
    snap = fx.load(fx.fetch(web_for(fac), tmp_path, date(2026, 11, 26)))
    ser = series_from(fac, lambda f: (f["MKT"], 0.0), n=5)
    r = at.attribute(ser, snap)
    assert r["invested"]["CAPM"]["n"] == 5 and r["invested"]["CAPM"]["alpha"] is not None
    assert r["invested"]["FF5+Mom+QMJ+BAB"]["alpha"] is None        # 9 parameters, 5 months


# ---- report and CLI ---------------------------------------------------------------------

def report(results, meta):
    base = {"run_date": date(2026, 11, 26), "start": date(2015, 1, 31), "end": date(2018, 12, 31),
            "cost_bps": 50, "survivorship_gap": 0.1, "leak_check": None, "variants": {}}
    return bt.render_report(results, base | meta)


def test_report_has_a_factor_attribution_section(tmp_path):
    con = connect(":memory:")
    panel(con, [(date(2024, 1, 31), 1, "shortlist", 0.1), (date(2024, 1, 31), 2, "fail", 0.0)])
    res = {"value": {"as_first_reported": bt.evaluate(con, "ranked", cost_bps=0)}}
    fac = factor_table(60, start=(2014, 12))
    snap = fx.load(fx.fetch(web_for(fac), tmp_path, date(2026, 11, 26)))
    attr = {"value": at.attribute(series_from(fac, lambda f: (f["RF"] + f["MKT"], f["MKT"]), n=24), snap)}
    md = report(res, {"factors": snap, "attribution": attr})
    sec = md.split("## Factor attribution", 1)[1]
    assert "snapshot 2026-11-26" in sec and fx.load(snap.path).files["q5"]["sha256"][:12] in sec
    assert "Strategy minus RF, invested months only" in sec and "Strategy minus universe" in sec
    assert "| value | CAPM | 24 (short) |" in sec
    assert "newest factor month" in sec


def test_report_without_a_snapshot_says_how_to_get_one():
    con = connect(":memory:")
    panel(con, [(date(2024, 1, 31), 1, "shortlist", 0.1)])
    res = {"value": {"as_first_reported": bt.evaluate(con, "ranked", cost_bps=0)}}
    md = report(res, {"factors": None, "attribution": None})
    assert "## Factor attribution" in md and "`fetch-factors`" in md


def test_fetch_factors_command_writes_a_snapshot(tmp_path, monkeypatch, capsys):
    from src import cli
    web = web_for(factor_table(3, start=(2026, 7)))
    monkeypatch.setattr(fx, "FactorClient", lambda: web)
    cli.main(["fetch-factors", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert fx.latest(tmp_path) is not None and "2026-09" in out


def test_vault_has_a_sources_note_for_the_factor_files(tmp_path):
    from src.obsidian.sync import sync_vault
    con = connect(":memory:")
    sync_vault(con, tmp_path, date(2026, 11, 26))
    note = (tmp_path / "Sources" / "Factor returns (French, q5, AQR).md").read_text()
    assert "recompute" in note and "data/factors/YYYY-MM-DD" in note and "fetch-factors" in note
    assert "q5_factors_monthly_2025.csv" in note and "USA" in note
