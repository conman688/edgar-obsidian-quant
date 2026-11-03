"""Quote-anchored extraction schemas.

The model never supplies a number we trust. It supplies a verbatim quote and the
table's scale header; Python checks the quote exists in the source chunk and
parses the number from the quote itself. Fiscal years are checked against the
filing's own period. Ratios, CAGRs and EV are computed in DuckDB, never here.

Validate with: Model.model_validate(data, context={"source": chunk_text, "fiscal_year": 2024})
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

SCALE = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}
_NUM = re.compile(r"\(?-?\$?\s*\d[\d,]*(?:\.\d+)?\)?")
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s.replace(" ", " ").replace("’", "'")).strip().lower()


class Evidence(BaseModel):
    quote: str = Field(min_length=8, max_length=600, description="Verbatim sentence or table row from the source.")

    @field_validator("quote")
    @classmethod
    def _in_source(cls, v: str, info: ValidationInfo) -> str:
        src = (info.context or {}).get("source")
        if src is None:
            raise ValueError("validation context must include 'source'")
        if _norm(v) not in _norm(src):
            raise ValueError("quote not found verbatim in source chunk")
        return v


class Amount(Evidence):
    """A money/share figure. `value` is derived from `quote`, never taken from the model."""
    number_text: str = Field(description="The exact number as printed inside quote, e.g. '(1,234.5)'.")
    scale: Literal["units", "thousands", "millions", "billions"] = Field(
        description="From the table header, e.g. '(in thousands)'. Use 'units' only if none is stated."
    )
    fiscal_year: int = Field(description="Fiscal year of the column this number came from.")
    value: Decimal | None = None

    @model_validator(mode="after")
    def _derive(self, info: ValidationInfo) -> "Amount":
        if self.number_text not in self.quote:
            raise ValueError("number_text must appear inside quote")
        if not _NUM.fullmatch(self.number_text.strip()):
            raise ValueError(f"unparseable number_text {self.number_text!r}")
        raw = self.number_text.strip()
        neg = raw.startswith("(") or "-" in raw
        digits = re.sub(r"[^\d.]", "", raw)
        v = Decimal(digits) * SCALE[self.scale]
        self.value = -v if neg else v
        fy = (info.context or {}).get("fiscal_year")
        # A 10-K shows the current year plus up to two comparatives. Anything else is a misread column.
        if fy is not None and not (fy - 2 <= self.fiscal_year <= fy):
            raise ValueError(f"fiscal_year {self.fiscal_year} outside filing window {fy - 2}..{fy}")
        return self


class DebtMaturity(BaseModel):
    year: int
    amount: Amount


class RedFlag(BaseModel):
    kind: Literal[
        "off_balance_sheet", "guarantee", "pension_underfunding", "customer_concentration",
        "covenant", "litigation_reserve", "going_concern", "material_weakness",
        "accounting_policy_change", "related_party", "impairment",
    ]
    summary: str = Field(max_length=300)
    evidence: Evidence
    amount: Amount | None = None


class Acquisition(BaseModel):
    target: str
    announced_or_closed: str | None = Field(None, description="Date text as printed.")
    consideration: Amount | None = None
    goodwill_recognized: Amount | None = None
    evidence: Evidence


class FootnoteAnalysis(BaseModel):
    schema_version: Literal[2] = 2
    red_flags: list[RedFlag] = []
    acquisitions: list[Acquisition] = []
    debt_maturities: list[DebtMaturity] = []
    goodwill_impairments: list[Amount] = []


class IncentiveMetric(BaseModel):
    metric: Literal[
        "adjusted_ebitda", "ebitda", "revenue", "eps", "adjusted_eps", "tsr_relative", "tsr_absolute",
        "roic", "roe", "fcf", "fcf_per_share", "operating_income", "individual", "other",
    ]
    plan: Literal["annual_bonus", "ltip", "psu", "other"]
    weight_pct: float | None = Field(None, ge=0, le=100)
    evidence: Evidence


class NamedExecutive(BaseModel):
    full_name: str = Field(description="Name exactly as printed in the Summary Compensation Table.")
    title: str
    total_comp: Amount | None = None
    evidence: Evidence


class ProxyAnalysis(BaseModel):
    schema_version: Literal[2] = 2
    executives: list[NamedExecutive] = []
    incentive_metrics: list[IncentiveMetric] = []
    related_party: list[RedFlag] = []
