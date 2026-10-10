import pytest
from b_common.report_schemas import ReportRequest, Sentence
from conftest import CASES, body, evidence, request

from app import assemble


def notes(case):
    return {n.field: n for n in assemble.not_found_notes(request(case))}


def test_not_found_notes_case1_templates():
    n = notes("case1")
    assert set(n) == {"year_built", "cladding", "last_sale", "annual_rates", "floor_area_m2"}
    assert n["year_built"].text == "Year built: the council's property category for this address has no build decade."
    assert n["annual_rates"].reason == "non_rateable_property"


def test_same_reason_fields_are_merged():
    n = notes("case2")
    assert len(n) == 3  # 17 fields in the golden reports
    merged = next(v for k, v in n.items() if "," in k)
    assert merged.reason == "no_unique_council_property_match"
    assert merged.field.split(",")[:2] == ["cv", "year_built"]
    assert merged.text.startswith("Council valuation (CV), year built, annual rates")
    assert merged.text.endswith("could not be matched to a single property at this address.")


def test_hazard_notes_merge_and_disclaim():
    n = notes("edge1")
    hz = n["hazards.flood_plain,hazards.flood_prone_area,hazards.overland_flow_path,hazards.coastal_inundation"]
    assert hz.reason == "service_timeout"
    assert hz.text.startswith("Flood plains, flood prone areas, overland flow paths and coastal inundation layers:")
    assert hz.text.endswith("This does not mean there is no hazard.")
    assert n["title_type,year_built"].text == "Title type and year built: not found in the public records we checked."


def test_single_hazard_note():
    b = body("case1")
    b["property_context"]["hazards"][0].update(status="unavailable", reason=None, overlap_pct=None)
    n = {x.field: x for x in assemble.not_found_notes(ReportRequest.model_validate(b))}
    assert n["hazards.flood_plain"].text == ("Flood plains layer: could not be checked this time. "
                                             "This does not mean there is no hazard.")
    assert n["hazards.flood_plain"].reason == "unavailable"


def test_leasehold_land_area_is_neutral():
    text = notes("edge4")["land_area_m2"].text
    assert "cross lease" not in text.lower() and "unit title" not in text.lower()


def test_secondary_and_found_layers_make_no_notes():
    assert not any(k.startswith("hazards.") for k in notes("case2"))


@pytest.mark.parametrize("case", CASES)
def test_next_steps_rules(case):
    req, data, ev = evidence(case)
    steps = assemble.next_steps(req, data.items, [], ev.no_loan)
    assert 1 <= len(steps) <= assemble.MAX_NEXT_STEPS
    texts = [s.text for s in steps]
    assert len(texts) == len(set(texts))
    selected = set(req.selected_kb_ids())
    for s in steps:
        assert s.kb_item == s.source_ids[0]
        for i in s.source_ids:
            assert i in selected and data.items[i]["next_step_en"] == s.text  # verbatim
    if ev.no_loan:
        assert not any(data.items[i]["topic"] == "lending" for s in steps for i in s.source_ids)


def test_identical_next_steps_merged():
    req, data, ev = evidence("case1")
    steps = assemble.next_steps(req, data.items, [Sentence(text="x", source_ids=["kb_fhl_premium"])], ev.no_loan)
    assert steps[0].source_ids == ["kb_fhl_premium", "kb_fhl_govt_not_funding_premium"]
    assert steps[0].kb_item == "kb_fhl_premium"


def test_summary_citations_come_first_and_flood_insurance_stays_in_top_6():
    req, data, ev = evidence("case1")
    cited = ["kb_rbnz_lvr", "kb_low_deposit_higher_rates", "kb_fhl_premium", "kb_fhl_deposit_5pct",
             "kb_fhl_income_basis", "kb_title_freehold", "kb_cv_not_market"]
    steps = assemble.next_steps(req, data.items, [Sentence(text="x", source_ids=cited)], ev.no_loan)
    assert steps[0].kb_item == "kb_rbnz_lvr"
    assert any("kb_flood_insurance_limit" in s.source_ids for s in steps)


def test_program_order_without_citations():
    req, data, ev = evidence("case1")
    ids = [s.kb_item for s in assemble.next_steps(req, data.items, [], ev.no_loan)]
    assert ids[:2] == ["kb_flood_insurance_limit", "kb_overland_flow_path"]
    assert "kb_rbnz_lvr" in ids


def test_warnings_drop_panel_and_internal_codes():
    b = body("case1")
    b["rules_output"]["warnings"] += ["kb_hash_mismatch:kb_x", "hazard_status_unknown:x", "rates_snapshot_stale"]
    b["rules_output"]["kb_includes_draft"] = True
    w = assemble.reader_warnings(ReportRequest.model_validate(b))
    assert w == ["year_built_unavailable", "cladding_unavailable", "rates_snapshot_stale", "kb_includes_draft"]


@pytest.mark.parametrize("case,code", [("edge1", "price_missing"), ("edge2", "deposit_exceeds_price")])
def test_no_loan_sentence(case, code):
    s = assemble.no_loan_sentence(request(case))
    assert s.text == assemble.NO_LOAN_SENTENCES[code] and s.source_ids == []


def test_no_loan_sentence_absent_with_a_loan():
    assert assemble.no_loan_sentence(request("case1")) is None


REFUSED = "council_site_refuses_this_server"


def _council_refused(case="case1"):
    b = body(case)
    for name in ("year_built", "floor_area_m2", "building_site_coverage_m2", "local_board"):
        b["property_context"]["property"][name].update(value=None, status="unavailable", reason=REFUSED)
    return ReportRequest.model_validate(b)


def test_council_refused_reason_merges_into_one_neutral_note():
    notes_ = [n for n in assemble.not_found_notes(_council_refused()) if n.reason == REFUSED]
    assert len(notes_) == 1
    n = notes_[0]
    assert n.field == "year_built,floor_area_m2,building_site_coverage_m2,local_board"
    assert n.text == ("Year built, floor area, building site coverage and local board: council property records "
                      "could not be read from our server. You can enter the year built and cladding in the form "
                      "if you know them.")
    assert "no data" not in n.text.lower() and "not found" not in n.text.lower()


def _refused_note(chat):
    b = body("case1")
    for name in ("year_built", "floor_area_m2", "building_site_coverage_m2", "local_board"):
        b["property_context"]["property"][name].update(value=None, status="unavailable", reason=REFUSED)
    b["user_inputs"]["chat_supplied"] = chat
    return next(n for n in assemble.not_found_notes(ReportRequest.model_validate(b)) if n.reason == REFUSED).text


def test_council_refused_hint_only_asks_for_what_the_user_has_not_entered():
    assert _refused_note({}).endswith("You can enter the year built and cladding in the form if you know them.")
    assert _refused_note({"year_built": 1998}).endswith("You can enter cladding in the form if you know it.")
    assert _refused_note({"cladding": "brick"}).endswith("You can enter the year built in the form if you know it.")
    both = _refused_note({"year_built": "1970s", "cladding": "brick"})
    assert both.endswith("read from our server.") and "form" not in both
