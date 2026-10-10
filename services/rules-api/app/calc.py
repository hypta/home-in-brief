"""Deterministic pure-function calculations for the rules engine."""
from __future__ import annotations

import math
import re
import statistics
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from b_common.schemas import (
    MAX_AMOUNT,
    BankRate,
    BankStandardScenario,
    CandidateRates,
    FhlLender,
    FirstHomeLoanScenario,
    Flag,
    HazardGap,
    PriceUsed,
    PropertyContext,
    RatesSnapshot,
    RulesOutput,
    UserInputs,
    UserRateScenario,
)

STRESS_ADD_PP = 2.0
FHL_PREMIUM_PCT = 1.2
FHL_MIN_DEPOSIT_PCT = 5.0
FHL_CAP_SINGLE = 95000
FHL_CAP_MULTIPLE_OR_DEPENDANTS = 150000
FHL_INCOME_CAPS = {"single": FHL_CAP_SINGLE, "multiple_or_dependants": FHL_CAP_MULTIPLE_OR_DEPENDANTS}
FHL_LMI_NOTE = (
    "Kāinga Ora charges lenders a 1.2% lenders' mortgage insurance premium, and each lender passes on a "
    "different share of it to the borrower. For lenders that have not published their share, the repayment "
    "range assumes the full 1.2% premium is added to the loan."
)
TERM_YEARS = 30
MONTHLY, FORTNIGHTLY = 12, 26
STALE_DAYS = 30
HAZARD_STATUSES = {"intersects", "none", "unavailable", "not_found"}
HAZARD_PRIORITIES = {"primary", "secondary"}
NZ_TZ = ZoneInfo("Pacific/Auckland")


class InputError(ValueError):
    """Invalid user input; the API maps it to HTTP 422."""

    def __init__(self, field_name: str, reason: str):
        super().__init__(f"{field_name}: {reason}")
        self.field_name, self.reason = field_name, reason


def money(x: float) -> str:
    """Whole-dollar display for user-facing text, e.g. 340000.0 -> "$340,000"."""
    return f"${x:,.0f}"


FHL_CONDITIONAL_NOTE = (
    "Eligible only for two or more applicants, or a single applicant with dependants "
    f"(income cap {money(FHL_CAP_MULTIPLE_OR_DEPENDANTS)}); single applicants are capped at {money(FHL_CAP_SINGLE)}."
)


def default_rate_pct(snap: Optional[RatesSnapshot]) -> Optional[float]:
    """Median 1-year standard rate across the banks in the snapshot, 2 decimals; None if there is none."""
    vals = [r for b in snap.banks if (r := (b.standard or {}).get("1y")) is not None] if snap else []
    return round(statistics.median(vals), 2) if vals else None


def payment(principal: float, rate_pct: float, periods_per_year: int, years: int = TERM_YEARS) -> int:
    """Level principal-and-interest payment per period, rounded to a whole dollar."""
    if not (math.isfinite(principal) and math.isfinite(rate_pct)) or rate_pct < 0:
        raise ValueError("principal and rate must be finite and the rate non-negative")
    n = years * periods_per_year
    r = rate_pct / 100 / periods_per_year
    if r == 0:
        return round(principal / n)
    # P*r / (1 - (1+r)^-n), written with expm1/log1p so tiny r stays accurate (-> P/n)
    return round(principal * r / -math.expm1(-n * math.log1p(r)))


def _pay_set(loan: float, rate_pct: float) -> tuple[int, int, int]:
    return (
        payment(loan, rate_pct, MONTHLY),
        payment(loan, rate_pct, FORTNIGHTLY),
        payment(loan, rate_pct + STRESS_ADD_PP, MONTHLY),
    )


# ---- property context normalisation ----------------------------------------
@dataclass
class Normalized:
    cv_value: Optional[float] = None
    valuation_date: Optional[str] = None
    title_type: Optional[str] = None
    year_range: Optional[tuple[int, int]] = None
    cladding: Optional[str] = None
    hazards: list[dict] = field(default_factory=list)
    hazard_gaps: list[HazardGap] = field(default_factory=list)


@dataclass
class Raw:
    """Unrounded values: flags and KB selection use these, not the displayed ones."""

    lvr: Optional[float] = None
    dti: Optional[float] = None
    fhl_eligible: bool = False


_DECADE = re.compile(r"^\s*([0-9]{3})0s\s*$")
_YEAR = re.compile(r"^\s*[0-9]{1,4}\s*$")
MIN_YEAR, MAX_YEAR = 1000, 2200
OPEN_LOW, OPEN_HIGH = -10**6, 10**6  # open-ended decade bounds


def parse_year(v: Union[int, str, None]) -> Optional[tuple[int, int]]:
    """A year or a decade string as an inclusive range; None if it is not a plausible year."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return (v, v) if MIN_YEAR <= v <= MAX_YEAR else None
    if isinstance(v, str):
        if _YEAR.match(v):
            y = int(v)
            return (y, y) if MIN_YEAR <= y <= MAX_YEAR else None
        m = _DECADE.match(v)
        if m:
            y = int(m.group(1)) * 10
            return (y, y + 9) if MIN_YEAR <= y <= MAX_YEAR else None
    return None


def parse_decade_range(r: list) -> Optional[tuple[int, int]]:
    if len(r) != 2 or any(x is not None and (isinstance(x, bool) or not MIN_YEAR <= x <= MAX_YEAR) for x in r):
        return None
    lo = OPEN_LOW if r[0] is None else r[0]
    hi = OPEN_HIGH if r[1] is None else r[1]
    return (lo, hi) if lo <= hi else None


def normalize(ctx: PropertyContext, ui: UserInputs, warnings: list[str]) -> Normalized:
    """Use a property field only when its status is "found"; chat fills year and cladding."""
    p, n = ctx.property, Normalized()

    def found(name: str, status: Optional[str]) -> bool:
        if status != "found":
            warnings.append(f"{name}_unavailable")
            return False
        return True

    if found("cv", p.cv.status):
        v = p.cv.value
        if v is not None and math.isfinite(v) and 0 < v <= MAX_AMOUNT:
            n.cv_value, n.valuation_date = v, p.cv.valuation_date
        elif v is not None:
            warnings.append("cv_invalid")
    if found("title_type", p.title_type.status):
        n.title_type = p.title_type.value
        if n.title_type is None and p.title_type.linz_types:
            warnings.append("title_type_unresolved")  # LINZ lists types but none was chosen: do not guess
    if found("year_built", p.year_built.status):
        yb = p.year_built
        if "decade_range" in yb.model_fields_set and yb.decade_range is None:
            pass  # explicit null: mixed eras, so no decade is known
        elif yb.decade_range is not None:
            n.year_range = parse_decade_range(yb.decade_range)
            if n.year_range is None:
                warnings.append("year_built_unparseable")
        else:
            n.year_range = parse_year(yb.value)
            if yb.value is not None and n.year_range is None:
                warnings.append("year_built_unparseable")
    if found("cladding", p.cladding.status):
        n.cladding = p.cladding.value
    chat = ui.chat_supplied
    if n.year_range is None and chat.year_built is not None:
        n.year_range = parse_year(chat.year_built)
        if n.year_range is None:
            warnings.append("chat_year_built_unparseable")
    if n.cladding is None and chat.cladding:
        n.cladding = chat.cladding

    for h in ctx.hazards:
        status, priority = h.status, h.priority
        if status is None:
            warnings.append(f"hazard_status_missing:{h.layer}")
            status = "unavailable"
        elif status not in HAZARD_STATUSES:
            warnings.append(f"hazard_status_unknown:{h.layer}")
            status = "unavailable"
        if priority is None or priority not in HAZARD_PRIORITIES:
            warnings.append(f"hazard_priority_unknown:{h.layer}")
            priority = "secondary"
        n.hazards.append({"layer": h.layer, "priority": priority, "status": status})
        if priority == "primary" and status in ("unavailable", "not_found"):
            warnings.append(f"hazards_partial:{h.layer}")
            n.hazard_gaps.append(HazardGap(layer=h.layer, status=status, reason=h.reason))
    primary = [h for h in n.hazards if h["priority"] == "primary"]
    if not primary or all(h["status"] == "unavailable" for h in primary):
        warnings.append("hazards_unavailable")
    return n


def resolve_price(n: Normalized, ui: UserInputs) -> PriceUsed:
    if ui.price_override is not None:
        return PriceUsed(value=ui.price_override, basis="user")
    if n.cv_value is not None:
        return PriceUsed(value=n.cv_value, basis="cv", valuation_date=n.valuation_date)
    return PriceUsed(value=None, basis="none")


def snapshot_date(snap: Optional[RatesSnapshot]) -> Optional[str]:
    """Oldest per-bank captured_at (date part); falls back to the top-level one."""
    if snap is None:
        return None
    dates = [b.captured_at[:10] for b in snap.banks if b.captured_at]
    if dates:
        return min(dates)
    return snap.captured_at[:10] if snap.captured_at else None


def band_margin(bands, lvr: float) -> Optional[float]:
    # bands start at e.g. 80.01; tolerate the 0.01 gap for unrounded LVRs
    for b in bands:
        if b.lvr_from - 0.01 < lvr <= b.lvr_to:
            return b.margin_pct
    return None


def bank_standard(
    loan: float, lvr: float, snap: RatesSnapshot, warnings: list[str], user_rate: Optional[float]
) -> Optional[BankStandardScenario]:
    per_bank: list[BankRate] = []
    margins: list[float] = []
    for bank in snap.banks:
        le = bank.low_equity
        if lvr <= 80:
            rate, basis = bank.special.get("1y"), "special"
        elif le.mechanism == "lem":
            base, m = bank.special.get("1y"), band_margin(le.bands, lvr)
            if base is None or m is None:
                warnings.append(f"bank_skipped:{bank.bank_id}")
                continue
            rate, basis = round(base + m, 4), "special+lem"
            margins.append(m)
            if le.lem_base == "unverified":
                warnings.append(f"lem_base_unverified:{bank.bank_id}")
        else:
            rate, basis = (bank.standard or {}).get("1y"), "standard"
        if rate is None:
            warnings.append(f"bank_skipped:{bank.bank_id}")
            continue
        per_bank.append(BankRate(bank_id=bank.bank_id, rate_pct=rate, basis=basis, captured_at=bank.captured_at))
    if not per_bank:
        return None
    lo, hi = min(b.rate_pct for b in per_bank), max(b.rate_pct for b in per_bank)
    return BankStandardScenario(
        rate_pct=user_rate,
        rate_range_pct=[lo, hi],
        low_equity_margin_pct=[min(margins), max(margins)] if margins else None,
        monthly_range=[payment(loan, lo, MONTHLY), payment(loan, hi, MONTHLY)],
        fortnightly_range=[payment(loan, lo, FORTNIGHTLY), payment(loan, hi, FORTNIGHTLY)],
        stress_monthly=payment(loan, hi + STRESS_ADD_PP, MONTHLY),
        stress_delta_monthly=payment(loan, hi + STRESS_ADD_PP, MONTHLY) - payment(loan, hi, MONTHLY),
        per_bank=per_bank,
    )


def _fhl_lender(bank, loan: float) -> FhlLender:
    special, standard = bank.special.get("1y"), (bank.standard or {}).get("1y")
    sa = bank.fhl.special_applies
    if sa is True and special is not None:
        rate, basis = special, "special"
    elif sa is False and standard is not None:
        rate, basis = standard, "standard"
    else:
        rate, basis = None, "unknown"  # special_applies unverified: do not guess
    full = round(loan * FHL_PREMIUM_PCT / 100, 2)
    pt = bank.fhl.lmi_passthrough_pct
    lmi = round(loan * pt / 100, 2) if pt is not None else None
    lender = FhlLender(
        bank_id=bank.bank_id, name=bank.name, captured_at=bank.captured_at,
        rate_pct=rate, rate_basis=basis,
        candidate_rates=CandidateRates(special=special, standard=standard),
        lmi_passthrough_pct=pt, lmi_amount=lmi, lmi_amount_if_full_premium=full,
    )
    if rate is not None:
        lender.monthly_if_full_premium = payment(loan + full, rate, MONTHLY)
        if lmi is not None:
            lender.loan_with_lmi = loan + lmi
            lender.monthly, lender.fortnightly, lender.stress_monthly = _pay_set(loan + lmi, rate)
            lender.stress_delta_monthly = lender.stress_monthly - lender.monthly
    return lender


def _range(vals: list[int]) -> Optional[list[int]]:
    return [min(vals), max(vals)] if vals else None


def first_home_loan(
    deposit: float, price: float, loan: float, ui: UserInputs, snap: RatesSnapshot,
    flags: list[Flag], warnings: list[str],
) -> Optional[FirstHomeLoanScenario]:
    src = ["kainga_ora_fhl_brochure"]  # eligibility rules (the premium has its own source)
    income = ui.household_income
    if income is None:
        warnings.append("fhl_skipped_income_missing")
        return None
    ok, conditional = True, False
    if deposit / price * 100 < FHL_MIN_DEPOSIT_PCT:
        flags.append(Flag(id="fhl_deposit_below_5", source_ids=src))
        ok = False
    if income > FHL_CAP_MULTIPLE_OR_DEPENDANTS:
        flags.append(Flag(id="fhl_income_over_cap", source_ids=src))
        ok = False
    elif income > FHL_CAP_SINGLE:
        if ui.household_type == "single":
            flags.append(Flag(id="fhl_income_over_cap", source_ids=src))
            ok = False
        elif ui.household_type is None:
            flags.append(Flag(id="fhl_household_type_unknown", source_ids=src))
            conditional = True
    if not ok:
        return None
    lenders = [_fhl_lender(b, loan) for b in snap.banks if b.fhl.lender]
    if not lenders:
        warnings.append("fhl_no_lenders")
        return None
    known = [x for x in lenders if x.rate_pct is not None]
    # lenders with an unknown pass-through use the full premium in the ranges
    m, f, s, assumed = [], [], [], []
    for x in known:
        if x.lmi_amount is None:
            assumed.append(x.bank_id)
        lmi = x.lmi_amount if x.lmi_amount is not None else x.lmi_amount_if_full_premium
        mm, ff, ss = _pay_set(loan + lmi, x.rate_pct)
        m.append(mm); f.append(ff); s.append(ss)
    return FirstHomeLoanScenario(
        eligibility="conditional" if conditional else "eligible",
        eligibility_note=FHL_CONDITIONAL_NOTE if conditional else None,
        income_caps=dict(FHL_INCOME_CAPS),
        lmi_amount=round(loan * FHL_PREMIUM_PCT / 100, 2),
        lmi_note=FHL_LMI_NOTE,
        lenders=lenders,
        monthly_range=_range(m), fortnightly_range=_range(f), stress_monthly_range=_range(s),
        full_premium_assumed_for=assumed,
    )


def compute_with_raw(
    ctx: PropertyContext, ui: UserInputs, snap: Optional[RatesSnapshot],
    now: Optional[datetime] = None,
) -> tuple[RulesOutput, Raw, Normalized]:
    now = (now or datetime.now(timezone.utc)).astimezone(NZ_TZ)  # NZ time for outward timestamps (team decision 10/04)
    warnings: list[str] = []
    flags: list[Flag] = []
    raw = Raw()
    norm = normalize(ctx, ui, warnings)
    out = RulesOutput(
        generated_at=now.isoformat(timespec="seconds"),
        price_used=resolve_price(norm, ui),
        rates_snapshot_date=snapshot_date(snap),
        deposit=ui.deposit,
        stress_add_pp=STRESS_ADD_PP,
        loan_term_years=TERM_YEARS,
        rates_stale_after_days=STALE_DAYS,
        default_rate_pct=default_rate_pct(snap),
        fhl_income_caps=dict(FHL_INCOME_CAPS),
    )
    if out.rates_snapshot_date:
        age = now.astimezone(NZ_TZ).date() - datetime.fromisoformat(out.rates_snapshot_date).date()
        if age > timedelta(days=STALE_DAYS):
            warnings.append("rates_snapshot_stale")
    income, debts = ui.household_income, ui.existing_debts or 0.0
    if ui.existing_debts is None:
        warnings.append("existing_debts_assumed_zero")
    if income is not None:
        out.high_dti_threshold = round(6 * income - debts, 2)

    price = out.price_used.value
    if price is None:
        warnings.append("price_missing")
    elif ui.deposit is None:
        warnings.append("deposit_missing")
    else:
        loan = max(price - ui.deposit, 0.0)
        if ui.deposit > price:
            warnings.append("deposit_exceeds_price")
        elif loan == 0:
            warnings.append("cash_purchase_no_loan")
        out.loan_amount = round(loan, 2)
        lvr = loan / price * 100
        if loan > 0:
            raw.lvr = lvr
            out.lvr_pct = round(lvr, 2)
            if lvr > 80:
                flags.append(Flag(id="lvr_over_80", source_ids=["rbnz_lvr"]))
        # No loan: LVR and DTI stay null (both limits apply to new lending; the warning above says why).
        if income is not None:
            if loan > 0:
                dti = raw.dti = (debts + loan) / income
                out.dti = round(dti, 2)
                if dti > 6:
                    flags.append(Flag(id="dti_over_6", source_ids=["rbnz_dti"]))
        else:
            warnings.append("income_missing")
        if loan > 0:
            scenarios = []
            if snap is not None:
                scenarios.append(bank_standard(loan, lvr, snap, warnings, ui.rate_pct))
            if ui.rate_pct is not None:
                m, f, s = _pay_set(loan, ui.rate_pct)
                scenarios.append(UserRateScenario(
                    rate_pct=ui.rate_pct, monthly=m, fortnightly=f, stress_monthly=s,
                    stress_delta_monthly=s - m))
            if snap is not None and lvr > 80:
                scenarios.append(first_home_loan(ui.deposit, price, loan, ui, snap, flags, warnings))
            out.scenarios = [x for x in scenarios if x is not None]
            raw.fhl_eligible = any(x.id == "first_home_loan" for x in out.scenarios)
    if snap is None:
        warnings.append("rates_unavailable")
    out.hazard_gaps = norm.hazard_gaps
    out.flags = flags
    out.warnings = warnings
    return out, raw, norm


def compute(ctx: PropertyContext, ui: UserInputs, snap: Optional[RatesSnapshot], now: Optional[datetime] = None) -> RulesOutput:
    return compute_with_raw(ctx, ui, snap, now)[0]
