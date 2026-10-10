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


class IntField(_Lenient):
    """Kept for property-api, whose year_built model extends it."""

    value: Optional[int] = None
    status: Optional[str] = None


class StrField(_Lenient):
    value: Optional[str] = None
    status: Optional[str] = None


class TitleField(StrField):
    # title types exactly as LINZ records them; used to tell "unresolved" from "unknown"
    linz_types: list[str] = Field(default_factory=list)


class YearField(_Lenient):
    """A year, or a decade.

    The council records a build decade: `value` is its first year and `decade_range` is
    `[from, to]` (inclusive). `year_built_between` matches when `decade_range` overlaps
    the rule's range. `[null, 1913]` means 1913 or earlier (no lower bound). An explicit
    `decade_range: null` (mixed eras) means no decade is known. Without a `decade_range`
    key, `value` (an int year or a string such as "1990s") is used.
    """

    value: Optional[Union[int, str]] = None
    status: Optional[str] = None
    decade_range: Optional[list[Optional[int]]] = Field(
        None,
        description="Inclusive [from, to] decade bounds; a null bound is open-ended. "
        "year_built_between matches when this range overlaps the rule's range. "
        "An explicit null means mixed eras (no decade).",
    )


class Property(_Lenient):
    cv: CV = Field(default_factory=CV)
    title_type: TitleField = Field(default_factory=TitleField)
    year_built: YearField = Field(default_factory=YearField)
    cladding: StrField = Field(default_factory=StrField)


class Hazard(_Lenient):
    layer: str
    # null or unknown values are accepted here; the engine maps them to "secondary" /
    # "unavailable" and adds a warning.
    priority: Optional[str] = "secondary"
    status: Optional[str] = "unavailable"
    reason: Optional[str] = None


class PropertyContext(_Lenient):
    property: Property = Field(default_factory=Property)
    hazards: list[Hazard] = Field(default_factory=list)


# ---- UserInputs -------------------------------------------------------------
class ChatSupplied(_Lenient):
    year_built: Optional[Union[int, str]] = None
    cladding: Optional[str] = None


MAX_AMOUNT = 1e10
MAX_RATE_PCT = 30
Amount = Annotated[float, Field(allow_inf_nan=False)]


class UserInputs(_Lenient):
    price_override: Optional[Amount] = Field(None, gt=0, le=MAX_AMOUNT)
    deposit: Optional[Amount] = Field(None, ge=0, le=MAX_AMOUNT)
    household_income: Optional[Amount] = Field(None, gt=0, le=MAX_AMOUNT)
    existing_debts: Optional[Amount] = Field(None, ge=0, le=MAX_AMOUNT)
    rate_pct: Optional[Amount] = Field(None, ge=0, le=MAX_RATE_PCT)
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
    applies_when: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Selection conditions, AND-ed across keys. Key `hazard_layer_intersects: [layer ids]` matches "
            "when any listed layer (exact id, any priority) has status `intersects`. It only selects items; "
            "it does not state flood history or risk level."
        ),
    )
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
    verified_content_hash: Optional[str] = None
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
    stress_delta_monthly: int  # stress_monthly minus the monthly payment at the highest rate
    per_bank: list[BankRate]
    source_ids: list[str] = Field(default_factory=lambda: ["bank_rates_snapshot", "rbnz_lvr"])


class UserRateScenario(BaseModel):
    id: Literal["user_rate"] = "user_rate"
    rate_pct: float
    monthly: int
    fortnightly: int
    stress_monthly: int
    stress_delta_monthly: int


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
    stress_delta_monthly: Optional[int] = None
    monthly_if_full_premium: Optional[int] = None


class FirstHomeLoanScenario(BaseModel):
    id: Literal["first_home_loan"] = "first_home_loan"
    eligibility: Literal["eligible", "conditional"] = "eligible"
    eligibility_note: Optional[str] = None
    income_caps: dict[str, int] = Field(default_factory=dict)
    lmi_amount: float  # full 1.2% premium; lenders may pass on less
    lmi_note: str = ""
    # Section 6.4 top-level fields, kept for compatibility. Always null: see lenders[]
    # for each lender's figures and monthly_range / stress_monthly_range for ranges.
    monthly: Optional[int] = Field(
        None, description="Always null. Per-lender figures are in lenders[]; the range is in monthly_range.")
    stress_monthly: Optional[int] = Field(
        None, description="Always null. Per-lender figures are in lenders[]; the range is in stress_monthly_range.")
    stress_delta_monthly: Optional[int] = Field(
        None, description="Always null. See lenders[].stress_delta_monthly.")
    lenders: list[FhlLender] = Field(default_factory=list)
    # Over lenders with a known rate; each uses its own pass-through where known,
    # otherwise the full premium.
    monthly_range: Optional[list[int]] = None
    fortnightly_range: Optional[list[int]] = None
    stress_monthly_range: Optional[list[int]] = None
    # Lenders whose pass-through is unknown, so the ranges assume the full 1.2% premium.
    full_premium_assumed_for: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(
        default_factory=lambda: ["kainga_ora_fhl_brochure", "kainga_ora_fhl"]
    )
    note_ids: list[str] = Field(default_factory=lambda: ["fhl_passthrough_varies"])


Scenario = Annotated[
    Union[BankStandardScenario, UserRateScenario, FirstHomeLoanScenario],
    Field(discriminator="id"),
]


DISCLAIMER = (
    "Information only, not financial or legal advice. Figures come from published sources "
    "on the dates shown; confirm with the lender or provider before deciding."
)


class HazardGap(BaseModel):
    layer: str
    status: str
    reason: Optional[str] = None


class RulesOutput(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: str = Field(description="ISO 8601 timestamp in New Zealand time (Pacific/Auckland, e.g. +13:00)")
    price_used: PriceUsed
    deposit: Optional[float] = None
    loan_amount: Optional[float] = None
    lvr_pct: Optional[float] = None
    dti: Optional[float] = None
    high_dti_threshold: Optional[float] = None
    rates_snapshot_date: Optional[str] = None
    stress_add_pp: float = 2.0
    loan_term_years: Optional[int] = Field(
        None, description="Loan term in years used for every repayment figure (30).")
    rates_stale_after_days: Optional[int] = Field(
        None, description="Age in days after which the rates snapshot counts as stale (`rates_snapshot_stale`).")
    default_rate_pct: Optional[float] = Field(
        None, description="Median 1-year standard rate (%) across the banks in the rates snapshot, 2 decimals; "
                          "null when the snapshot has no such rates. A suggested starting rate for the user.")
    fhl_income_caps: dict[str, int] = Field(
        default_factory=dict,
        description="First Home Loan income caps by household (`single`, `multiple_or_dependants`). Always "
                    "returned, including when `first_home_loan` is not (flag `fhl_income_over_cap`).")
    scenarios: list[Scenario] = Field(default_factory=list)
    flags: list[Flag] = Field(
        default_factory=list, description="Active flags only. Codes are listed in CODES.md.")
    hazard_gaps: list[HazardGap] = Field(
        default_factory=list, description="Primary hazard layers that could not be checked, with the original reason.")
    kb_items: list[str] = Field(default_factory=list)
    kb_includes_draft: bool = False
    kb_items_from_user_input: list[str] = Field(default_factory=list)
    kb_empty_reason: Optional[str] = None
    warnings: list[str] = Field(
        default_factory=list, description="Machine-readable warning codes. Codes are listed in CODES.md.")
    disclaimer: str = DISCLAIMER


# ---- Error responses (every 4xx/5xx) ----------------------------------------
class ErrorField(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    fields: list[ErrorField] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorBody
