import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kbhelpers import hashed
from app import calc
from app.main import app
from app.repo import LocalRepo
from app.select import SelectContext, matches
from b_common.schemas import PropertyContext, UserInputs

ROOT = Path(__file__).resolve().parents[3]
SNAP = LocalRepo(ROOT).rates()
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def found(v, **kw):
    return {"value": v, "status": "found", **kw}


def run(ctx=None, snap=SNAP, **ui):
    return calc.compute(PropertyContext.model_validate(ctx or {}), UserInputs(**ui), snap, now=NOW)


def sc(out, id):
    return next((s for s in out.scenarios if s.id == id), None)


# ---- 1. input validation ----
@pytest.mark.parametrize("field,val", [
    ("price_override", 0), ("price_override", -5), ("price_override", float("nan")),
    ("price_override", float("inf")), ("deposit", -1), ("deposit", float("inf")),
    ("existing_debts", -1), ("household_income", 0), ("household_income", float("nan")),
    ("rate_pct", -1200), ("rate_pct", float("inf")), ("rate_pct", float("nan")),
])
def test_bad_amounts_rejected(field, val):
    with pytest.raises(ValidationError) as e:
        UserInputs(**{field: val})
    assert field in str(e.value)


def test_api_422_names_field():
    c = TestClient(app)
    r = c.post("/v1/rules", content=b'{"user_inputs": {"rate_pct": -1200}}',
               headers={"content-type": "application/json"})
    assert r.status_code == 422 and "rate_pct" in json.dumps(r.json()["error"]["fields"])
    r = c.post("/v1/rules", content=b'{"user_inputs": {"deposit": NaN}}',
               headers={"content-type": "application/json"})
    assert r.status_code == 422 and "deposit" in json.dumps(r.json()["error"]["fields"])


def test_api_deposit_over_price_422():
    r = TestClient(app).post("/v1/rules", json={"user_inputs": {"price_override": 500000, "deposit": 600000}})
    assert r.status_code == 422
    d = r.json()["error"]["fields"][0]
    assert d["field"] == "user_inputs.deposit" and "exceed" in d["message"]


def test_payment_numerically_stable():
    assert calc.payment(360000, 1e-15, 12) == 1000  # P/n
    assert calc.payment(360000, 0, 12) == 1000
    assert calc.payment(500000, 6.0, 12) == 2998
    for bad in (float("nan"), float("inf"), -1200):
        with pytest.raises(ValueError):
            calc.payment(100000, bad, 12)
    with pytest.raises(ValueError):
        calc.payment(float("nan"), 5, 12)


# ---- 2. unavailable is not "none" ----
def test_hazard_defaults_and_unknown_status():
    ctx = {"hazards": [{"layer": "a", "priority": "primary"},
                       {"layer": "b", "priority": "primary", "status": "weird"}]}
    out = run(ctx)
    assert "hazard_status_unknown:b" in out.warnings
    assert "hazards_unavailable" in out.warnings


def test_hazards_available_no_warning():
    ctx = {"hazards": [{"layer": "a", "priority": "primary", "status": "none"},
                       {"layer": "b", "priority": "primary", "status": "unavailable"}]}
    assert "hazards_unavailable" not in run(ctx).warnings


def test_hazard_unavailable_does_not_select_but_intersects_does():
    n = calc.normalize(PropertyContext.model_validate(
        {"hazards": [{"layer": "a", "priority": "primary", "status": "intersects"}]}), UserInputs(), [])
    assert matches({"hazard_status": "intersects"}, SelectContext(hazards=n.hazards))


def test_fields_used_only_when_found():
    ctx = {"property": {"cv": {"value": 900000, "status": "unavailable"},
                        "title_type": {"value": "Cross Lease", "status": "not_found"},
                        "year_built": {"value": 1990, "status": "unavailable"},
                        "cladding": {"value": "brick", "status": None}}}
    w: list[str] = []
    n = calc.normalize(PropertyContext.model_validate(ctx), UserInputs(), w)
    assert (n.cv_value, n.title_type, n.year_range, n.cladding) == (None, None, None, None)
    assert {"cv_unavailable", "title_type_unavailable", "year_built_unavailable", "cladding_unavailable"} <= set(w)
    assert run(ctx, deposit=1).price_used.basis == "none"


def test_year_decade_and_int():
    assert calc.parse_year("1990s") == (1990, 1999)
    assert calc.parse_year(1985) == (1985, 1985)
    assert calc.parse_year("soon") is None
    ctx = PropertyContext.model_validate({"property": {"year_built": found("1990s")}})
    n = calc.normalize(ctx, UserInputs(), [])
    assert matches({"year_built_between": [1995, 2005]}, SelectContext(year_range=n.year_range))
    assert not matches({"year_built_between": [2000, 2005]}, SelectContext(year_range=n.year_range))
    assert matches({"year_built_between": [1980, 1990]}, SelectContext(year_range=(1990, 1999)))


# ---- 3. rounding consistency ----
def test_lvr_80_04_uses_unrounded():
    out = run({"property": {"cv": found(1_000_000)}}, deposit=199_600)
    assert out.lvr_pct == 80.04
    assert "lvr_over_80" in {f.id for f in out.flags}
    _, raw, _ = calc.compute_with_raw(PropertyContext(), UserInputs(price_override=1e6, deposit=199_600), SNAP, NOW)
    assert raw.lvr > 80 and matches({"lvr_over": 80}, SelectContext(lvr_pct=raw.lvr))


# ---- 4/5. First Home Loan ----
def fhl(**ui):
    base = dict(price_override=1_000_000, deposit=120_000, household_income=90_000)
    return sc(run(**{**base, **ui}), "first_home_loan")


def test_fhl_lists_all_lenders():
    s = fhl()
    ids = {x.bank_id for x in s.lenders}
    assert ids == {b.bank_id for b in SNAP.banks if b.fhl.lender}
    assert s.lmi_amount == 10560
    assert s.income_caps == {"single": 95000, "multiple_or_dependants": 150000}


def test_fhl_unknown_values_are_null_not_defaulted():
    s = fhl()
    by = {x.bank_id: x for x in s.lenders}
    asb = by["asb"]  # special_applies null
    assert asb.rate_pct is None and asb.rate_basis == "unknown"
    assert asb.candidate_rates.special == 4.99
    assert asb.monthly is None and asb.monthly_if_full_premium is None
    kiwi = by["kiwibank"]  # special_applies true, passthrough 1.00
    assert kiwi.rate_basis == "special" and kiwi.rate_pct == 5.15
    assert kiwi.lmi_amount == 8800 and kiwi.loan_with_lmi == 888800
    assert kiwi.monthly == calc.payment(888800, 5.15, 12)
    assert kiwi.fortnightly == calc.payment(888800, 5.15, 26)
    assert kiwi.stress_monthly == calc.payment(888800, 7.15, 12)
    unity = by["unity"]  # passthrough null
    assert unity.lmi_passthrough_pct is None and unity.lmi_amount is None
    assert unity.lmi_amount_if_full_premium == 10560 and unity.monthly is None


def test_fhl_ranges_cover_known_rates_only():
    s = fhl()
    assert s.monthly_range and s.monthly_range[0] <= s.monthly_range[1]
    assert s.stress_monthly_range[0] > s.monthly_range[0]


@pytest.mark.parametrize("income,htype,expect", [
    (90_000, "single", "eligible"),
    (95_000, "single", "eligible"),
    (120_000, "single", None),
    (120_000, "multiple", "eligible"),
    (120_000, "single_with_dependants", "eligible"),
    (120_000, None, "conditional"),
    (150_000, "multiple", "eligible"),
    (150_001, "multiple", None),
    (150_001, None, None),
    (90_000, None, "eligible"),
])
def test_fhl_income_caps(income, htype, expect):
    s = fhl(household_income=income, household_type=htype)
    if expect is None:
        assert s is None
    else:
        assert s.eligibility == expect
        assert (s.eligibility_note == calc.FHL_CONDITIONAL_NOTE) == (expect == "conditional")


def test_fhl_conditional_still_selects_fhl_eligible():
    out, raw, _ = calc.compute_with_raw(
        PropertyContext(), UserInputs(price_override=1e6, deposit=120000, household_income=120000), SNAP, NOW)
    assert raw.fhl_eligible and sc(out, "first_home_loan").eligibility == "conditional"
    assert "fhl_household_type_unknown" in {f.id for f in out.flags}


# ---- 6. misc ----
def test_bank_standard_has_rate_pct():
    out = run(price_override=1e6, deposit=120000, rate_pct=6.5)
    assert sc(out, "bank_standard").rate_pct == 6.5
    assert run(price_override=1e6, deposit=120000).scenarios[0].rate_pct is None


def test_user_rate_without_snapshot():
    out = run(snap=None, price_override=800000, deposit=160000, rate_pct=6.0)
    assert [s.id for s in out.scenarios] == ["user_rate"]
    assert "rates_unavailable" in out.warnings


def test_stale_snapshot_warning():
    assert "rates_snapshot_stale" not in run().warnings  # 7 days old
    late = datetime(2026, 11, 1, tzinfo=timezone.utc)
    out = calc.compute(PropertyContext(), UserInputs(), SNAP, now=late)
    assert "rates_snapshot_stale" in out.warnings
    early = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert "rates_snapshot_stale" not in calc.compute(PropertyContext(), UserInputs(), SNAP, now=early).warnings


# ---- API-level: KB provenance, empty reasons, failures, sources ----
class BoomRepo(LocalRepo):
    def kb_items(self):
        raise RuntimeError("boom")

    def rates(self):
        raise RuntimeError("boom")


@pytest.fixture
def kbdir(tmp_path, monkeypatch):
    seed = tmp_path / "kb" / "seed"
    seed.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rates_seed.json").write_text((ROOT / "data" / "rates_seed.json").read_text())
    base = dict(topic="t", claim_en="c", source_id="s", status="verified")
    (seed / "a.json").write_text(json.dumps([hashed(d) for d in [
        dict(id="clad", applies_when={"cladding_in": ["monolithic"]}, **base),
        dict(id="year", applies_when={"year_built_between": [1990, 2000]}, **base),
        dict(id="never", applies_when={"title_type": ["zzz"]}, **base),
    ]]))
    (seed / "sources.json").write_text(json.dumps([dict(
        id="s", title="S", url="http://x", licence="CC BY 4.0", attribution="Source: S",
        tos_status="allowed", tos_url="http://t", tos_checked_at="2026-10-03")]))
    monkeypatch.setattr("app.main.get_repo", lambda: LocalRepo(tmp_path))
    monkeypatch.delenv("INCLUDE_DRAFT", raising=False)
    return tmp_path


def post(body):
    return TestClient(app).post("/v1/rules", json=body).json()


def test_kb_items_from_user_input(kbdir):
    out = post({"user_inputs": {"chat_supplied": {"year_built": 1995, "cladding": "Monolithic"}}})
    assert set(out["kb_items"]) == {"clad", "year"}
    assert set(out["kb_items_from_user_input"]) == {"clad", "year"}
    out = post({"property_context": {"property": {"year_built": found(1995), "cladding": found("monolithic")}}})
    assert out["kb_items_from_user_input"] == []
    assert set(out["kb_items"]) == {"clad", "year"}


def test_kb_empty_reasons(kbdir, monkeypatch):
    assert post({})["kb_empty_reason"] == "no_matching_items"
    assert post({"property_context": {"property": {"title_type": found("Freehold")}}})["kb_empty_reason"] == "no_matching_items"
    (kbdir / "kb" / "seed" / "a.json").write_text("[]")
    assert post({})["kb_empty_reason"] == "no_verified_items"
    monkeypatch.setattr("app.main.get_repo", lambda: BoomRepo(kbdir))
    r = TestClient(app).post("/v1/rules", json={"user_inputs": {"price_override": 1e6, "deposit": 2e5, "rate_pct": 6}})
    assert r.status_code == 200
    out = r.json()
    assert out["kb_empty_reason"] == "kb_unavailable" and out["kb_items"] == []
    assert "kb_unavailable" in out["warnings"] and "rates_unavailable" in out["warnings"]
    assert [s["id"] for s in out["scenarios"]] == ["user_rate"]


def test_sources_keep_attribution_and_tos(kbdir):
    src = TestClient(app).get("/v1/sources").json()[0]
    assert src["attribution"] == "Source: S" and src["tos_status"] == "allowed"
    assert src["licence"] == "CC BY 4.0" and src["tos_checked_at"] == "2026-10-03"


def test_real_seed_sources_keep_fields():
    srcs = LocalRepo(ROOT).sources()
    assert srcs and all(s.tos_status for s in srcs) and all(s.attribution for s in srcs)


def test_dockerfile_copies_kb():
    assert "COPY kb/" in (ROOT / "services" / "rules-api" / "Dockerfile").read_text()


# ---- FHL source ids ----
def test_fhl_flags_use_brochure_source():
    out = run(price_override=1_000_000, deposit=20_000, household_income=200_000, household_type="single")
    flags = {f.id: f.source_ids for f in out.flags}
    assert flags["fhl_deposit_below_5"] == ["kainga_ora_fhl_brochure"]
    assert flags["fhl_income_over_cap"] == ["kainga_ora_fhl_brochure"]
    s = sc(run(price_override=1e6, deposit=120000, household_income=90000), "first_home_loan")
    assert s.source_ids == ["kainga_ora_fhl_brochure", "kainga_ora_fhl"]


# ---- KB hash gate ----
def test_hash_mismatch_item_not_served(kbdir):
    f = kbdir / "kb" / "seed" / "a.json"
    items = json.loads(f.read_text())
    items[0]["claim_en"] = "edited after review"
    items[1].pop("verified_content_hash")
    f.write_text(json.dumps(items))
    out = post({"user_inputs": {"chat_supplied": {"year_built": 1995, "cladding": "Monolithic"}}})
    assert out["kb_items"] == []
    assert {"kb_hash_mismatch:clad", "kb_hash_mismatch:year"} <= set(out["warnings"])
    assert [i["id"] for i in TestClient(app).get("/v1/kb/items").json()] == ["never"]


def test_hash_matches_kb_module_on_every_seed_item():
    import importlib.util

    spec = importlib.util.spec_from_file_location("kb_content_hash", ROOT / "kb" / "content_hash.py")
    kb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kb)
    from b_common.content_hash import content_hash

    n = 0
    for p in (ROOT / "kb" / "seed").glob("*.json"):
        if p.name == "sources.json":
            continue
        for raw in json.loads(p.read_text()):
            assert content_hash(raw) == kb.content_hash(raw), raw["id"]
            n += 1
    assert n > 0
