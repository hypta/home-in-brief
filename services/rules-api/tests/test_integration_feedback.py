"""Changes from the frontend integration feedback (response fields only added, none removed or renamed)."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import calc
from app.main import app
from test_review_fixes import SNAP, run, sc

client = TestClient(app)
FHL_KW = dict(price_override=1_000_000, deposit=120_000, household_income=150_000, household_type="multiple")


# ---- 1. lmi_note is plain English ----
def test_lmi_note_is_for_people():
    note = sc(run(**FHL_KW), "first_home_loan").lmi_note
    assert note and "null" not in note and not re.search(r"\w+_\w+", note)
    assert "1.2%" in note and "lender" in note


# ---- 2. money formatting ----
def test_money_format():
    assert calc.money(340000.0) == "$340,000"
    assert calc.money(1234567) == "$1,234,567"
    assert calc.money(0) == "$0"


def test_user_facing_text_has_no_float_amounts():
    assert "$150,000" in calc.FHL_CONDITIONAL_NOTE and "$95,000" in calc.FHL_CONDITIONAL_NOTE
    out = run(**FHL_KW)
    texts = [sc(out, "first_home_loan").lmi_note, calc.FHL_CONDITIONAL_NOTE]
    assert not any(re.search(r"\d\.0\b", t) for t in texts)


# ---- 3. existing_debts left blank ----
def test_existing_debts_assumed_zero_warning():
    assert "existing_debts_assumed_zero" in run(price_override=800000, deposit=160000, household_income=150000).warnings
    assert "existing_debts_assumed_zero" in run().warnings
    assert "existing_debts_assumed_zero" not in run(existing_debts=0, household_income=150000).warnings
    assert "existing_debts_assumed_zero" not in run(existing_debts=5000).warnings


# ---- 4. every note_id and code is documented ----
def test_codes_md_documents_note_ids():
    codes = (Path(calc.__file__).resolve().parents[3] / "CODES.md").read_text()
    for n in sc(run(**FHL_KW), "first_home_loan").note_ids:
        assert f"`{n}`" in codes, n
    for code in ("existing_debts_assumed_zero", "deposit_exceeds_price"):
        assert f"`{code}`" in codes, code


# ---- 5. new top-level fields ----
def test_new_top_level_fields():
    out = run(**FHL_KW)
    assert out.loan_term_years == calc.TERM_YEARS == 30
    assert out.rates_stale_after_days == calc.STALE_DAYS == 30
    assert out.fhl_income_caps == {"single": 95000, "multiple_or_dependants": 150000}
    assert out.fhl_income_caps == sc(out, "first_home_loan").income_caps
    assert run().fhl_income_caps == out.fhl_income_caps  # returned with no inputs at all


def test_default_rate_is_median_of_standard_1y():
    vals = sorted(b.standard["1y"] for b in SNAP.banks if b.standard and b.standard.get("1y") is not None)
    mid = len(vals) // 2
    expected = round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 2)
    assert run().default_rate_pct == expected == 5.79
    assert calc.default_rate_pct(None) is None
    assert run(snap=None).default_rate_pct is None


def test_default_rate_odd_and_missing():
    snap = SNAP.model_copy(deep=True)
    snap.banks = snap.banks[:3]
    for b, v in zip(snap.banks, (5.0, 7.0, 6.0)):
        b.standard = {"1y": v}
    assert calc.default_rate_pct(snap) == 6.0
    for b in snap.banks:
        b.standard = None
    assert calc.default_rate_pct(snap) is None


def test_fhl_caps_present_when_income_over_cap():
    out = run(price_override=1_000_000, deposit=120_000, household_income=200_000, household_type="multiple")
    assert any(f.id == "fhl_income_over_cap" for f in out.flags)
    assert sc(out, "first_home_loan") is None
    assert out.fhl_income_caps["multiple_or_dependants"] == 150000


def test_new_fields_in_api_and_openapi():
    r = client.post("/v1/rules", json={"user_inputs": FHL_KW}).json()
    for k in ("loan_term_years", "rates_stale_after_days", "default_rate_pct", "fhl_income_caps", "stress_add_pp"):
        assert k in r
    props = app.openapi()["components"]["schemas"]["RulesOutput"]["properties"]
    for k in ("loan_term_years", "rates_stale_after_days", "default_rate_pct", "fhl_income_caps"):
        assert props[k].get("description"), k


# ---- 6. deposit above the price ----
def test_deposit_exceeds_price_like_cash_purchase():
    out = run(price_override=500000, deposit=600000, household_income=100000, existing_debts=20000)
    assert out.loan_amount == 0 and out.lvr_pct == 0 and out.scenarios == []
    assert out.deposit == 600000
    assert "deposit_exceeds_price" in out.warnings and "cash_purchase_no_loan" not in out.warnings
    assert out.dti == 0.2  # existing debts only, no loan part
    assert not any(f.id in ("lvr_over_80", "dti_over_6") for f in out.flags)
    cash = run(price_override=500000, deposit=500000)
    assert "cash_purchase_no_loan" in cash.warnings and "deposit_exceeds_price" not in cash.warnings


def test_deposit_exceeds_price_still_selects_kb_items():
    r = client.post("/v1/rules", json={"user_inputs": {"price_override": 500000, "deposit": 600000}})
    assert r.status_code == 200 and "kb_items" in r.json()


def test_negative_deposit_still_422():
    r = client.post("/v1/rules", json={"user_inputs": {"price_override": 500000, "deposit": -1}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


# ---- 7. /health ----
def test_health_endpoints_match():
    a, b = client.get("/health"), client.get("/healthz")
    assert a.status_code == b.status_code == 200 and a.json() == b.json() == {"status": "ok"}
    assert "/health" in app.openapi()["paths"]
