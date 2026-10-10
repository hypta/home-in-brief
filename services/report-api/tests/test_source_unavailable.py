"""property-api answers `source_unavailable` for any read failure (the real cause stays in its log unless
REASON_DETAIL=on). The council-page fields and hazard layers must read like the council_site_refuses_this_server case.

Input: tests/data/case1_source_unavailable.json, out/golden/inputs_council_off/case1 with that reason, English only.
"""
import copy
import json
from pathlib import Path

import pytest
from b_common.report_schemas import ReportRequest
from fastapi.testclient import TestClient

from app import assemble, followup, kb
from app import followup_prompt as fp
from app import prompt
from app import validator as V
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
DATA = Path(__file__).parent / "data" / "case1_source_unavailable.json"
PAGE = "year_built,floor_area_m2,building_site_coverage_m2,local_board"
SU = "source_unavailable"
HINT = "You can enter the year built and cladding in the form if you know them."


def fixture(chat=None):
    b = json.loads(DATA.read_text(encoding="utf-8"))
    if chat:
        b["user_inputs"]["chat_supplied"] = chat
    return b


def notes(b):
    return assemble.not_found_notes(ReportRequest.model_validate(b))


def setup(b):
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    return req, data, V.build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)


def test_fixture_is_english_only():
    assert not any("一" <= c <= "鿿" for c in DATA.read_text(encoding="utf-8"))


def test_page_fields_merge_into_one_neutral_note_with_the_form_hint():
    page = [n for n in notes(fixture()) if n.reason == SU and n.field == PAGE]
    assert len(page) == 1
    assert page[0].text == ("Year built, floor area, building site coverage and local board: council property "
                            "records could not be retrieved. " + HINT)
    assert "refus" not in page[0].text and "server" not in page[0].text


def test_hint_skips_what_the_user_already_entered():
    def text(chat):
        return next(n.text for n in notes(fixture(chat)) if n.field == PAGE)
    assert text({"year_built": 1998}).endswith("You can enter cladding in the form if you know it.")
    assert text({"cladding": "brick"}).endswith("You can enter the year built in the form if you know it.")
    both = text({"year_built": "1970s", "cladding": "brick"})
    assert both.endswith("could not be retrieved.") and "form" not in both


def test_refused_and_source_unavailable_share_the_logic_but_keep_their_wording():
    b = fixture()
    for name in PAGE.split(","):
        b["property_context"]["property"][name]["reason"] = "council_site_refuses_this_server"
    n = next(n for n in notes(b) if n.field == PAGE)
    assert "could not be read from our server" in n.text and n.text.endswith(HINT)


def test_other_fields_use_the_general_sentence():
    b = fixture()
    for name in ("cv", "title_type"):
        b["property_context"]["property"][name].update(value=None, status="unavailable", reason=SU)
    n = next(n for n in notes(b) if n.field == "cv,title_type")
    assert n.text == "Council valuation (CV) and title type: could not be retrieved this time."
    assert "form" not in n.text


def test_hazard_layer_gets_the_fixed_sentence():
    b = fixture()
    layer = next(h for h in b["property_context"]["hazards"] if h["layer"] == "flood_plain")
    layer.update(status="unavailable", reason=SU)
    n = next(n for n in notes(b) if n.field == "hazards.flood_plain")
    assert n.text == ("Flood plains layer: could not be retrieved this time. "
                      "This does not mean there is no hazard.")


# ---- facts and follow-ups ----------------------------------------------------------------------------------
def test_year_built_is_one_short_optional_fact_and_other_page_fields_are_cited_facts():
    req, data, ev = setup(fixture())
    fs = prompt.fact_list(req, ev, data.items)
    hit = [f for f in fs if f.topic == "council_not_readable"]
    assert len(hit) == 1 and not hit[0].must and len(hit[0].text.split()) <= 22
    assert "retrieved" in hit[0].text and "in the form" in hit[0].text and "server" not in hit[0].text
    assert not any(f.topic == "not_checked" and "year built" in f.text for f in fs)
    ff = prompt.followup_facts(req, ev, data.items)
    for label in ("floor area", "local board", "building site coverage"):
        f = next(f for f in ff if f"the {label} is unknown" in f.text)
        assert f.source_ids == ["ac_property_page"] and not f.program and "retrieved" in f.text


def fake(answer):
    def gen(p, schema, timeout_s):
        if p.startswith(fp.TASK_CLASSIFY):
            return json.dumps({"out_of_scope": False, "year_built": None, "cladding": None, "hypothetical": False,
                               "links": []})
        return json.dumps({"answer": answer(p), "links": []})
    return gen


def ask(monkeypatch, b, question):
    def rules(pc, ui, timeout):
        raise AssertionError("rules-api must not be called")
    monkeypatch.setattr(followup, "call_rules", rules)
    monkeypatch.setattr(followup, "gemini_json", fake(lambda p: []))
    r = client.post("/v1/followup", json=copy.deepcopy(b) | {"question": question})
    assert r.status_code == 200, r.text
    return r.json()["answer"]


def test_cladding_question_is_answered_with_the_no_source_sentence(monkeypatch):
    out = ask(monkeypatch, fixture(), "What are the outside walls made of?")
    assert out == [{"text": prompt.NO_SOURCE_TEXT["cladding"], "source_ids": []}]


def test_year_question_states_the_user_year_with_a_neutral_reason(monkeypatch):
    out = ask(monkeypatch, fixture({"year_built": "1970s", "cladding": "brick"}), "When was this house built?")
    assert out == [{"text": "Based on what you told us, the year built you gave is the 1970s; council records could "
                            "not be retrieved to confirm it.", "source_ids": ["ac_property_page"]}]
