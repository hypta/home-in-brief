import json

import pytest
from b_common.report_schemas import ReportRequest
from conftest import CASES, REPO, body


@pytest.mark.parametrize("case", CASES)
def test_report_request_keeps_inputs_whole(case):
    b = body(case)
    assert ReportRequest.model_validate(b).model_dump() == b


@pytest.mark.parametrize("name", ["property_context_740_sandringham_road_extension.json",
                                  "property_context_44_lorne_street.json"])
def test_property_api_fixtures_round_trip(name):
    pc = json.loads((REPO / "services" / "property-api" / "fixtures" / name).read_text())
    req = ReportRequest.model_validate({"property_context": pc, "user_inputs": {}, "rules_output": {}})
    assert req.model_dump()["property_context"] == pc
    assert {h.layer for h in req.hazards()} >= {"flood_plain", "overland_flow_path"}
    assert req.property_fields()["cv"].source_id == "ac_valuation"


def test_typed_accessors():
    req = ReportRequest.model_validate(body("case1"))
    flood = next(h for h in req.hazards() if h.layer == "flood_plain")
    assert (flood.overlap_pct, flood.source_id, flood.status) == (51.2, "ac_flood_plains", "intersects")
    assert "linz_titles" in {s.id for s in req.sources()}
    assert "council_valuation" in {link.id for link in req.links()}
    assert req.property_fields()["year_built"].reason == "property_category_has_no_build_decade"
    assert {"rbnz_lvr", "bank_rates_snapshot", "kainga_ora_fhl"} <= req.rules_source_ids()
    assert ReportRequest.model_validate(body("case5")).chat_supplied() == {"year_built": 1998, "cladding": "monolithic"}


def test_accessors_tolerate_odd_shapes():
    req = ReportRequest.model_validate({"property_context": {"hazards": "x", "property": {"cv": 3}},
                                        "user_inputs": {}, "rules_output": {"kb_items": [1, "kb_a"]}})
    assert req.hazards() == [] and req.property_fields() == {} and req.kb_items() == ["kb_a"]
