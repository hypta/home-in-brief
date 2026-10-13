"""Leaky-building user facts in the summary, and follow-up answers about fields public data could not give us.

Inputs are case1 with the council property page unreadable (COUNCIL_PAGE=off: year built, floor area, building site
coverage and local board unavailable / council_site_refuses_this_server).
"""
import copy
import json

import pytest
from b_common.report_schemas import ReportRequest, Sentence
from conftest import body
from fastapi.testclient import TestClient

from app import assemble, followup, kb
from app import followup_prompt as fp
from app import prompt
from app import validator as V
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
REFUSED = {"value": None, "status": "unavailable", "reason": "council_site_refuses_this_server",
           "source_id": "ac_property_page"}
LEAKY = ["kb_leaky_build_era", "kb_monolithic_cladding"]
CLADDING_Q = "Is the cladding a leaky-building risk?"


def council_off(chat=None, user_kb=()):
    b = copy.deepcopy(body("case1"))
    for name in ("year_built", "floor_area_m2", "building_site_coverage_m2", "local_board"):
        b["property_context"]["property"][name] = dict(REFUSED)
    if chat:
        b["user_inputs"]["chat_supplied"] = chat
    b["rules_output"]["kb_items_from_user_input"] = list(user_kb)
    return b


def setup(b):
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    return req, data, V.build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)


# ---- problem 2: a user fact that triggered a leaky-building item is a must-mention --------------------------
def test_user_monolithic_1998_is_a_must_with_prefix_and_land_use_still_in_the_summary():
    req, data, ev = setup(council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY))
    fs = prompt.fact_list(req, ev, data.items)
    musts = [f for f in fs if f.must]
    assert len(musts) <= prompt.MAX_TOPICS
    user = [f for f in musts if f.user]
    assert len(user) == 1 and set(LEAKY) <= set(user[0].source_ids)
    text = prompt.draft_text(user[0])
    assert text.startswith(V.PREFIX) and "leaky buildings" in text and "monolithic" in text
    # the non-residential use the user fact pushed out is merged into the title sentence (case1 must-mentions)
    title = next(f for f in musts if f.topic == "title")
    assert "RESERVE LAND" in title.draft() and "Freehold" in title.draft() and "ac_valuation" in title.source_ids
    assert "the year you gave, 1998," in text  # the user's own year, labelled
    for f in musts:
        assert V.check_sentence(Sentence(text=prompt.draft_text(f), source_ids=f.source_ids),
                                V.with_user_year(ev, [f])) == [], f.text


def test_user_year_alone_in_the_leaky_era_is_a_must_and_names_the_guidance():
    req, data, ev = setup(council_off({"year_built": 1998}, ["kb_leaky_build_era"]))
    fs = prompt.fact_list(req, ev, data.items)
    user = [f for f in fs if f.user and f.must]
    assert len(user) == 1 and "kb_leaky_build_era" in user[0].source_ids
    assert "leaky buildings" in user[0].draft() and "The year you gave, 1998, falls" in user[0].draft()
    assert "1998" not in user[0].text  # the stored text has only the placeholder


def test_web_search_year_is_not_shown_in_the_leaky_facts():
    b = council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY)
    b["user_inputs"]["chat_supplied_source"] = {"year_built": "web_search"}
    req, data, ev = setup(b)
    fs = prompt.fact_list(req, ev, data.items)
    user = [f for f in fs if f.user and f.must]
    assert user and not any("1998" in f.draft() or "user_year" in f.slots for f in fs)
    assert "found online" in user[0].draft()


def test_a_rewrite_that_swaps_the_year_or_drops_you_gave_is_blocked():
    req, data, ev = setup(council_off({"year_built": 1998}, ["kb_leaky_build_era"]))
    fs = prompt.fact_list(req, ev, data.items)
    f = next(f for f in fs if f.user and f.must)

    def check(text):
        return V.check_rewrite({"text": text, "fact_ids": [f.id]}, fs, ev).fails
    ok = ("The year you gave, {{%s.user_year}}, falls in a period covered by Settled.govt.nz's guidance on leaky "
          "buildings; council records could not confirm it." % f.id)
    assert check(ok) == []
    assert V.BARE_NUMBER in check(ok.replace("{{%s.user_year}}" % f.id, "1999"))          # another year
    assert V.BARE_NUMBER in check(ok.replace("{{%s.user_year}}" % f.id, "1998"))          # the number written bare
    assert V.MISSING_QUALIFIER in check(ok.replace("you gave", "found"))                  # label dropped
    assert V.SLOT_CONTEXT in check(ok.replace("The year you gave, {{", "The year you gave is {{"))
    # the year is allowed for this slot only: the same number in another sentence is still refused
    other = V.check_sentence(Sentence(text="The council valuation is 1998.", source_ids=["ac_valuation"]),
                             V.with_user_year(ev, [f]))
    assert "number_not_in_cited_sources" in other


def test_user_facts_that_trigger_nothing_stay_optional():
    req, data, ev = setup(council_off({"year_built": "1970s", "cladding": "brick"}))
    fs = prompt.fact_list(req, ev, data.items)
    assert not any(f.user and f.must for f in fs)


def test_next_steps_keep_the_triggered_leaky_items_within_six():
    req, data, ev = setup(council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY))
    fs = prompt.fact_list(req, ev, data.items)
    summary = [Sentence(text=prompt.draft_text(f), source_ids=f.source_ids) for f in fs if f.must]
    ids = [s.kb_item for s in assemble.next_steps(req, data.items, summary, ev.no_loan)]
    assert len(ids) <= assemble.MAX_NEXT_STEPS and set(LEAKY) <= set(ids)
    assert "kb_flood_insurance_limit" in ids  # the flood item stays pinned too
    # even when the summary cites none of them (every rewrite blocked and drafts dropped)
    ids = [s.kb_item for s in assemble.next_steps(req, data.items, [], ev.no_loan)]
    assert set(LEAKY) <= set(ids) and "kb_flood_insurance_limit" in ids and len(ids) == assemble.MAX_NEXT_STEPS


# ---- problem 1: follow-up questions about a field public data could not give us ----------------------------
def fake(answer):
    def gen(p, schema, timeout_s):
        if p.startswith(fp.TASK_CLASSIFY):
            return json.dumps({"out_of_scope": False, "year_built": None, "cladding": None, "hypothetical": False,
                               "links": []})
        return json.dumps({"answer": answer(p), "links": []})
    return gen


@pytest.fixture
def no_rules(monkeypatch):
    def rules(pc, ui, timeout):
        raise AssertionError("rules-api must not be called")
    monkeypatch.setattr(followup, "call_rules", rules)


def facts_of(b):
    req, data, ev = setup(b)
    return prompt.followup_facts(req, ev, data.items)


def ask(monkeypatch, b, question, answer):
    monkeypatch.setattr(followup, "gemini_json", fake(answer))
    r = client.post("/v1/followup", json=b | {"question": question})
    assert r.status_code == 200, r.text
    return r.json()["answer"]


def test_cladding_not_found_is_answered_even_when_the_model_picks_an_unrelated_fact(monkeypatch, no_rules):
    b = council_off()
    rates = next(f.id for f in facts_of(b) if "annual rates" in f.text)
    out = ask(monkeypatch, b, CLADDING_Q,
              lambda p: [{"text": "The annual rates could not be checked from public records.", "fact_ids": [rates]}])
    assert out[0] == {"text": prompt.NO_SOURCE_TEXT["cladding"], "source_ids": []}


def test_cladding_program_fact_named_by_the_model_is_shown_as_its_draft(monkeypatch, no_rules):
    b = council_off()
    clad = next(f for f in facts_of(b) if f.topic == "program:cladding")
    assert clad.program and clad.source_ids == []
    out = ask(monkeypatch, b, "What are the outside walls made of?",
              lambda p: [{"text": "Nobody knows the cladding, it is totally safe.", "fact_ids": [clad.id]}])
    assert out == [{"text": prompt.NO_SOURCE_TEXT["cladding"], "source_ids": []}]


def test_cladding_entered_as_monolithic_states_it_with_prefix_and_the_kb_item(monkeypatch, no_rules):
    b = council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY)
    fs = facts_of(b)
    claim = next(f for f in fs if f.topic == "kb:cladding")
    out = ask(monkeypatch, b, CLADDING_Q, lambda p: [{"text": claim.text, "fact_ids": [claim.id]}])
    assert out[0]["text"] == ("Based on what you told us, the cladding you gave is monolithic, which Settled.govt.nz "
                              "discusses in its guidance on leaky buildings; public data could not confirm it.")
    assert out[0]["source_ids"] == ["kb_monolithic_cladding"]
    assert out[1]["text"].startswith("Based on what you told us, Settled.govt.nz discusses plaster-style monolithic")
    assert out[1]["source_ids"] == ["kb_monolithic_cladding"]


def test_cladding_entered_as_brick_states_it_without_a_leaky_item(monkeypatch, no_rules):
    b = council_off({"year_built": "1970s", "cladding": "brick"})
    out = ask(monkeypatch, b, CLADDING_Q, lambda p: [])
    assert out == [{"text": "Based on what you told us, the cladding you gave is brick; no free public source records "
                            "the cladding, so public data could not confirm it.", "source_ids": []}]
    assert not any(i.startswith("kb_") for s in out for i in s["source_ids"])


def test_year_question_states_the_year_the_user_entered(monkeypatch, no_rules):
    b = council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY)
    out = ask(monkeypatch, b, "When was this house built?", lambda p: [])
    assert out[0]["text"] == ("Based on what you told us, the year built you gave is 1998, a period Settled.govt.nz's "
                              "guidance on leaky buildings covers; council records could not confirm it.")
    assert out[0]["source_ids"] == ["ac_property_page", "kb_leaky_build_era"]


def test_year_outside_the_leaky_era_says_why_it_was_not_checked(monkeypatch, no_rules):
    b = council_off({"year_built": "1970s", "cladding": "brick"})
    out = ask(monkeypatch, b, "How old is the house?", lambda p: [])
    assert out == [{"text": "Based on what you told us, the year built you gave is the 1970s; council records could "
                            "not be read from our server to confirm it.", "source_ids": ["ac_property_page"]}]


def test_year_question_without_a_user_year_uses_the_council_not_readable_fact():
    fs = facts_of(council_off())
    assert not any(f.topic == "program:year_built" for f in fs)
    assert any(f.topic == "council_not_readable" and "enter it in the form" in f.text for f in fs)


def test_council_refused_fields_have_cited_followup_facts():
    fs = facts_of(council_off())
    for label in ("floor area", "local board", "building site coverage"):
        f = next(f for f in fs if f"the {label} is unknown" in f.text)
        assert f.source_ids == ["ac_property_page"] and not f.program


def test_user_year_in_a_program_fact_is_not_evidence_elsewhere():
    req, data, ev = setup(council_off({"year_built": 1998}, ["kb_leaky_build_era"]))
    bad = Sentence(text="The council valuation dates from 1998.", source_ids=["ac_valuation"])
    assert prompt.check_program(bad, ev) != []


def test_comma_as_cause_is_blocked_but_as_you_entered_is_not():
    req, data, ev = setup(body("case5"))
    fs = prompt.fact_list(req, ev, data.items)
    lending = next(f for f in fs if f.topic == "lending")
    bad = V.check_rewrite({"text": "Reserve Bank rules show your loan is not high-LVR or high-DTI lending, as no existing "
                                   "debts were entered so the DTI assumes none.", "fact_ids": [lending.id]}, fs, ev)
    assert V.ADDED_CAUSE in bad.fails
    assert V._CAUSE_RE.findall("the cap for multiple buyers, as you entered") == []
