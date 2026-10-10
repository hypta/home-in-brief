import pytest
from conftest import CASES, evidence

from app import prompt


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
    assert any("1975, not the year you gave" in t["say"] for t in build("edge4")[1])
