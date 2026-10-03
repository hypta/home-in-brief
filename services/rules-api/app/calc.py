"""Deterministic pure-function calculations for the rules engine."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from b_common.schemas import (
    BankRate,
    BankStandardScenario,
    FirstHomeLoanScenario,
    Flag,
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
FHL_INCOME_CAP = {"single": 95000.0, "single_with_dependants": 150000.0, "multiple": 150000.0}
FHL_INCOME_CAP_DEFAULT = 150000.0
TERM_YEARS = 30
MONTHLY, FORTNIGHTLY = 12, 26


def payment(principal: float, rate_pct: float, periods_per_year: int, years: int = TERM_YEARS) -> int:
    """Level principal-and-interest payment per period, rounded to a whole dollar."""
    n = years * periods_per_year
    r = rate_pct / 100 / periods_per_year
    if r == 0:
        return round(principal / n)
    return round(principal * r / (1 - (1 + r) ** -n))


def _pay_set(loan: float, rate_pct: float) -> tuple[int, int, int]:
    return (
        payment(loan, rate_pct, MONTHLY),
        payment(loan, rate_pct, FORTNIGHTLY),
        payment(loan, rate_pct + STRESS_ADD_PP, MONTHLY),
    )


def resolve_price(ctx: PropertyContext, ui: UserInputs) -> PriceUsed:
    if ui.price_override is not None:
        return PriceUsed(value=ui.price_override, basis="user")
    if ctx.property.cv.value is not None:
        return PriceUsed(
            value=ctx.property.cv.value, basis="cv",
            valuation_date=ctx.property.cv.valuation_date,
        )
    return PriceUsed(value=None, basis="none")


def band_margin(bands, lvr: float) -> Optional[float]:
    # bands start at e.g. 80.01; tolerate the 0.01 gap for unrounded LVRs
    for b in bands:
        if b.lvr_from - 0.01 < lvr <= b.lvr_to:
            return b.margin_pct
    return None


def bank_standard(loan: float, lvr: float, snap: RatesSnapshot, warnings: list[str]) -> Optional[BankStandardScenario]:
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
        per_bank.append(BankRate(bank_id=bank.bank_id, rate_pct=rate, basis=basis))
    if not per_bank:
        return None
    lo, hi = min(b.rate_pct for b in per_bank), max(b.rate_pct for b in per_bank)
    return BankStandardScenario(
        rate_range_pct=[lo, hi],
        low_equity_margin_pct=[min(margins), max(margins)] if margins else None,
        monthly_range=[payment(loan, lo, MONTHLY), payment(loan, hi, MONTHLY)],
        fortnightly_range=[payment(loan, lo, FORTNIGHTLY), payment(loan, hi, FORTNIGHTLY)],
        stress_monthly=payment(loan, hi + STRESS_ADD_PP, MONTHLY),
        per_bank=per_bank,
    )


def first_home_loan(
    price: float, deposit: float, loan: float, ui: UserInputs, snap: RatesSnapshot,
    flags: list[Flag], warnings: list[str],
) -> Optional[FirstHomeLoanScenario]:
    if ui.household_income is None:
        warnings.append("fhl_skipped_income_missing")
        return None
    if ui.household_type is None:
        flags.append(Flag(id="fhl_household_type_unknown", source_ids=["kainga_ora_fhl"]))
        cap = FHL_INCOME_CAP_DEFAULT
    else:
        cap = FHL_INCOME_CAP[ui.household_type]
    ok = True
    if deposit / price * 100 < FHL_MIN_DEPOSIT_PCT:
        flags.append(Flag(id="fhl_deposit_below_5", source_ids=["kainga_ora_fhl"]))
        ok = False
    if ui.household_income > cap:
        flags.append(Flag(id="fhl_income_over_cap", source_ids=["kainga_ora_fhl"]))
        ok = False
    if not ok:
        return None
    lenders = [b for b in snap.banks if b.fhl.lender and "1y" in b.special]
    if not lenders:
        warnings.append("fhl_no_lender_rate")
        return None
    best = min(lenders, key=lambda b: b.special["1y"])
    rate = best.special["1y"]
    lmi = round(loan * FHL_PREMIUM_PCT / 100, 2)
    total = loan + lmi
    return FirstHomeLoanScenario(
        rate_pct=rate, lender_bank_id=best.bank_id, lmi_amount=lmi, loan_with_lmi=total,
        monthly=payment(total, rate, MONTHLY), fortnightly=payment(total, rate, FORTNIGHTLY),
    )


def compute(ctx: PropertyContext, ui: UserInputs, snap: Optional[RatesSnapshot]) -> RulesOutput:
    warnings: list[str] = []
    flags: list[Flag] = []
    out = RulesOutput(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        price_used=resolve_price(ctx, ui),
        rates_snapshot_date=(snap.captured_at or "")[:10] or None if snap else None,
        deposit=ui.deposit,
        stress_add_pp=STRESS_ADD_PP,
    )
    income, debts = ui.household_income, ui.existing_debts or 0.0
    if income is not None and income > 0:
        out.high_dti_threshold = round(6 * income - debts, 2)

    price = out.price_used.value
    if price is None or price <= 0:
        warnings.append("price_missing")
    elif ui.deposit is None:
        warnings.append("deposit_missing")
    else:
        loan = price - ui.deposit
        lvr = loan / price * 100
        out.loan_amount = round(loan, 2)
        out.lvr_pct = round(lvr, 1)
        if lvr > 80:
            flags.append(Flag(id="lvr_over_80", source_ids=["rbnz_lvr"]))
        if income is not None and income > 0:
            dti = (debts + loan) / income
            out.dti = round(dti, 2)
            if dti > 6:
                flags.append(Flag(id="dti_over_6", source_ids=["rbnz_dti"]))
        else:
            warnings.append("income_missing")
        if loan > 0 and snap is not None:
            scenarios = [bank_standard(loan, lvr, snap, warnings)]
            if ui.rate_pct is not None:
                m, f, s = _pay_set(loan, ui.rate_pct)
                scenarios.append(UserRateScenario(
                    rate_pct=ui.rate_pct, monthly=m, fortnightly=f, stress_monthly=s
                ))
            scenarios.append(first_home_loan(price, ui.deposit, loan, ui, snap, flags, warnings))
            out.scenarios = [x for x in scenarios if x is not None]
        elif snap is None:
            warnings.append("rates_unavailable")
    out.flags = flags
    out.warnings = warnings
    return out
