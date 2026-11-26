"""Factor attribution of the backtest's monthly strategy (M12-A5). Informational only.

The strategy's return for as_of month end M is realized over month M+1 (an April 30 entry exits
May 31), so it is regressed on month M+1's factor returns. Two responses:
- strategy minus RF, over invested months only: cash months would pull every loading toward 0.
  RF is the model's own (French's for the French models, q5's for q5).
- strategy minus universe, over every measured month: both legs are portfolios, so RF cancels
  and is not subtracted.
Factor returns are long-short or excess returns already, so neither side subtracts RF twice.
OLS with classical t-statistics; non-overlapping monthly returns.
"""
from __future__ import annotations

import math

from src.ingestion.factors import Snapshot

MIN_MONTHS = 36         # fewer: the regression is flagged short
_FF5 = {"MKT": "ff5.Mkt-RF", "SMB": "ff5.SMB", "HML": "ff5.HML", "RMW": "ff5.RMW", "CMA": "ff5.CMA",
        "MOM": "mom.Mom"}
MODELS: dict[str, tuple[str, dict[str, str]]] = {         # name: (risk-free key, {label: factor key})
    "CAPM": ("ff3.RF", {"MKT": "ff3.Mkt-RF"}),
    "FF3": ("ff3.RF", {"MKT": "ff3.Mkt-RF", "SMB": "ff3.SMB", "HML": "ff3.HML"}),
    "FF5+Mom": ("ff5.RF", _FF5),
    "q5": ("q5.R_F", {"MKT": "q5.R_MKT", "ME": "q5.R_ME", "IA": "q5.R_IA", "ROE": "q5.R_ROE", "EG": "q5.R_EG"}),
    "FF5+Mom+QMJ+BAB": ("ff5.RF", _FF5 | {"QMJ": "qmj.USA", "BAB": "bab.USA"}),
}


def _inverse(a: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan with partial pivoting; None when the matrix is singular."""
    n = len(a)
    m = [row[:] + [float(i == j) for j in range(n)] for i, row in enumerate(a)]
    scale = max(abs(v) for row in a for v in row) or 1.0
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) <= 1e-12 * scale:
            return None
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c]
                m[r] = [v - f * w for v, w in zip(m[r], m[c])]
    return [row[n:] for row in m]


def ols(y: list[float], X: list[list[float]]) -> dict | None:
    """y on an intercept and the columns of X. coef and t start with the intercept. None when
    there are no residual degrees of freedom or the factors are collinear."""
    n = len(y)
    A = [[1.0] + list(r) for r in X]
    p = len(A[0]) if A else 0
    if n <= p:
        return None
    inv = _inverse([[sum(r[i] * r[j] for r in A) for j in range(p)] for i in range(p)])
    if inv is None:
        return None
    xty = [sum(r[i] * v for r, v in zip(A, y)) for i in range(p)]
    coef = [sum(inv[i][j] * xty[j] for j in range(p)) for i in range(p)]
    resid = [v - sum(c * x for c, x in zip(coef, r)) for r, v in zip(A, y)]
    ssr = sum(e * e for e in resid)
    mean = sum(y) / n
    sst = sum((v - mean) ** 2 for v in y)
    s2 = ssr / (n - p)
    t = [c / math.sqrt(inv[i][i] * s2) if inv[i][i] * s2 > 0 else None for i, c in enumerate(coef)]
    return {"coef": coef, "t": t, "r2": 1 - ssr / sst if sst > 0 else None, "n": n}


def realized_month(as_of) -> tuple[int, int]:
    return (as_of.year + 1, 1) if as_of.month == 12 else (as_of.year, as_of.month + 1)


def _fit(rows: list[tuple[tuple[int, int], float]], snap: Snapshot, rf_key: str | None,
         factors: dict[str, str]) -> dict:
    """rows: (realized month, response before RF). rf_key None: no RF is subtracted."""
    need = list(factors.values()) + ([rf_key] if rf_key else [])
    y, X, dropped = [], [], 0
    for ym, r in rows:
        f = snap.months.get(ym, {})
        if any(k not in f for k in need):
            dropped += 1
            continue
        y.append(r - (f[rf_key] if rf_key else 0.0))
        X.append([f[k] for k in factors.values()])
    fit = ols(y, X)
    out = {"n": len(y), "dropped": dropped, "short": len(y) < MIN_MONTHS,
           "alpha": None, "alpha_t": None, "alpha_annual": None, "r2": None, "loadings": {}}
    if fit:
        out |= {"alpha": fit["coef"][0], "alpha_t": fit["t"][0], "alpha_annual": 12 * fit["coef"][0],
                "r2": fit["r2"],
                "loadings": {lab: (c, t) for lab, c, t in zip(factors, fit["coef"][1:], fit["t"][1:])}}
    return out


def attribute(series: list[dict], snap: Snapshot) -> dict:
    """series: evaluate()["series"], measured months only. Returns
    {"invested": {model: fit}, "active": {model: fit}, "counts": {...}}."""
    invested = [(realized_month(s["as_of"]), s["strategy"]) for s in series if s["picks"]]
    active = [(realized_month(s["as_of"]), s["strategy"] - s["universe"]) for s in series]
    return {"invested": {m: _fit(invested, snap, rf, f) for m, (rf, f) in MODELS.items()},
            "active": {m: _fit(active, snap, None, f) for m, (rf, f) in MODELS.items()},
            "counts": {"months": len(series), "cash_months": len(series) - len(invested)}}


# ---- report ---------------------------------------------------------------------------------

def _pct(v, digits: int = 2) -> str:
    return "-" if v is None else f"{v * 100:.{digits}f}%"


def _t(v) -> str:
    return "-" if v is None else f"{v:.1f}"


def render(snap: Snapshot | None, attribution: dict[str, dict] | None) -> list[str]:
    lines = ["## Factor attribution", ""]
    if snap is None or not attribution:
        return lines + ["No factor snapshot: run `fetch-factors`, then the backtest again.", ""]
    lines += [f"Monthly strategy returns after costs (as first reported), regressed on the factor returns of the "
              f"month in which they were realized: an April 30 entry is explained by May. OLS, t-statistics in "
              f"brackets; alpha is per month, and x12 per year. `short` marks fewer than {MIN_MONTHS} months. "
              f"Months with no factor row (after the newest factor month) are dropped and counted.", "",
              f"Factor snapshot {snap.snapshot_date.isoformat()} (`{snap.path}`):"]
    for name, f in snap.files.items():
        lines.append(f"- {name}: `{f['file']}`, sha256 {f['sha256'][:12]}, {f['first_month']} to "
                     f"newest factor month {f['newest_month']}")
    for key, title in (("invested", "Strategy minus RF, invested months only"),
                       ("active", "Strategy minus universe, all measured months (RF cancels)")):
        lines += ["", f"### {title}", "",
                  "| variant | model | months | alpha/mo | t | alpha/yr | R2 | loadings (t) | dropped |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for variant, a in attribution.items():
            for model, r in a[key].items():
                n = f"{r['n']} (short)" if r["short"] else str(r["n"])
                load = ", ".join(f"{lab} {c:.2f} ({_t(t)})" for lab, (c, t) in r["loadings"].items()) or "-"
                r2 = "-" if r["r2"] is None else f"{r['r2']:.2f}"
                lines.append(f"| {variant} | {model} | {n} | {_pct(r['alpha'])} | {_t(r['alpha_t'])} | "
                             f"{_pct(r['alpha_annual'], 1)} | {r2} | {load} | {r['dropped']} |")
    cash = {v: a["counts"]["cash_months"] for v, a in attribution.items()}
    lines += ["", f"- Cash months left out of the invested regressions: {cash}.", ""]
    return lines
