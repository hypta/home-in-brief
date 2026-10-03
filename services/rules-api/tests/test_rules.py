import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import calc
from app.main import app
from app.repo import LocalRepo
from app.select import SelectContext, matches
from b_common.schemas import PropertyContext, RulesOutput, UserInputs

ROOT = Path(__file__).resolve().parents[3]
SNAP = LocalRepo(ROOT).rates()


def sc(out, id):
    return next((s for s in out.scenarios if s.id == id), None)


def run(ctx=None, **ui):
    return calc.compute(PropertyContext.model_validate(ctx or {}), UserInputs(**ui), SNAP)


def test_rbnz_example_1_dti_threshold():
    out = run(household_income=135000, existing_debts=27000)
    assert out.high_dti_threshold == 783000


def test_rbnz_example_2_lvr():
    out = run(price_override=800000, deposit=160000)
    assert out.lvr_pct == 80.0
    assert "lvr_over_80" not in {f.id for f in out.flags}


def test_team_spec_example():
    ctx = {"property": {"cv": {"value": 1_000_000, "valuation_date": "2024-05-01"}}}
    out = run(ctx, deposit=120000, household_income=150000, existing_debts=30000,
              household_type="multiple")
    assert out.price_used.basis == "cv" and out.price_used.valuation_date == "2024-05-01"
    assert out.rates_snapshot_date == "2026-09-27"  # oldest per-bank captured_at
    assert out.loan_amount == 880000
    assert out.lvr_pct == 88.0
    assert out.dti == 6.07
    assert out.high_dti_threshold == 870000
    assert sc(out, 'first_home_loan').lmi_amount == 10560
    flags = {f.id: f.source_ids for f in out.flags}
    assert flags == {"lvr_over_80": ["rbnz_lvr"], "dti_over_6": ["rbnz_dti"]}
    assert [s.id for s in out.scenarios] == ["bank_standard", "first_home_loan"]


def test_payment_known_value():
    # 500k @ 6% monthly, 30y -> 2997.75
    assert calc.payment(500000, 6.0, 12) == 2998


def test_price_missing_no_calc():
    out = run(deposit=100000, household_income=100000)
    assert out.price_used.basis == "none" and out.price_used.value is None
    assert out.loan_amount is None and out.lvr_pct is None and out.scenarios == []
    assert out.price_used.valuation_date is None
    assert "price_missing" in out.warnings


def test_bank_standard_lvr_88():
    out = run(price_override=1_000_000, deposit=120000, household_income=150000)
    per = {b.bank_id: b for b in sc(out, 'bank_standard').per_bank}
    assert per["asb"].rate_pct == pytest.approx(5.74)
    assert per["anz"].rate_pct == pytest.approx(5.59)
    assert per["kiwibank"].rate_pct == pytest.approx(6.05)
    assert per["bnz"].rate_pct == pytest.approx(5.94)
    assert sc(out, 'bank_standard').low_equity_margin_pct == [0.75, 0.75]
    assert sc(out, 'bank_standard').stress_monthly > sc(out, 'bank_standard').monthly_range[1]


def test_bank_standard_lvr_le_80_uses_special_and_no_margin():
    out = run(price_override=1_000_000, deposit=200000)
    bs = sc(out, 'bank_standard')
    assert bs.low_equity_margin_pct is None
    assert bs.rate_range_pct == [4.99, 5.19]


def test_user_rate_scenario():
    out = run(price_override=800000, deposit=160000, rate_pct=6.0)
    assert out.price_used.basis == "user"
    assert sc(out, 'user_rate').monthly == calc.payment(640000, 6.0, 12)
    assert sc(out, 'user_rate').fortnightly == calc.payment(640000, 6.0, 26)


def test_fhl_ineligible_flags():
    out = run(price_override=1_000_000, deposit=20000, household_income=200000, household_type="single")
    ids = {f.id for f in out.flags}
    assert sc(out, 'first_home_loan') is None
    assert {"fhl_deposit_below_5", "fhl_income_over_cap"} <= ids


def test_fhl_household_unknown_flag():
    out = run(price_override=1_000_000, deposit=100000, household_income=120000)
    assert "fhl_household_type_unknown" in {f.id for f in out.flags}
    assert sc(out, 'first_home_loan').rate_pct == 4.99


# ---- applies_when ----
def C(**kw):
    return SelectContext(**kw)


CASES = [
    ({"title_type": ["Cross Lease"]}, C(title_type="Cross Lease"), C(title_type="Freehold")),
    ({"hazard_status": "intersects"},
     C(hazards=[{"layer": "flood", "priority": "primary", "status": "intersects"}]),
     C(hazards=[{"layer": "flood", "priority": "secondary", "status": "intersects"}])),
    ({"fhl_eligible": True}, C(fhl_eligible=True), C(fhl_eligible=False)),
    ({"lvr_over": 80}, C(lvr_pct=88.0), C(lvr_pct=80.0)),
    ({"dti_over": 6}, C(dti=6.07), C(dti=5.9)),
    ({"year_built_between": [1990, 2005]}, C(year_built=1990), C(year_built=2006)),
    ({"cladding_in": ["Monolithic"]}, C(cladding="monolithic"), C(cladding="brick")),
]


@pytest.mark.parametrize("rule,yes,no", CASES)
def test_applies_when(rule, yes, no):
    assert matches(rule, yes) is True
    assert matches(rule, no) is False


def test_always():
    assert matches({"always": True}, C()) is True
    assert matches({"always": False}, C()) is False


def test_unknown_key_not_selected(caplog):
    assert matches({"always": True, "bogus": 1}, C()) is False
    assert "bogus" in caplog.text


def test_and_semantics():
    assert matches({"lvr_over": 80, "dti_over": 6}, C(lvr_pct=90, dti=5)) is False


# ---- API ----
@pytest.fixture
def client(tmp_path, monkeypatch):
    seed = tmp_path / "kb" / "seed"
    seed.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rates_seed.json").write_text((ROOT / "data" / "rates_seed.json").read_text())
    base = dict(topic="t", claim_en="c", source_id="s")
    (seed / "a.json").write_text(json.dumps([
        dict(id="always_draft", applies_when={"always": True}, status="draft", **base),
        dict(id="always_ver", applies_when={"always": True}, status="verified", **base),
        dict(id="lvr_ver", applies_when={"lvr_over": 80}, status="verified", **base),
    ]))
    (seed / "sources.json").write_text(json.dumps(
        [dict(id="s", title="S", url="http://x", source_type="official")]))
    monkeypatch.setattr("app.repo.ROOT", tmp_path)
    monkeypatch.setattr("app.main.get_repo", lambda: LocalRepo(tmp_path))
    monkeypatch.delenv("INCLUDE_DRAFT", raising=False)
    return TestClient(app)


BODY = {"property_context": {"property": {"cv": {"value": 1000000}}, "extra": 1},
        "user_inputs": {"deposit": 120000, "household_income": 150000, "existing_debts": 30000}}


def test_api_rules_ok_and_shape(client):
    r = client.post("/v1/rules", json=BODY)
    assert r.status_code == 200
    out = RulesOutput.model_validate(r.json())
    assert out.lvr_pct == 88.0
    assert out.kb_items == ["always_ver", "lvr_ver"]
    assert out.kb_includes_draft is False


def test_api_include_draft(client, monkeypatch):
    monkeypatch.setenv("INCLUDE_DRAFT", "true")
    out = client.post("/v1/rules", json=BODY).json()
    assert "always_draft" in out["kb_items"] and out["kb_includes_draft"] is True


def test_api_fhl_eligible_key(client, tmp_path):
    (tmp_path / "kb" / "seed" / "b.json").write_text(json.dumps([dict(
        id="fhl_k", topic="t", claim_en="c", source_id="s", status="verified",
        applies_when={"fhl_eligible": True})]))
    assert "fhl_k" not in client.post("/v1/rules", json={}).json()["kb_items"]
    body = {"property_context": BODY["property_context"],
            "user_inputs": {**BODY["user_inputs"], "household_type": "multiple"}}
    assert "fhl_k" in client.post("/v1/rules", json=body).json()["kb_items"]


def test_snapshot_date_is_oldest_bank():
    assert calc.snapshot_date(SNAP) == "2026-09-27"
    assert all(b.captured_at for b in SNAP.banks)


def test_api_empty_body_no_exception(client):
    r = client.post("/v1/rules", json={})
    assert r.status_code == 200 and r.json()["price_used"]["basis"] == "none"


def test_api_kb_items_rates_sources_health(client):
    assert client.get("/healthz").status_code == 200
    assert [i["id"] for i in client.get("/v1/kb/items?ids=lvr_ver,always_draft,nope").json()] == ["lvr_ver"]
    assert len(client.get("/v1/rates/latest").json()["banks"]) == len(SNAP.banks) >= 5
    assert client.get("/v1/sources").json()[0]["id"] == "s"


def test_local_repo_missing_files_empty(tmp_path):
    r = LocalRepo(tmp_path)
    assert r.kb_items() == [] and r.sources() == [] and r.rates() is None
