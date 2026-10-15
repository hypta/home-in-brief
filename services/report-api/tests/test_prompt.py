import importlib.util

import pytest
from b_common.report_schemas import Sentence
from conftest import CASES, REPO, evidence

from app import gemini, prompt
from app import validator as V


def build(case):
    req, data, ev = evidence(case)
    titles = {s.id: s.title or "" for s in req.sources()} | data.sources
    return prompt.build_prompt(req, ev, data.items, titles), prompt.topics(req, ev, data.items)


@pytest.mark.parametrize("case", CASES)
def test_topics_cite_known_ids_and_stay_within_five(case):
    req, data, ev = evidence(case)
    tops = prompt.topics(req, ev, data.items)
    assert 1 <= len(tops) <= prompt.MAX_TOPICS
    for t in tops:
        assert t["cite"] and all(i in ev.known_ids for i in t["cite"]), t


def test_case1_prompt_has_placeholders_and_no_values():
    text, tops = build("case1")
    facts_part = text.split("CASE DATA")[-1]
    assert "{{F1.flood_plain_pct}}" in facts_part and "{{F2.lvr_threshold}}" in facts_part
    import re
    values_part = re.sub(r'F\d+|"(?:words|negations)": \d+', "", facts_part)
    assert not any(c.isdigit() for c in values_part)
    assert "RESERVE LAND" not in text and "Freehold" not in facts_part
    assert tops[0]["cite"][:3] == ["ac_flood_plains", "ac_flood_prone", "ac_overland_flow"]
    assert any("RESERVE LAND" in t["say"] for t in tops)  # non-residential use


@pytest.mark.parametrize("case,user_year", [("case5", "1998"), ("edge4", "1999")])
def test_user_year_reaches_the_prompt_only_as_a_placeholder(case, user_year):
    text, tops = build(case)
    assert user_year not in text and "{{F" in text  # the number itself never goes to the model
    req, data, ev = evidence(case)
    fs = prompt.fact_list(req, ev, data.items)
    uf = next(f for f in fs if f.topic.startswith("user_year"))
    assert "{{" + uf.id + ".user_year}}" in uf.text and uf.slots["user_year"].value == int(user_year)
    # the final draft states it, labelled as the user's own
    assert any(f"the year you gave, {user_year}," in t["say"] for t in tops)


@pytest.mark.parametrize("case,user_year", [("case5", "1998"), ("edge4", "1999")])
def test_web_search_year_never_appears_in_the_summary(case, user_year):
    req, data, ev = evidence(case)
    req.user_inputs["chat_supplied_source"] = {"year_built": "web_search"}
    tops = prompt.topics(req, ev, data.items)
    assert not any(user_year in t["say"] for t in tops)
    assert any("the year found online" in t["say"] for t in tops)
    fs = prompt.fact_list(req, ev, data.items)
    assert not any("user_year" in f.slots for f in fs)


def test_no_loan_case_has_no_lending_topic():
    _, tops = build("edge2")
    assert not any("loan" in t["say"].lower() for t in tops)


def test_landslide_topic_only_when_high():
    assert not any("landslide" in t["say"] for t in build("case1")[1])
    assert any("High" in t["say"] for t in build("case3")[1])


def test_user_year_relation():
    assert any("same decade" in t["say"] for t in build("case5")[1])
    assert any("the year you gave, 1999, differs" in t["say"] and "1975" in t["say"] for t in build("edge4")[1])


@pytest.mark.parametrize("case", CASES)
def test_every_draft_passes_the_validator_as_written(case):
    """Gemini copies the drafts, so each draft must already be a valid sentence (with room to spare)."""
    req, data, ev = evidence(case)
    fs = prompt.fact_list(req, ev, data.items)
    for t in prompt.topics(req, ev, data.items):
        s = Sentence(text=t["say"], source_ids=t["cite"])
        assert V.check_sentence(s, V.with_user_year(ev, fs, t["say"])) == [], t  # the year only in its own sentence
        assert prompt.words(t["say"]) <= V.MAX_WORDS - 1, t


@pytest.mark.parametrize("case", CASES)
def test_user_fact_topics_are_prefixed_and_detected(case):
    req, data, ev = evidence(case)
    for t in prompt.topics(req, ev, data.items):
        s = Sentence(text=t["say"], source_ids=t["cite"])
        assert t["user"] == V.relies_on_user_input(s, ev), t
        assert t["user"] == t["say"].startswith(V.PREFIX), t


def _eval_module():
    spec = importlib.util.spec_from_file_location("eval_report_api", REPO / "scripts" / "eval_report_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("case", CASES)
def test_drafts_cover_every_rubric_must_mention(case):
    """The must / must-not checks hold on the drafts alone (plus the code-written no-loan sentence)."""
    from app import assemble

    ev_mod = _eval_module()
    req, data, ev = evidence(case)
    summary = [{"text": t["say"], "source_ids": t["cite"]} for t in prompt.topics(req, ev, data.items)]
    fixed = assemble.no_loan_sentence(req)
    if fixed:
        summary.append({"text": fixed.text, "source_ids": []})
    checks = [c for c in ev_mod.rubric(case, {"summary": summary, "next_steps": []}, data.items)
              if c["kind"] in ("S", "not", "year")]
    assert [c["check"] for c in checks if not c["ok"]] == []


@pytest.mark.parametrize("case,user_year", [("case5", "1998"), ("edge4", "1999")])
def test_rubric_wants_the_users_year_but_never_a_web_search_year(case, user_year):
    """The same drafts: shown with "you gave" for a typed year (rubric passes), and the rubric flags the number
    when the year came from a web search."""
    ev_mod = _eval_module()
    req, data, ev = evidence(case)
    summary = [{"text": V.PREFIX + t["say"], "source_ids": t["cite"]} for t in prompt.topics(req, ev, data.items)]
    rep = {"summary": summary, "next_steps": []}
    year = lambda src: [c for c in ev_mod.rubric(case, rep, data.items, src) if c["kind"] == "year"]  # noqa: E731
    assert [c["ok"] for c in year("user")] == [True]
    assert [c["ok"] for c in year("web_search")] == [False]
    rep = {"summary": [{"text": "Your year could not be checked.", "source_ids": []}], "next_steps": []}
    assert [c["ok"] for c in ev_mod.rubric(case, rep, data.items, "user") if c["kind"] == "year"] == [False]
    assert [c["ok"] for c in ev_mod.rubric(case, rep, data.items, "web_search") if c["kind"] == "year"] == [True]


def test_known_wording_fixes():
    tops = {c: " ".join(t["say"] for t in build(c)[1]) for c in ("case1", "case4", "case5")}
    assert "exempt from Reserve Bank LVR limits" in tops["case1"]
    assert "borrowing over 80% of the price" in tops["case1"]
    assert "debt over 6 times income" in tops["case4"]
    assert "year built could not be checked" in tops["case4"]
    assert "no existing debts" not in tops["case5"]  # the page says it on the DTI line
    assert "zero" not in tops["case5"]


def test_low_temperature_and_seed():
    assert gemini.TEMPERATURE == 0.0 and isinstance(gemini.SEED, int)


def test_null_lvr_and_dti_when_no_loan_are_handled():
    """rules-api now returns lvr_pct/dti as null without a loan: no lending topic, no crash, no blacklist trouble."""
    from b_common.report_schemas import ReportRequest
    from app import kb
    from conftest import body
    b = body("edge2")
    b["rules_output"]["lvr_pct"] = None
    b["rules_output"]["dti"] = None
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    ev = V.build_evidence(req, data.items, data.sources.keys())
    tops = prompt.topics(req, ev, data.items)
    assert ev.no_loan and not any("loan" in t["say"].lower() for t in tops)
    assert not any("lending" in t["say"].lower() for t in tops)


def test_title_stays_a_must_mention_with_unchecked_flood_layer_and_no_debts_entered():
    # hazard hit + an unchecked flood layer + a flagged loan + a First Home Loan are four of the five must-mentions;
    # the "no existing debts entered" note used to be the fifth, which pushed the title out of the summary
    from b_common.report_schemas import ReportRequest
    from conftest import body

    from app import kb

    for case in ("case1", "edge3"):
        b = body(case)
        b["rules_output"]["warnings"].append("existing_debts_assumed_zero")
        for h in b["property_context"]["hazards"]:
            if h["layer"] == "coastal_inundation":
                h["status"], h["reason"] = "unavailable", "source_unavailable"
        req = ReportRequest.model_validate(b)
        data = kb.load(req.selected_kb_ids())
        ev = V.build_evidence(req, data.items, data.sources.keys())
        fs = prompt.fact_list(req, ev, data.items)
        by_topic = {f.topic: f for f in fs}
        assert {"hazard", "hazard_unknown", "lending", "first_home_loan"} <= set(by_topic), case
        assert by_topic["title"].must, case
        assert sum(f.must for f in fs) == prompt.MAX_TOPICS
        assert "lending_debts" not in by_topic  # the page says it on the DTI line


@pytest.mark.parametrize("case", ["case5", "edge4"])
def test_eval_hard_checks_allow_the_year_only_in_the_code_written_sentence(case):
    ev_mod = _eval_module()
    req, data, ev = evidence(case)
    good = [{"text": prompt.draft_text(f), "source_ids": f.source_ids} for f in prompt.fact_list(req, ev, data.items)]
    year = next(s for s in good if "you gave," in s["text"])
    rep = {"summary": [year], "next_steps": [], "model": "mock"}
    assert not [b for b in ev_mod.hard_checks(req, rep, data.items, ev) if "fails" in b]
    for fake in ("The council year is 1998.", "Based on what you told us, the council-verified year you gave, 1998, is right.",
                 "The year you gave, 1998, is shown in the council record."):
        rep = {"summary": [year, {"text": fake, "source_ids": year["source_ids"]}], "next_steps": [], "model": "mock"}
        assert [b for b in ev_mod.hard_checks(req, rep, data.items, ev) if "fails" in b and fake[:20] in b], fake
