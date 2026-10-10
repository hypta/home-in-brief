import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import calc
from app.main import app
from b_common.schemas import PropertyContext, UserInputs

ROOT = Path(__file__).resolve().parents[3]
client = TestClient(app)


def post(prop, chat=None):
    body = {"property_context": {"property": prop}, "user_inputs": {"deposit": 1, "chat_supplied": chat or {}}}
    r = client.post("/v1/rules", json=body)
    assert r.status_code == 200
    return r.json()["warnings"]


def title(v):
    return {"title_type": {"value": v, "status": "found"}}


def year(value=None, rng=None):
    d = {"value": value, "status": "found"}
    if rng is not None:
        d["decade_range"] = rng
    return {"year_built": d}


@pytest.mark.parametrize("v", ["Freehold", "Cross Lease", "Unit Title", "freehold "])
def test_title_type_mapped_no_warning(v):
    assert "title_type_unmapped" not in post(title(v))


def test_leasehold_unmapped():
    assert "title_type_unmapped" in post(title("Leasehold"))


def test_title_type_unresolved_not_unmapped():
    w = post({"title_type": {"value": None, "status": "found", "linz_types": ["Freehold", "Leasehold"]}})
    assert "title_type_unresolved" in w and "title_type_unmapped" not in w


def test_year_inside_decade_no_conflict():
    p = year(1975, [1970, 1979])
    assert "year_built_user_conflict" not in post(p, {"year_built": 1972})
    assert "year_built_user_conflict" not in post(p, {"year_built": "1970s"})
    assert "year_built_user_conflict" not in post(year(1975), {"year_built": 1975})


def test_year_outside_decade_conflicts():
    p = year(1975, [1970, 1979])
    assert "year_built_user_conflict" in post(p, {"year_built": 1999})
    assert "year_built_user_conflict" in post(p, {"year_built": "1990s"})
    assert "year_built_user_conflict" in post(year(1975), {"year_built": 1976})


def test_year_conflict_needs_both_sides():
    assert "year_built_user_conflict" not in post({"year_built": {"value": None, "status": "unavailable"}},
                                                  {"year_built": 1999})
    assert "year_built_user_conflict" not in post(year(1975, [1970, 1979]))


def test_year_property_still_wins():
    ctx = PropertyContext.model_validate({"property": year(1975, [1970, 1979])})
    n = calc.normalize(ctx, UserInputs(chat_supplied={"year_built": 1999}), [])
    assert n.year_range == (1970, 1979)


def test_cladding_conflict():
    p = {"cladding": {"value": "brick", "status": "found"}}
    assert "cladding_user_conflict" in post(p, {"cladding": "monolithic"})
    assert "cladding_user_conflict" in post(p, {"cladding": "Plaster"})
    assert "cladding_user_conflict" not in post(p, {"cladding": "Brick"})
    assert "cladding_user_conflict" not in post(p, {"cladding": "brick veneer"})
    assert "cladding_user_conflict" not in post({"cladding": {"value": None, "status": "unavailable"}},
                                                {"cladding": "wood"})


def test_edge4_integration():
    d = json.loads((ROOT / "out/golden/inputs/edge4.json").read_text())
    r = client.post("/v1/rules", json={"property_context": d["property_context"], "user_inputs": d["user_inputs"]})
    w = r.json()["warnings"]
    assert "title_type_unmapped" in w and "year_built_user_conflict" in w
    assert "cladding_user_conflict" not in w  # property cladding is unavailable: chat value is used
