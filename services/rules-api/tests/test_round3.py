import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import calc
from app.main import app
from app.repo import LocalRepo
from app.select import SelectContext, matches
from b_common.schemas import DISCLAIMER, PropertyContext, RulesOutput, UserInputs
from kbhelpers import hashed

ROOT = Path(__file__).resolve().parents[3]
SNAP = LocalRepo(ROOT).rates()
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def run(ctx=None, snap=SNAP, now=NOW, **ui):
    return calc.compute(PropertyContext.model_validate(ctx or {}), UserInputs(**ui), snap, now=now)


def sc(out, id):
    return next((s for s in out.scenarios if s.id == id), None)


# ---- hazards ----
def test_hazards_partial_keeps_reason():
    ctx = {"hazards": [
        {"layer": "flood", "priority": "primary", "status": "none"},
        {"layer": "coastal", "priority": "primary", "status": "unavailable", "reason": "service_timeout"},
        {"layer": "slip", "priority": "primary", "status": "not_found", "reason": "no_record"},
        {"layer": "minor", "priority": "secondary", "status": "unavailable"},
    ]}
    out = run(ctx)
    assert "hazards_partial:coastal" in out.warnings and "hazards_partial:slip" in out.warnings
    assert "hazards_partial:minor" not in out.warnings
    assert "hazards_unavailable" not in out.warnings
    gaps = {g.layer: (g.status, g.reason) for g in out.hazard_gaps}
    assert gaps == {"coastal": ("unavailable", "service_timeout"), "slip": ("not_found", "no_record")}


def test_hazard_null_status_priority_no_422():
    body = {"property_context": {"hazards": [{"layer": "x", "priority": None, "status": None}]}}
    r = TestClient(app).post("/v1/rules", json=body)
    assert r.status_code == 200
    w = r.json()["warnings"]
    assert "hazard_status_missing:x" in w and "hazard_priority_unknown:x" in w
    out = run({"hazards": [{"layer": "x", "priority": "weird", "status": "intersects"}]})
    assert "hazard_priority_unknown:x" in out.warnings
    # unknown priority becomes secondary, so it cannot trigger the primary-only selector
    n = calc.normalize(PropertyContext.model_validate(
        {"hazards": [{"layer": "x", "priority": None, "status": "intersects"}]}), UserInputs(), [])
    assert not matches({"hazard_status": "intersects"}, SelectContext(hazards=n.hazards))


# ---- rounding ----
def test_lvr_two_decimals_and_spec_example():
    assert run(price_override=1e6, deposit=120000).lvr_pct == 88.0
    assert run(price_override=1e6, deposit=199_600).lvr_pct == 80.04
    assert run(price_override=3, deposit=1).lvr_pct == 66.67


# ---- caps ----
@pytest.mark.parametrize("field,val", [
    ("price_override", 1e10 + 1), ("deposit", 1e11), ("household_income", 1e12),
    ("existing_debts", 1e10 + 1), ("rate_pct", 30.01), ("rate_pct", 100),
])
def test_upper_caps(field, val):
    with pytest.raises(ValidationError):
        UserInputs(**{field: val})


def test_caps_boundaries_ok():
    UserInputs(price_override=1e10, deposit=1e10, rate_pct=30)


def test_cash_purchase_warning():
    out = run(price_override=500000, deposit=500000)
    assert "cash_purchase_no_loan" in out.warnings and out.loan_amount == 0 and out.scenarios == []


# ---- NZ date for staleness ----
def test_stale_uses_nz_date():
    # snapshot oldest date is 2026-09-27 (see seed); day 30 is not stale, day 31 is
    old = calc.snapshot_date(SNAP)
    base = datetime.fromisoformat(old).date()
    # 2026-10-27 11:30 UTC is already 2026-10-28 in NZ (NZDT, +13): age 31 -> stale
    ny = datetime(base.year, base.month, base.day, 11, 30, tzinfo=timezone.utc)
    from datetime import timedelta
    edge = ny + timedelta(days=30)
    assert "rates_snapshot_stale" in run(now=edge).warnings
    before = ny + timedelta(days=30) - timedelta(hours=2)  # still day 30 in NZ
    assert "rates_snapshot_stale" not in run(now=before).warnings


# ---- FHL extras ----
def fhl(**ui):
    base = dict(price_override=1_000_000, deposit=120_000, household_income=90_000)
    return sc(run(**{**base, **ui}), "first_home_loan")


def test_fhl_top_level_nulls_and_docs():
    s = fhl()
    assert s.monthly is None and s.stress_monthly is None and s.stress_delta_monthly is None
    schema = RulesOutput.model_json_schema()
    props = schema["$defs"]["FirstHomeLoanScenario"]["properties"]
    assert "lenders[]" in props["monthly"]["description"]
    assert "monthly_range" in props["monthly"]["description"]
    assert "CODES.md" in schema["properties"]["warnings"]["description"]
    assert "CODES.md" in schema["properties"]["flags"]["description"]


def test_fhl_full_premium_assumed_list():
    s = fhl()
    by = {x.bank_id: x for x in s.lenders}
    expected = [x.bank_id for x in s.lenders if x.rate_pct is not None and x.lmi_amount is None]
    assert s.full_premium_assumed_for == expected
    assert all(by[b].lmi_passthrough_pct is None for b in expected)


def test_fhl_lender_stress_delta():
    for x in fhl().lenders:
        if x.monthly is not None:
            assert x.stress_delta_monthly == x.stress_monthly - x.monthly > 0
        else:
            assert x.stress_delta_monthly is None


def test_stress_delta_other_scenarios():
    out = run(price_override=800000, deposit=160000, rate_pct=6.0)
    u = sc(out, "user_rate")
    assert u.stress_delta_monthly == u.stress_monthly - u.monthly > 0
    b = sc(out, "bank_standard")
    assert b.stress_delta_monthly == b.stress_monthly - b.monthly_range[1] > 0


def test_disclaimer():
    assert run().disclaimer == DISCLAIMER and DISCLAIMER.startswith("Information only")


# ---- selectors: case / whitespace ----
def test_selectors_case_and_space_insensitive():
    assert matches({"title_type": ["Cross Lease"]}, SelectContext(title_type="  cross   LEASE "))
    assert matches({"title_type": [" cross lease"]}, SelectContext(title_type="Cross Lease"))
    assert matches({"cladding_in": ["Monolithic"]}, SelectContext(cladding=" MONOLITHIC  "))
    assert not matches({"cladding_in": ["Monolithic"]}, SelectContext(cladding="mono lithic"))


# ---- verified needs reviewer ----
@pytest.fixture
def kbdir(tmp_path, monkeypatch):
    seed = tmp_path / "kb" / "seed"
    seed.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rates_seed.json").write_text((ROOT / "data" / "rates_seed.json").read_text())
    base = dict(topic="t", claim_en="c", source_id="s", status="verified", applies_when={"always": True})
    items = [hashed(dict(id="ok", **base)), hashed(dict(id="no_by", **base)), hashed(dict(id="no_at", **base))]
    del items[1]["verified_by"]
    del items[2]["verified_at"]
    (seed / "a.json").write_text(json.dumps(items))
    monkeypatch.setattr("app.main.get_repo", lambda: LocalRepo(tmp_path))
    monkeypatch.delenv("INCLUDE_DRAFT", raising=False)
    return tmp_path


def test_verified_requires_by_and_at(kbdir):
    out = TestClient(app).post("/v1/rules", json={}).json()
    assert out["kb_items"] == ["ok"]
    assert {"kb_verification_incomplete:no_by", "kb_verification_incomplete:no_at"} <= set(out["warnings"])


# ---- unified errors ----
def test_error_shape_422_404_500(kbdir, monkeypatch):
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/v1/rules", json={"user_inputs": {"rate_pct": 31}})
    e = r.json()["error"]
    assert r.status_code == 422 and e["code"] == "validation_error" and e["message"]
    assert e["fields"][0]["field"] == "user_inputs.rate_pct" and e["fields"][0]["message"]
    monkeypatch.setattr("app.main.get_repo", lambda: type("R", (), {"rates": lambda s: None})())
    r = c.get("/v1/rates/latest")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    monkeypatch.setattr("app.main.get_repo", lambda: type("R", (), {"sources": lambda s: 1 / 0})())
    r = c.get("/v1/sources")
    assert r.status_code == 500 and r.json() == {
        "error": {"code": "internal_error", "message": "Internal server error.", "fields": []}}


# ---- docs / pins ----
def test_codes_md_lists_every_code():
    text = (ROOT / "CODES.md").read_text()
    import re
    src = (ROOT / "services/rules-api/app/calc.py").read_text() + (ROOT / "services/rules-api/app/main.py").read_text()
    codes = set(re.findall(r'(?:warnings\.append\(f?"|Flag\(id=")([a-z0-9_]+)', src))
    codes |= {"kb_unavailable", "rates_unavailable"}
    missing = [c for c in codes if c not in text]
    assert not missing, missing


def test_requirements_pinned():
    lines = [l for l in (ROOT / "services/rules-api/requirements.txt").read_text().splitlines() if l.strip()]
    assert lines and all("==" in l for l in lines)
