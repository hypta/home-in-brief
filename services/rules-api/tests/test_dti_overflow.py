"""F09: finite inputs that would make DTI overflow to infinity (serialized as null) are rejected with 422."""
import math

import pytest
from fastapi.testclient import TestClient

from app import calc
from app.main import app
from b_common.schemas import PropertyContext, UserInputs


@pytest.mark.parametrize("field,val", [
    ("household_income", 1e-309), ("household_income", 0.001), ("price_override", 1e-309),
    ("household_income", 1e15), ("existing_debts", 1e15), ("price_override", 1e15), ("deposit", 1e15),
])
def test_out_of_range_amount_is_field_level_422(field, val):
    ui = {"price_override": 1_000_000, "deposit": 100_000, "household_income": 130_000, field: val}
    r = TestClient(app).post("/v1/rules", json={"user_inputs": ui})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert [f["field"] for f in err["fields"]] == [f"user_inputs.{field}"]


def test_extreme_allowed_inputs_keep_a_finite_dti():
    ui = {"price_override": 1e10, "deposit": 0, "household_income": 0.01, "existing_debts": 1e10}
    r = TestClient(app).post("/v1/rules", json={"user_inputs": ui})
    assert r.status_code == 200
    dti = r.json()["dti"]
    assert dti is not None and math.isfinite(dti)


def test_calc_refuses_an_infinite_dti():
    # model_construct skips the schema bound: the calculation itself still does not return dti null
    ui = UserInputs.model_construct(price_override=1_000_000.0, deposit=100_000.0, household_income=1e-309,
                                    existing_debts=None, rate_pct=None, household_type=None,
                                    chat_supplied=UserInputs().chat_supplied)
    with pytest.raises(calc.InputError) as e:
        calc.compute(PropertyContext(), ui, None)
    assert e.value.field_name == "household_income"
