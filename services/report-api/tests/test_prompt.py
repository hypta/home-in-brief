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


def test_case1_prompt_lists_allowed_and_forbidden_numbers():
    text, tops = build("case1")
    assert '"ac_flood_plains": ["2026", "51.2"]' in text
    assert "720000" in text.split("FORBIDDEN NUMBERS:")[-1]
    assert tops[0]["cite"][:3] == ["ac_flood_plains", "ac_flood_prone", "ac_overland_flow"]
    assert any("RESERVE LAND" in t["say"] for t in tops)  # non-residential use


@pytest.mark.parametrize("case,user_year", [("case5", "1998"), ("edge4", "1999")])
def test_user_year_never_reaches_the_prompt(case, user_year):
    text, _ = build(case)
    assert user_year not in text


def test_no_loan_case_has_no_lending_topic():
    _, tops = build("edge2")
    assert not any("loan" in t["say"].lower() for t in tops)


def test_landslide_topic_only_when_high():
    assert not any("landslide" in t["say"] for t in build("case1")[1])
    assert any("High" in t["say"] for t in build("case3")[1])


def test_user_year_relation():
    assert any("same decade" in t["say"] for t in build("case5")[1])
    assert any("the year you gave differs" in t["say"] and "1975" in t["say"] for t in build("edge4")[1])


@pytest.mark.parametrize("case", CASES)
def test_every_draft_passes_the_validator_as_written(case):
    """Gemini copies the drafts, so each draft must already be a valid sentence (with room to spare)."""
    req, data, ev = evidence(case)
    for t in prompt.topics(req, ev, data.items):
        s = Sentence(text=t["say"], source_ids=t["cite"])
        assert V.check_sentence(s, ev) == [], t
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
    """RUBRIC 8 must / must-not checks hold on the drafts alone (plus the code-written no-loan sentence)."""
    from app import assemble

    ev_mod = _eval_module()
    req, data, ev = evidence(case)
    summary = [{"text": t["say"], "source_ids": t["cite"]} for t in prompt.topics(req, ev, data.items)]
    fixed = assemble.no_loan_sentence(req)
    if fixed:
        summary.append({"text": fixed.text, "source_ids": []})
    checks = [c for c in ev_mod.rubric(case, {"summary": summary, "next_steps": []}, data.items)
              if c["kind"] in ("S", "not")]
    assert [c["check"] for c in checks if not c["ok"]] == []


def test_known_wording_fixes():
    tops = {c: " ".join(t["say"] for t in build(c)[1]) for c in ("case1", "case4", "case5")}
    assert "exempt from Reserve Bank LVR limits" in tops["case1"]
    assert "borrowing over 80% of the price" in tops["case1"]
    assert "debt over 6 times income" in tops["case4"]
    assert "year built could not be checked" in tops["case4"]
    assert "no existing debts were entered, so the DTI assumes none" in tops["case5"]
    assert "zero" not in tops["case5"]


def test_low_temperature_and_seed():
    assert gemini.TEMPERATURE == 0.0 and isinstance(gemini.SEED, int)
