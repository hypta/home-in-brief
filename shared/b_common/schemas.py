"""Shared pydantic v2 models for the rules-api and its consumers."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

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
    status: Optional[str] = None


class YearField(_Lenient):
    """An int year, or a decade string such as "1990s"."""

    value: Optional[Union[int, str]] = None
    status: Optional[str] = None


class Property(_Lenient):
    cv: CV = Field(default_factory=CV)
    title_type: StrField = Field(default_factory=StrField)
    year_built: YearField = Field(default_factory=YearField)
    cladding: StrField = Field(default_factory=StrField)


class Hazard(_Lenient):
    layer: str
    priority: Literal["primary", "secondary"] = "secondary"
    # Unknown strings are accepted here and mapped to "unavailable" by the engine.
    status: str = "unavailable"


class PropertyContext(_Lenient):
    property: Property = Field(default_factory=Property)
    hazards: list[Hazard] = Field(default_factory=list)


# ---- UserInputs -------------------------------------------------------------
class ChatSupplied(_Lenient):
    year_built: Optional[Union[int, str]] = None
    cladding: Optional[str] = None


Amount = Annotated[float, Field(allow_inf_nan=False)]


class UserInputs(_Lenient):
    price_override: Optional[Amount] = Field(None, gt=0)
    deposit: Optional[Amount] = Field(None, ge=0)
    household_income: Optional[Amount] = Field(None, gt=0)
    existing_debts: Optional[Amount] = Field(None, ge=0)
    rate_pct: Optional[Amount] = Field(None, ge=0, le=100)
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
    # True: FHL borrowers get the special rate despite <20% equity; null: unverified
    special_applies: Optional[bool] = None


class BankRates(BaseModel):
    bank_id: str
    name: str
    source_url: str
    captured_at: Optional[str] = None
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
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    url: str
    licence: Optional[str] = None
    accessed_at: Optional[str] = None
    source_type: Literal["official", "secondary"] = "official"
    attribution: Optional[str] = None
    tos_status: Optional[str] = None
    tos_url: Optional[str] = None
    tos_checked_at: Optional[str] = None


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
    basis: Literal["cv", "user", "none"] = "none"
    valuation_date: Optional[str] = None


class Flag(BaseModel):
    id: str
    source_ids: list[str] = Field(default_factory=list)


class BankRate(BaseModel):
    bank_id: str
    rate_pct: float
    basis: str
    captured_at: Optional[str] = None


class BankStandardScenario(BaseModel):
    id: Literal["bank_standard"] = "bank_standard"
    rate_pct: Optional[float] = None  # the user's own rate, if given
    rate_range_pct: list[float]
    low_equity_margin_pct: Optional[list[float]] = None
    monthly_range: list[int]
    fortnightly_range: list[int]
    stress_monthly: int
    per_bank: list[BankRate]
    source_ids: list[str] = Field(default_factory=lambda: ["bank_rates_snapshot", "rbnz_lvr"])


class UserRateScenario(BaseModel):
    id: Literal["user_rate"] = "user_rate"
    rate_pct: float
    monthly: int
    fortnightly: int
    stress_monthly: int


class CandidateRates(BaseModel):
    special: Optional[float] = None
    standard: Optional[float] = None


class FhlLender(BaseModel):
    bank_id: str
    name: str
    captured_at: Optional[str] = None
    rate_pct: Optional[float] = None  # null when it is not known which rate applies
    rate_basis: Literal["special", "standard", "unknown"] = "unknown"
    candidate_rates: CandidateRates = Field(default_factory=CandidateRates)
    lmi_passthrough_pct: Optional[float] = None
    lmi_amount: Optional[float] = None  # null when the pass-through is unknown
    lmi_amount_if_full_premium: float
    loan_with_lmi: Optional[float] = None
    monthly: Optional[int] = None
    fortnightly: Optional[int] = None
    stress_monthly: Optional[int] = None
    monthly_if_full_premium: Optional[int] = None


class FirstHomeLoanScenario(BaseModel):
    id: Literal["first_home_loan"] = "first_home_loan"
    eligibility: Literal["eligible", "conditional"] = "eligible"
    eligibility_note: Optional[str] = None
    income_caps: dict[str, int] = Field(default_factory=dict)
    lmi_amount: float  # full 1.2% premium; lenders may pass on less
    lmi_note: str = ""
    lenders: list[FhlLender] = Field(default_factory=list)
    # Over lenders with a known rate; each uses its own pass-through where known,
    # otherwise the full premium.
    monthly_range: Optional[list[int]] = None
    fortnightly_range: Optional[list[int]] = None
    stress_monthly_range: Optional[list[int]] = None
    source_ids: list[str] = Field(default_factory=lambda: ["kainga_ora_fhl"])
    note_ids: list[str] = Field(default_factory=lambda: ["fhl_passthrough_varies"])


Scenario = Annotated[
    Union[BankStandardScenario, UserRateScenario, FirstHomeLoanScenario],
    Field(discriminator="id"),
]


class RulesOutput(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: str
    price_used: PriceUsed
    deposit: Optional[float] = None
    loan_amount: Optional[float] = None
    lvr_pct: Optional[float] = None
    dti: Optional[float] = None
    high_dti_threshold: Optional[float] = None
    rates_snapshot_date: Optional[str] = None
    stress_add_pp: float = 2.0
    scenarios: list[Scenario] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    kb_items: list[str] = Field(default_factory=list)
    kb_includes_draft: bool = False
    kb_items_from_user_input: list[str] = Field(default_factory=list)
    kb_empty_reason: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
