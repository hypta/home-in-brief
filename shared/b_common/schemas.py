"""Shared pydantic v2 models for the rules-api and its consumers."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1"


class _Lenient(BaseModel):
    """Inputs may carry extra fields; we ignore them."""

    model_config = ConfigDict(extra="ignore")


# ---- PropertyContext (teammate spec 6.2, only the fields we read) ----------
class CV(_Lenient):
    value: Optional[float] = None
    valuation_date: Optional[str] = None
    status: Optional[str] = None


class StrField(_Lenient):
    value: Optional[str] = None


class IntField(_Lenient):
    value: Optional[int] = None


class Property(_Lenient):
    cv: CV = Field(default_factory=CV)
    title_type: StrField = Field(default_factory=StrField)
    year_built: IntField = Field(default_factory=IntField)
    cladding: StrField = Field(default_factory=StrField)


class Hazard(_Lenient):
    layer: str
    priority: Literal["primary", "secondary"] = "secondary"
    status: Literal["intersects", "none", "unavailable", "not_found"] = "none"


class PropertyContext(_Lenient):
    property: Property = Field(default_factory=Property)
    hazards: list[Hazard] = Field(default_factory=list)


# ---- UserInputs -------------------------------------------------------------
class ChatSupplied(_Lenient):
    year_built: Optional[int] = None
    cladding: Optional[str] = None


class UserInputs(_Lenient):
    price_override: Optional[float] = None
    deposit: Optional[float] = None
    household_income: Optional[float] = None
    existing_debts: Optional[float] = None
    rate_pct: Optional[float] = None
    household_type: Optional[
        Literal["single", "single_with_dependants", "multiple"]
    ] = None
    chat_supplied: ChatSupplied = Field(default_factory=ChatSupplied)


class RulesRequest(BaseModel):
    property_context: PropertyContext = Field(default_factory=PropertyContext)
    user_inputs: UserInputs = Field(default_factory=UserInputs)


# ---- Rates snapshot ---------------------------------------------------------
class LemBand(BaseModel):
    lvr_from: float
    lvr_to: float
    margin_pct: float


class LowEquity(BaseModel):
    mechanism: Literal["lem", "standard_rate"]
    lem_base: Optional[Literal["special", "unverified"]] = None
    bands: list[LemBand] = Field(default_factory=list)


class Fhl(BaseModel):
    lender: bool
    lmi_passthrough_pct: Optional[float] = None


class BankRates(BaseModel):
    bank_id: str
    name: str
    source_url: str
    page_sha256: Optional[str] = None
    page_effective_date: Optional[str] = None
    special: dict[str, float] = Field(default_factory=dict)
    standard: Optional[dict[str, float]] = None
    floating: Optional[float] = None
    special_requires_equity_pct: Optional[float] = 20
    low_equity: LowEquity
    fhl: Fhl
    notes: list[str] = Field(default_factory=list)


class RatesSnapshot(BaseModel):
    schema_version: str = SCHEMA_VERSION
    captured_at: Optional[str] = None
    banks: list[BankRates] = Field(default_factory=list)


# ---- Knowledge base ---------------------------------------------------------
class Source(BaseModel):
    id: str
    title: str
    url: str
    licence: Optional[str] = None
    accessed_at: Optional[str] = None
    source_type: Literal["official", "secondary"] = "official"


class KbItem(BaseModel):
    id: str
    topic: str
    applies_when: dict[str, Any] = Field(default_factory=dict)
    claim_en: str
    quote: Optional[str] = None
    source_id: str
    source_url: Optional[str] = None
    source_title: Optional[str] = None
    source_type: Literal["official", "secondary"] = "official"
    licence: Optional[str] = None
    next_step_en: Optional[str] = None
    verified_by: Optional[str] = None
    verified_at: Optional[str] = None
    status: Literal["draft", "verified", "needs_review", "retired"] = "draft"
    last_check: Optional[Any] = None


# ---- RulesOutput ------------------------------------------------------------
class PriceUsed(BaseModel):
    value: Optional[float] = None
    basis: Literal["user_override", "cv", "none"] = "none"


class Flag(BaseModel):
    id: str
    active: bool = True
    source_id: Optional[str] = None


class BankRate(BaseModel):
    bank_id: str
    rate_pct: float
    basis: str


class BankStandardScenario(BaseModel):
    rate_range_pct: list[float]
    low_equity_margin_pct: Optional[list[float]] = None
    monthly_range: list[int]
    fortnightly_range: list[int]
    stress_monthly: int
    per_bank: list[BankRate]
    source_ids: list[str] = Field(default_factory=lambda: ["bank_rates_snapshot", "rbnz_lvr"])


class UserRateScenario(BaseModel):
    rate_pct: float
    monthly: int
    fortnightly: int
    stress_monthly: int


class FirstHomeLoanScenario(BaseModel):
    rate_pct: float
    lender_bank_id: Optional[str] = None
    lmi_amount: float
    lmi_capitalised: bool = True
    loan_with_lmi: float
    monthly: int
    fortnightly: int
    source_ids: list[str] = Field(default_factory=lambda: ["kainga_ora_fhl"])
    note_ids: list[str] = Field(default_factory=lambda: ["fhl_passthrough_varies"])


class Scenarios(BaseModel):
    bank_standard: Optional[BankStandardScenario] = None
    user_rate: Optional[UserRateScenario] = None
    first_home_loan: Optional[FirstHomeLoanScenario] = None


class RulesOutput(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: str
    price_used: PriceUsed
    deposit: Optional[float] = None
    loan: Optional[float] = None
    lvr_pct: Optional[float] = None
    dti: Optional[float] = None
    high_dti_threshold: Optional[float] = None
    stress_add_pp: float = 2.0
    scenarios: Scenarios = Field(default_factory=Scenarios)
    flags: list[Flag] = Field(default_factory=list)
    kb_items: list[str] = Field(default_factory=list)
    kb_includes_draft: bool = False
    warnings: list[str] = Field(default_factory=list)
