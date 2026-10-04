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


# ================= property-api integration =================
import subprocess
import sys

from app.calc import parse_year

FIXTURE = ROOT / "services/property-api/fixtures/property_context_740_sandringham_road_extension.json"


def found(v, **kw):
    return {"value": v, "status": "found", **kw}


def yb(lo, hi, value=None):
    return {"property": {"year_built": found(value if value is not None else lo, decade_range=[lo, hi])}}


@pytest.mark.parametrize("rng,expect", [
    ([1980, 1989], True), ([2000, 2009], True), ([1970, 1979], False), ([2010, 2019], False),
])
def test_decade_range_overlap_with_rule_1987_2006(rng, expect):
    n = calc.normalize(PropertyContext.model_validate(yb(*rng)), UserInputs(), [])
    assert matches({"year_built_between": [1987, 2006]}, SelectContext(year_range=n.year_range)) is expect


def test_decade_range_open_lower_bound():
    n = calc.normalize(PropertyContext.model_validate(yb(None, 1913, value=1913)), UserInputs(), [])
    assert matches({"year_built_between": [1850, 1900]}, SelectContext(year_range=n.year_range))
    assert not matches({"year_built_between": [1920, 1930]}, SelectContext(year_range=n.year_range))


def test_decade_range_null_is_no_decade_and_chat_used():
    ctx = PropertyContext.model_validate({"property": {"year_built": {
        "value": 1980, "status": "found", "decade_range": None}}})
    w: list[str] = []
    n = calc.normalize(ctx, UserInputs(chat_supplied={"year_built": 1995}), w)
    assert n.year_range == (1995, 1995)
    assert calc.normalize(ctx, UserInputs(), []).year_range is None


def test_no_decade_range_key_uses_value():
    ctx = PropertyContext.model_validate({"property": {"year_built": found(1985)}})
    assert calc.normalize(ctx, UserInputs(), []).year_range == (1985, 1985)


def test_bad_decade_range_unparseable():
    w: list[str] = []
    ctx = PropertyContext.model_validate({"property": {"year_built": found(1980, decade_range=[1980])}})
    assert calc.normalize(ctx, UserInputs(), w).year_range is None and "year_built_unparseable" in w


@pytest.mark.parametrize("bad", ["²", "9" * 5000, "abc", "", 5000, -3, 10**30, "١٩٩٠"])
def test_parse_year_never_raises(bad):
    assert parse_year(bad) is None


def test_year_garbage_through_api_is_not_500():
    c = TestClient(app, raise_server_exceptions=False)
    for v in ["²", "9" * 5000, 10**30]:
        r = c.post("/v1/rules", json={"property_context": {"property": {"year_built": found(v)}},
                                      "user_inputs": {"chat_supplied": {"year_built": v}}})
        assert r.status_code == 200, v


def test_cv_over_cap_is_unavailable_not_422():
    ctx = {"property": {"cv": found(2e10)}}
    out = run(ctx, deposit=1)
    assert out.price_used.basis == "none" and "cv_invalid" in out.warnings
    r = TestClient(app).post("/v1/rules", json={"property_context": ctx})
    assert r.status_code == 200


def test_null_cv_uses_override_else_price_missing():
    ctx = {"property": {"cv": {"value": None, "status": "not_found"}}}
    out = run(ctx, price_override=900000, deposit=100000)
    assert out.price_used.basis == "user" and out.lvr_pct == 88.89
    out = run(ctx, deposit=100000)
    assert "price_missing" in out.warnings and out.lvr_pct is None
    ctx2 = {"property": {"cv": {"value": None, "status": "found"}}}
    assert run(ctx2, deposit=1).price_used.basis == "none"


def test_title_type_unresolved_does_not_guess():
    ctx = {"property": {"title_type": {"value": None, "status": "found",
                                       "linz_types": ["Freehold", "Cross Lease"]}}}
    w: list[str] = []
    n = calc.normalize(PropertyContext.model_validate(ctx), UserInputs(), w)
    assert n.title_type is None and "title_type_unresolved" in w
    assert "title_type_unresolved" not in run({"property": {"title_type": found("Freehold", linz_types=["Freehold"])}}).warnings


def test_extra_fields_and_null_annual_rates_ignored():
    ctx = {"property": {"annual_rates": {"status": "not_found", "value": None},
                        "land_value": {"value": 1, "status": "found"}, "local_board": {"value": "x"},
                        "cv": found(1e6)},
           "links": [{"id": "a", "label": "l", "url": "u", "kind": "site_search"}],
           "address": {"matched": "x"}, "parcel": {}, "sources": [{"id": "a"}]}
    r = TestClient(app).post("/v1/rules", json={"property_context": ctx, "user_inputs": {"deposit": 1}})
    assert r.status_code == 200


def test_chat_cladding_triggers_cladding_item_and_is_attributed(tmp_path, monkeypatch):
    seed = tmp_path / "kb" / "seed"
    seed.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (seed / "a.json").write_text(json.dumps([hashed(dict(
        id="clad", topic="cladding", claim_en="c", source_id="s", status="verified",
        applies_when={"cladding_in": ["monolithic", "plaster"]}))]))
    monkeypatch.setattr("app.main.get_repo", lambda: LocalRepo(tmp_path))
    ctx = {"property": {"cladding": {"value": None, "status": "unavailable", "reason": "no_free_source"}}}
    out = TestClient(app).post("/v1/rules", json={
        "property_context": ctx, "user_inputs": {"chat_supplied": {"cladding": "Plaster"}}}).json()
    assert out["kb_items"] == ["clad"] and out["kb_items_from_user_input"] == ["clad"]


def test_end_to_end_with_property_api_fixture(monkeypatch):
    monkeypatch.setenv("INCLUDE_DRAFT", "true")
    body = {"property_context": json.loads(FIXTURE.read_text()),
            "user_inputs": {"deposit": 40000, "household_income": 120000}}
    r = TestClient(app).post("/v1/rules", json=body)
    assert r.status_code == 200
    out = r.json()
    assert out["price_used"] == {"value": 340000.0, "basis": "cv", "valuation_date": "2024-05-01"}
    assert {"kb_flood_insurance_limit", "kb_title_freehold"} <= set(out["kb_items"])
    assert "year_built_unavailable" in out["warnings"]


# ---- property-api models stay compatible with b_common.schemas ----
def test_property_api_models_validate_their_fixtures():
    code = (
        "import json,sys,glob;"
        "from app.models import PropertyContext;"
        "fs=sorted(glob.glob('fixtures/*.json')); assert fs;"
        "[PropertyContext.model_validate(json.load(open(f))) for f in fs];"
        "print('ok', len(fs))"
    )
    env = {"PYTHONPATH": str(ROOT / "shared") + ":" + str(ROOT / "services/property-api"),
           "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "services/property-api",
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.startswith("ok"), r.stderr[-800:]


def test_openapi_declares_error_shape():
    spec = app.openapi()
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    for path, ops in spec["paths"].items():
        for op in ops.values():
            for code in ("405", "500"):
                ref = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/ErrorResponse"), (path, code)
    post = spec["paths"]["/v1/rules"]["post"]["responses"]
    assert post["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert spec["paths"]["/v1/rates/latest"]["get"]["responses"]["404"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert "decade_range" in json.dumps(spec["components"]["schemas"]["YearField"])
    assert set(spec["components"]["schemas"]["ErrorBody"]["properties"]) == {"code", "message", "fields"}
