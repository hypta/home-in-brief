"""Web search for property fields the records do not have (team decision 10/11, plan C).

Gemini (classify, answer, translate, extract) and the Google Search call are fakes; rules-api is a fake. No test
reads anything under out/.
"""
import copy
import json

import pytest
from b_common.report_schemas import ReportRequest
from conftest import body
from fastapi.testclient import TestClient

from app import followup, kb, web_fields
from app import followup_prompt as fp
from app import prompt as facts_prompt
from app import validator as V
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
SOURCES = [followup.WebSource(title="homes.co.nz", url="https://vertexaisearch.cloud.google.com/grounding-api-redirect/a")]


def web(text):
    return followup.WebAnswer(text=text, sources=SOURCES, search_suggestions="<div>chips</div>")


def fake(classify=None, answers=(), extract=None, translate=None):
    calls = {"classify": [], "answer": [], "translate": [], "extract": []}

    def gen(prompt, schema, timeout_s):
        assert timeout_s > 0
        for task, marker in (("classify", fp.TASK_CLASSIFY), ("answer", fp.TASK_ANSWER),
                             ("translate", fp.TASK_TRANSLATE), ("extract", fp.TASK_EXTRACT)):
            if prompt.startswith(marker):
                calls[task].append(prompt)
                break
        else:
            raise AssertionError("unknown task")
        if task == "classify":
            assert classify is not None, "classify must not be called"
            out = classify
        elif task == "extract":
            assert extract is not None, "extract must not be called"
            out = extract
        elif task == "translate":
            texts = json.loads(prompt.split("TEXTS:", 1)[1])
            out = {"texts": (translate or (lambda t: t))(texts)}
        else:
            out = answers[min(len(calls["answer"]), len(answers)) - 1] if answers else {"answer": [], "links": []}
        return json.dumps(out)

    gen.calls = calls
    return gen


def cls(language="en", **kw):
    return {"language": language, "out_of_scope": False, "year_built": None, "cladding": None,
            "hypothetical": False, "links": [], **kw}


@pytest.fixture
def search(monkeypatch):
    """search.reply = WebAnswer | None; search.prompts lists the prompts sent."""
    class S:
        reply = None
        prompts: list = []

    s = S()
    s.prompts = []

    def fn(prompt, timeout_s):
        assert timeout_s > 0
        s.prompts.append(prompt)
        return s.reply

    monkeypatch.setattr(followup, "gemini_search", fn)
    return s


@pytest.fixture
def rules(monkeypatch):
    """Fake rules-api: kb_leaky_build_era from the user's input when a year is supplied."""
    seen = []

    def fn(pc, ui, timeout):
        seen.append(copy.deepcopy(ui))
        ro = copy.deepcopy(body("case1")["rules_output"])
        if (ui.get("chat_supplied") or {}).get("year_built"):
            ro["kb_items"] = ro["kb_items"] + ["kb_leaky_build_era"]
            ro["kb_items_from_user_input"] = ["kb_leaky_build_era"]
        return ro

    monkeypatch.setattr(followup, "call_rules", fn)
    return seen


def post(case, question="", b=None, **extra):
    b = copy.deepcopy(b or body(case)) | extra
    if question:
        b["question"] = question
    return client.post("/v1/followup", json=b)


def web_facts(year):
    """The follow-up facts for case1 with a year found online (rules re-run selected kb_leaky_build_era)."""
    b = copy.deepcopy(body("case1"))
    b["rules_output"]["kb_items"] = b["rules_output"]["kb_items"] + ["kb_leaky_build_era"]
    b["rules_output"]["kb_items_from_user_input"] = ["kb_leaky_build_era"]
    b["user_inputs"]["chat_supplied"] = {"year_built": year}
    b["user_inputs"]["chat_supplied_source"] = {"year_built": "web_search"}
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    ev = V.build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
    return facts_prompt.followup_facts(req, ev, data.items, {})


# ---- conversation trigger -------------------------------------------------------------------------------
def test_question_about_a_missing_year_searches_extracts_and_reruns_rules(monkeypatch, search, rules):
    text = "Homes.co.nz lists this home as built in 1987. It last sold in May 2015 for $910,000."
    search.reply = web(text)
    year = next(f for f in web_facts(1987) if f.topic == "program:year_built")
    assert year.program and year.user and "The year built found online is" in year.text
    gen = fake(cls(), [{"answer": [{"text": "It is that year.", "fact_ids": [year.id]}], "links": []}])
    monkeypatch.setattr(followup, "gemini_json", gen)
    r = post("case1", "When was this place built, and when was it last sold?")
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(search.prompts) == 1 and search.prompts[0].startswith(fp.TASK_FIELDS)
    assert "built" in search.prompts[0] and "last sold" in search.prompts[0]
    assert out["web"]["text"] == text  # shown as returned
    assert out["web"]["fields"] == ["year_built", "last_sale"]
    assert out["web"]["extracted"] == {"year_built": 1987, "last_sale": {"price": "$910,000", "date": "May 2015"}}
    assert out["user_supplied"] == {"year_built": 1987}  # last_sale never goes to rules-api
    assert out["user_supplied_source"] == {"year_built": "web_search"}
    assert rules and rules[0]["chat_supplied"] == {"year_built": 1987}
    assert rules[0]["chat_supplied_source"] == {"year_built": "web_search"}
    assert out["answer"][0]["text"] == ("Based on a web search (not checked by this service), the year built found "
                                        "online is 1987, a period Settled.govt.nz's guidance on leaky buildings "
                                        "covers; council records could not confirm it.")
    assert "A web search just found the year built" in gen.calls["answer"][0]
    # the fixed "no licensed source for sale prices" sentence is not shown next to the search answer
    assert not any("sale" in s["text"] for s in out["answer"])


def test_found_year_is_answered_from_the_records_without_a_search(monkeypatch, search):
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("no rules re-run"))
    monkeypatch.setattr(followup, "gemini_json", fake(cls(), [{"answer": [], "links": []}]))
    post("case3", "When was this place built?")
    assert search.prompts == []


def test_statement_with_a_year_or_value_given_earlier_does_not_search(monkeypatch, search, rules):
    monkeypatch.setattr(followup, "gemini_json", fake(cls(year_built="1997"), [{"answer": [], "links": []}]))
    post("case1", "It was built in 1997. What does that mean?")
    b = copy.deepcopy(body("case1"))
    b["user_inputs"]["chat_supplied"] = {"year_built": 1997}
    monkeypatch.setattr(followup, "gemini_json", fake(cls(), [{"answer": [], "links": []}]))
    post("case1", "When was it built?", b=b)
    assert search.prompts == []


def test_no_search_answer_falls_back_to_the_fixed_sentence(monkeypatch, search, rules):
    search.reply = None
    monkeypatch.setattr(followup, "gemini_json", fake(cls(), [{"answer": [], "links": []}]))
    out = post("case1", "When was it last sold?").json()
    assert len(search.prompts) == 2  # asked once more when no source
    assert "web" not in out and out["answer"][0]["text"] == facts_prompt.NO_SOURCE_TEXT["last_sale"]


def test_eval_runs_never_search(monkeypatch, search, rules):
    monkeypatch.setattr(followup, "gemini_json", fake(cls(), [{"answer": [], "links": []}]))
    req = followup.FollowupRequest.model_validate(body("case1") | {"question": "When was it last sold?"})
    out = followup.answer_followup(req)  # web_search off
    assert search.prompts == [] and out.web is None


# ---- the button: search_field ---------------------------------------------------------------------------
def test_button_year_built(monkeypatch, search, rules):
    search.reply = web("OneRoof gives the year built as 1972.")
    monkeypatch.setattr(followup, "gemini_json", fake(None, [{"answer": [], "links": []}]))
    r = post("case1", search_field="year_built")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["user_supplied"] == {"year_built": 1972} and out["user_supplied_source"] == {"year_built": "web_search"}
    assert out["web"]["fields"] == ["year_built"] and rules[0]["chat_supplied"] == {"year_built": 1972}
    assert "TASK: web search" in search.prompts[0] and "the year (or decade) this building was built" in search.prompts[0]


def test_button_never_replaces_a_value_the_user_gave(monkeypatch, search):
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("no rules re-run"))
    b = copy.deepcopy(body("case1"))
    b["user_inputs"]["chat_supplied"] = {"year_built": 1998}
    search.reply = web("Homes.co.nz says it was built in 1987.")
    monkeypatch.setattr(followup, "gemini_json", fake(None, [{"answer": [], "links": []}]))
    out = post("case1", b=b, search_field="year_built").json()
    assert out["user_supplied"] == {} and out["web"]["extracted"] == {"year_built": 1987}


def test_button_cladding_is_normalized(monkeypatch, search, rules):
    search.reply = web("Listing pages say the exterior walls are weatherboard, under a tiled roof.")
    monkeypatch.setattr(followup, "gemini_json", fake(None, [{"answer": [], "links": []}]))
    out = post("case1", search_field="cladding").json()
    assert out["user_supplied"] == {"cladding": "wood"} and out["web"]["extracted"] == {"cladding": "wood"}
    assert rules[0]["chat_supplied"] == {"cladding": "wood"}


def test_button_last_sale_is_shown_but_never_supplied(monkeypatch, search):
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("no rules re-run"))
    search.reply = web("Homes.co.nz shows it last sold in March 2019 for $850,000.")
    gen = fake(None)
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post("case1", search_field="last_sale").json()
    assert out["user_supplied"] == {} and "user_supplied_source" not in out and out["answer"] == []
    assert out["web"]["extracted"] == {"last_sale": {"price": "$850,000", "date": "March 2019"}}
    assert gen.calls["classify"] == [] and gen.calls["answer"] == []


def test_button_needs_no_question_but_a_request_needs_one_or_the_other(search):
    r = client.post("/v1/followup", json=body("case1") | {"search_field": "roof"})
    assert r.status_code == 422
    r = client.post("/v1/followup", json=body("case1"))
    assert r.status_code == 422


def test_ambiguous_years_are_not_filled(monkeypatch, search):
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("no rules re-run"))
    search.reply = web("One listing says it was built in 1965; another says it was built in 1972.")
    monkeypatch.setattr(followup, "gemini_json", fake(None))  # no extract call for an ambiguous field
    out = post("case1", search_field="year_built").json()
    assert out["user_supplied"] == {} and out["web"]["extracted"] == {} and out["answer"] == []


def test_model_value_not_written_in_the_text_is_dropped(monkeypatch, search):
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("no rules re-run"))
    search.reply = web("OneRoof describes it as a post-war bungalow.")
    gen = fake(None, extract={"year_built": "1955", "year_built_quote": "a post-war bungalow"})
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post("case1", search_field="year_built").json()
    assert gen.calls["extract"] and out["user_supplied"] == {} and out["web"]["extracted"] == {}


def test_model_value_written_in_the_text_is_kept(monkeypatch, search, rules):
    search.reply = web("OneRoof gives 1972 as the date of the house.")
    gen = fake(None, [{"answer": [], "links": []}],
               extract={"year_built": "1972", "year_built_quote": "1972 as the date of the house"})
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post("case1", search_field="year_built").json()
    assert out["user_supplied"] == {"year_built": 1972}
    assert "OneRoof gives 1972" in gen.calls["extract"][0] and "QUESTION" not in gen.calls["extract"][0]


def test_conflict_with_the_property_year_uses_the_record(monkeypatch, search):
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("the record wins: no rules re-run"))
    search.reply = web("Homes.co.nz says the house was built in 1985.")
    monkeypatch.setattr(followup, "gemini_json", fake(None, [{"answer": [{"text": "No.", "fact_ids": []}]}]))
    out = post("case3", search_field="year_built").json()
    assert out["user_supplied"] == {"year_built": 1985} and out["user_supplied_source"] == {"year_built": "web_search"}
    first = out["answer"][0]
    assert first["text"] == ("A web search gave the year built as 1985; the council records the 1960s (1960–1969), "
                             "and this answer uses the council's record.")
    assert first["source_ids"] == ["ac_property_page"]


# ---- language -------------------------------------------------------------------------------------------
def test_chinese_question_gets_the_chinese_web_prefix(monkeypatch, search, rules):
    search.reply = web("根據 homes.co.nz，這棟房子建於 1987 年。")
    era = next(f for f in web_facts(1987) if "mostly built" in f.text)
    assert era.user
    zh = "網路搜尋找到的建造年份是 1987 年，屬於 Settled.govt.nz 漏水屋指引涵蓋的時期；市議會紀錄無法確認。"
    gen = fake(cls("zh-Hant"), [{"answer": [{"text": "Settled.govt.nz 的漏水屋指引討論這個年代建造的房屋。",
                                             "fact_ids": [era.id]}], "links": []}], translate=lambda t: [zh] * len(t))
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post("case1", "這房子哪年蓋的？").json()
    assert out["language"] == "zh-Hant" and out["user_supplied"] == {"year_built": 1987}
    # the program fact of the asked field (translated) and the rewrite, each with the Chinese web prefix
    assert [s["text"] for s in out["answer"]] == ["依網路搜尋結果（未經本系統核對），" + zh,
                                                 "依網路搜尋結果（未經本系統核對），Settled.govt.nz 的漏水屋指引討論這個年代建造的房屋。"]
    assert out["web"]["text"] == "根據 homes.co.nz，這棟房子建於 1987 年。"


def test_value_found_online_earlier_keeps_the_web_prefix_in_later_answers(monkeypatch):
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("no rules re-run"))
    b = copy.deepcopy(body("case1"))
    b["user_inputs"]["chat_supplied"] = {"cladding": "brick"}
    b["user_inputs"]["chat_supplied_source"] = {"cladding": "web_search"}
    monkeypatch.setattr(followup, "gemini_json", fake(cls(), [{"answer": [], "links": []}]))
    out = post("case1", "What is the cladding?", b=b).json()
    assert out["answer"][0]["text"] == ("Based on a web search (not checked by this service), the cladding found online "
                                        "is brick; no free public source records the cladding, so public data could "
                                        "not confirm it.")


def test_report_summary_prefix_names_the_web_search():
    b = copy.deepcopy(body("case5"))  # chat_supplied 1998 + monolithic, kb_monolithic_cladding from the user's input
    b["user_inputs"]["chat_supplied_source"] = {"year_built": "web_search", "cladding": "web_search"}
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    ev = V.build_evidence(req, data.items, data.sources.keys())
    assert facts_prompt.from_web(["kb_monolithic_cladding"], req, ev, data.items)
    b["user_inputs"]["chat_supplied_source"] = {"year_built": "web_search", "cladding": "user"}
    req = ReportRequest.model_validate(b)
    assert not facts_prompt.from_web(["kb_monolithic_cladding"], req, ev, data.items)
    s = V.with_prefix(V.Sentence(text="The cladding is monolithic.", source_ids=[]), V.WEB_PREFIX)
    assert s.text == "Based on a web search (not checked by this service), the cladding is monolithic."
    assert V.strip_prefix(s.text) == "the cladding is monolithic."


def test_rules_api_accepts_the_source_key():
    from b_common.schemas import UserInputs
    ui = UserInputs.model_validate(body("case1")["user_inputs"] | {
        "chat_supplied": {"year_built": 1987}, "chat_supplied_source": {"year_built": "web_search"}})
    assert ui.chat_supplied.year_built == 1987


# ---- mock mode ------------------------------------------------------------------------------------------
def test_mock_mode_returns_the_fixed_web_answer(monkeypatch, search, rules):
    monkeypatch.setenv("REPORT_MOCK", "true")
    out = post("case1", search_field="year_built").json()
    assert search.prompts == []
    assert out["web"]["text"].startswith("MOCK web search answer") and out["web"]["extracted"] == {"year_built": 1985}
    assert out["user_supplied"] == {"year_built": 1985} and out["user_supplied_source"] == {"year_built": "web_search"}
    out = post("case1", "When was it built and when was it last sold?").json()
    assert out["web"]["extracted"] == {"year_built": 1985, "last_sale": {"price": "$850,000", "date": "March 2019"}}
    out = post("case1", search_field="cladding").json()
    assert out["user_supplied"] == {"cladding": "wood"}


# ---- extraction by code ---------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Records show the home was built in 1998 and last sold in May 2015.", {"year_built": 1998}),
    ("Built in 1965, renovated in 2004.", {"year_built": 1965}),
    ("這棟房子建於 1998 年，2015 年售出。", {"year_built": 1998}),
    ("1970 年代興建的房子。", {"year_built": "1970s"}),
    ("The house was built in the 1990s.", {"year_built": "1990s"}),
    ("It may have been built in 1990.", {}),
    ("Homes.co.nz does not list a year built.", {}),
    ("It was built in 1700.", {}),
    ("It was built in the 1990s; another page says it was built in 1995.", {}),
    ("It has a brick exterior and a tiled roof.", {"cladding": "brick"}),
    ("The walls are brick and weatherboard.", {}),
    ("It has a metal roof.", {}),
])
def test_extract(text, expected):
    found, _, _ = web_fields.extract(text, ["year_built", "cladding"])
    assert found == expected
