"""Which follow-up questions may be searched, and what the reply says when they are.

Everything runs through the real wrappers (followup.gemini_json, followup.gemini_search, the allowance, the request
budget) against a fake google.genai.Client: no network, nothing read from out/, no real grounding output (every
search answer below is written by hand; the Gemini API terms do not allow storing real ones). The assertions count
the Google Search calls the fake client received.
"""
import copy
import json
import socket

import pytest
from b_common.report_schemas import ReportRequest
from conftest import body
from fastapi.testclient import TestClient

from app import followup, gemini, kb, main
from app import followup_prompt as fp
from app import prompt as facts_prompt
from app import validator as V
from test_followup import cls
from test_protection import FakeGenai, counters

client = TestClient(main.app, raise_server_exceptions=False)
ADVICE_WEB = "You should buy this house; it is a good price."  # what a search would say if it were (wrongly) asked
DEFINITION_WEB = "Auckland Council describes a LIM as a report of what the council holds about a property."
UNANSWERED = {"answered": False, "answer": [], "links": []}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("rules-api must not be called"))


class ProviderError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} provider error")
        self.code = code


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RouteGenai(FakeGenai):
    """Answers classify / answer / translate / extract from fixed payloads and every Search call with `search_text`
    (grounded on one page, or on none when `sources` is False). `tasks` lists the calls in order; `seconds` is how long
    each task takes on the fake clock; `search_error` is raised by a Search call."""

    def __init__(self, classify, answers=(UNANSWERED,), search_text=DEFINITION_WEB, sources=True, search_error=None,
                 seconds=None, clock=None):
        super().__init__()
        self.classify, self.answers, self.search_text, self.sources = classify, list(answers), search_text, sources
        self.search_error, self.seconds, self.clock, self.tasks = search_error, seconds or {}, clock, []

    def prompts(self, task):
        marker = {"classify": fp.TASK_CLASSIFY, "answer": fp.TASK_ANSWER, "translate": fp.TASK_TRANSLATE,
                  "search": fp.TASK_WEB, "field": fp.TASK_FIELDS}[task]
        return [c["prompt"] for c in self.calls if c["prompt"].startswith(marker)]

    def reply(self, prompt, config):
        from google.genai import types
        task = "search" if config.tools else next(
            t for t, m in (("classify", fp.TASK_CLASSIFY), ("answer", fp.TASK_ANSWER),
                           ("translate", fp.TASK_TRANSLATE)) if prompt.startswith(m))
        self.tasks.append(task)
        if self.clock:
            self.clock.t += self.seconds.get(task, 0)
        if task == "search":
            if self.search_error:
                raise self.search_error
            chunks = [types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://redirect.invalid/1", title="x.nz"))]
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text=self.search_text)]),
                grounding_metadata=types.GroundingMetadata(
                    grounding_chunks=chunks if self.sources else [],
                    search_entry_point=types.SearchEntryPoint(rendered_content="<div/>")))])
        if task == "classify":
            out = self.classify
        elif task == "translate":
            out = {"texts": json.loads(prompt.split("TEXTS:", 1)[1])}  # unchanged: English, so a CJK check rejects it
        else:
            out = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(out))]))])


def run(monkeypatch, question, classify, case="case1", history=(), b=None, **kw):
    fake = RouteGenai(classify, **kw).install(monkeypatch)
    r = client.post("/v1/followup", json=(b or body(case)) | {"question": question, "history": list(history)})
    assert r.status_code == 200, r.text
    return r.json(), fake


# ---- 1. advice is declined before any search, whatever scope classify gave it ----------------------------------------
@pytest.mark.parametrize("question, classify", [
    # Codex's first case: classify says in scope and not searchable, the answer step says nothing answers it
    ("Should I buy this house?", cls(out_of_scope=False, searchable=False)),
    ("Is this a good price?", cls()),
    # (a purchase question that also names a report topic, "... given when it was built?", gets the facts and the
    # decline sentence instead: test_a_purchase_question_with_a_report_topic_gets_the_facts_and_the_decline)
    ("Should I make an offer on this one?", cls(out_of_scope=True, searchable=True)),
    # no keyword: classify's own advice flag stops it
    ("Would you go ahead with this one?", cls(advice=True, searchable=True)),
])
def test_advice_is_refused_without_any_search_or_answer_call(monkeypatch, question, classify):
    out, fake = run(monkeypatch, question, classify, answers=[UNANSWERED], search_text=ADVICE_WEB)
    assert fake.searches == 0 and fake.tasks == ["classify"]  # not a search, a field search or an answer call
    assert out["out_of_scope"] is True and out["answer"] == [{"text": followup.REFUSAL_EN, "source_ids": []}]
    assert "web" not in out and out["warnings"] == [] and out["language"] == "en"
    assert ADVICE_WEB not in json.dumps(out) and "should buy" not in json.dumps(out)


def test_chinese_advice_gets_the_chinese_refusal_and_its_label(monkeypatch):
    out, fake = run(monkeypatch, "我該買嗎？", cls("zh-Hant"), search_text=ADVICE_WEB)
    assert fake.searches == 0 and fake.tasks == ["classify"]
    assert out["answer"][0]["text"] == followup.REFUSAL_ZH and out["language"] == "zh-Hant" and out["out_of_scope"]


def test_a_factual_question_beside_the_advice_is_not_searched_either(monkeypatch):
    out, fake = run(monkeypatch, "Which schools are near here, and should I buy?", cls(out_of_scope=True, searchable=True),
                    search_text=ADVICE_WEB)
    assert fake.searches == 0 and out["answer"][0]["text"] == followup.REFUSAL_EN


def test_a_worth_or_a_prediction_is_never_searched(monkeypatch):
    for q in ("What is this house worth?", "Will prices rise in this suburb?"):
        out, fake = run(monkeypatch, q, cls(), answers=[UNANSWERED], search_text=ADVICE_WEB)
        assert fake.searches == 0 and "web" not in out
        assert [s["text"] for s in out["answer"]] == [followup.INSUFFICIENT_EN]


# ---- search paths, with the fake SDK ---------------------------------------------------------------------------------
def test_search_success_for_a_definition_the_facts_do_not_hold(monkeypatch):
    out, fake = run(monkeypatch, "What is a LIM report?", cls())
    assert fake.tasks == ["classify", "answer", "search"] and fake.searches == 1
    assert out["web"]["text"] == DEFINITION_WEB and out["answer"] == [] and out["out_of_scope"] is False
    assert out["language"] == "en" and out["warnings"] == []
    assert "What is a LIM report?" in fake.prompts("search")[0]


def test_search_success_for_an_out_of_scope_factual_question(monkeypatch):
    out, fake = run(monkeypatch, "Which schools are near here?", cls(out_of_scope=True, searchable=True))
    assert fake.tasks == ["classify", "search"] and out["out_of_scope"] is True and out["web"]["text"] == DEFINITION_WEB


def test_a_search_with_no_source_is_asked_once_more_then_named(monkeypatch):
    out, fake = run(monkeypatch, "What is a LIM report?", cls(), sources=False)
    assert fake.tasks == ["classify", "answer", "search", "search"]
    assert out["warnings"] == ["search_unavailable"] and "web" not in out
    assert [s["text"] for s in out["answer"]] == [followup.INSUFFICIENT_EN]


def test_a_quota_stop_in_the_search_is_named_and_nothing_follows_it(monkeypatch):
    out, fake = run(monkeypatch, "What is a LIM report?", cls(), search_error=ProviderError(429))
    assert fake.tasks == ["classify", "answer", "search"]  # no second search, no translation
    assert out["warnings"] == ["search_unavailable"] and [s["text"] for s in out["answer"]] == [followup.INSUFFICIENT_EN]
    assert counters()[0] == 3


def test_at_most_two_searches_per_request(monkeypatch):
    # the field search takes both, so the search for the rest of the question does not run
    out, fake = run(monkeypatch, "When was it last sold, and which schools are near?",
                    cls(out_of_scope=True, searchable=True), sources=False)
    assert fake.searches == 2 and out["warnings"] == ["search_unavailable"]


def test_no_time_left_means_no_search(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gemini, "clock", clock)
    # the answer call uses up the request's 25 s down to 10 s: below the 11 s a call needs
    out, fake = run(monkeypatch, "What is a LIM report?", cls(), clock=clock, seconds={"classify": 1, "answer": 14})
    assert fake.tasks == ["classify", "answer"] and fake.searches == 0
    assert [s["text"] for s in out["answer"]] == [followup.INSUFFICIENT_EN] and "web" not in out


# ---- language: the label says what was delivered ---------------------------------------------------------------------
ZH_QUESTION = "什麼是 LIM 報告？"


def test_an_english_search_answer_to_a_chinese_question_is_labelled_english(monkeypatch):
    out, fake = run(monkeypatch, ZH_QUESTION, cls("zh-Hant"))
    assert fake.searches == 1 and out["web"]["text"] == DEFINITION_WEB and out["language"] == "en"


def test_a_chinese_search_answer_keeps_the_chinese_label(monkeypatch):
    text = "奧克蘭市議會說明，LIM 報告列出市議會對一處物業持有的資料。"
    out, _ = run(monkeypatch, ZH_QUESTION, cls("zh-Hant"), search_text=text)
    assert out["web"]["text"] == text and out["language"] == "zh-Hant"
    # the label is the variant the answer is written in, not the asker's: traditional text is zh-Hant
    out, _ = run(monkeypatch, "什么是 LIM 报告？", cls("zh-Hans"), search_text=text)
    assert out["language"] == "zh-Hant"
    out, _ = run(monkeypatch, "什么是 LIM 报告？", cls("zh-Hans"), search_text="奥克兰市议会说明，LIM 报告列出市议会持有的资料。")
    assert out["language"] == "zh-Hans"
    # characters that do not tell the variants apart: the asker's variant
    out, _ = run(monkeypatch, "什么是 LIM 报告？", cls("zh-Hans"), search_text="LIM 是一份文件。")
    assert out["language"] == "zh-Hans"


def test_a_french_question_with_an_english_answer_is_labelled_english(monkeypatch):
    # a French definition question (definition_question reads "qu'est-ce que")
    out, _ = run(monkeypatch, "Qu'est-ce qu'un rapport LIM ?", cls("fr", searchable=True))
    assert out["language"] == "en"
    out, _ = run(monkeypatch, "Qu'est-ce qu'un rapport LIM ?", cls("fr", searchable=True),
                 search_text="Un rapport LIM est un document établi par le conseil sur une propriété donnée.")
    assert out["language"] == "fr"


def test_every_refusal_names_the_language_it_is_written_in(monkeypatch):
    ja = "この近くの学校はどこですか？"
    # an invalid translation (the fake returns the English text unchanged): the English refusal, labelled en
    out, fake = run(monkeypatch, ja, cls("ja", out_of_scope=True, searchable=False))
    assert out["answer"][0]["text"] == followup.REFUSAL_EN and out["language"] == "en" and fake.searches == 0
    # advice in Japanese, same
    out, _ = run(monkeypatch, "この家は買うべきですか？", cls("ja"))
    assert out["answer"][0]["text"] == followup.REFUSAL_EN and out["language"] == "en"
    # Chinese is written by code and keeps its label
    out, _ = run(monkeypatch, "這附近有什麼學校？", cls("zh-Hant", out_of_scope=True, searchable=False))
    assert out["answer"][0]["text"] == followup.REFUSAL_ZH and out["language"] == "zh-Hant"
    # a search that gave nothing falls back to the same refusal
    out, _ = run(monkeypatch, ja, cls("ja", out_of_scope=True, searchable=True), sources=False)
    assert out["language"] == "en" and out["warnings"] == ["search_unavailable"]


# ---- 3. a reaction asks the earlier question again -------------------------------------------------------------------
LIM_HISTORY = [{"role": "user", "text": "What is a LIM report?"}, {"role": "assistant", "text": DEFINITION_WEB}]


def test_a_reaction_to_a_searched_definition_is_searched_again(monkeypatch):
    out, fake = run(monkeypatch, "Are you sure?", cls(reaction=True), history=LIM_HISTORY)
    # the earlier answer came from a search (a definition, not a fixed sentence): the search is asked again directly,
    # so a fact about something else cannot replace it
    assert fake.tasks == ["classify", "search"] and fake.searches == 1
    assert 'QUESTION: "What is a LIM report?"' in fake.prompts("search")[0]
    assert out["web"]["text"] == DEFINITION_WEB and out["language"] == "en"


def test_a_reaction_in_chinese_keeps_the_asker_language_and_the_earlier_question(monkeypatch):
    text = "奧克蘭市議會說明，LIM 報告列出市議會對一處物業持有的資料。"
    out, fake = run(monkeypatch, "你確定嗎？", cls("zh-Hant", reaction=True), history=LIM_HISTORY, search_text=text)
    assert fake.searches == 1 and 'QUESTION: "What is a LIM report?"' in fake.prompts("search")[0]
    assert out["language"] == "zh-Hant"


def test_a_reaction_skips_earlier_bare_reactions(monkeypatch):
    history = [*LIM_HISTORY, {"role": "user", "text": "Why?"}, {"role": "assistant", "text": "Because."}]
    _, fake = run(monkeypatch, "Really?", cls(reaction=True), history=history)
    assert 'QUESTION: "What is a LIM report?"' in fake.prompts("search")[0]


def test_a_reaction_to_advice_is_still_advice(monkeypatch):
    history = [{"role": "user", "text": "Should I buy this house?"}, {"role": "assistant", "text": followup.REFUSAL_EN}]
    out, fake = run(monkeypatch, "Are you sure?", cls(reaction=True), history=history, search_text=ADVICE_WEB)
    assert fake.searches == 0 and fake.tasks == ["classify"] and out["answer"][0]["text"] == followup.REFUSAL_EN


def test_only_a_short_reaction_is_resolved(monkeypatch):
    long_q = "Are you sure the flood plain covers half of the parcel, given the maps I looked at?"
    _, fake = run(monkeypatch, long_q, cls(reaction=True), history=LIM_HISTORY)
    assert json.dumps(long_q) in fake.prompts("answer")[0]  # classify's flag alone does not replace a real question
    # code, not classify's flag, decides what is a bare reaction: without the flag it is resolved all the same
    _, fake = run(monkeypatch, "Are you sure?", cls(), history=LIM_HISTORY)
    assert 'QUESTION: "What is a LIM report?"' in fake.prompts("search")[0]


# ---- 2. "What is my LVR?" ----------------------------------------------------------------------------------------------
def lvr_fact(case="case1"):
    req = ReportRequest.model_validate(body(case))
    data = kb.load(req.selected_kb_ids())
    ev = V.build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
    return next(f for f in facts_prompt.followup_facts(req, ev, data.items, {}, personal_lvr=True)
                if f.topic == facts_prompt.PERSONAL_LVR), ev


def test_my_lvr_is_the_computed_ratio_with_its_source_and_is_never_searched(monkeypatch):
    f, _ = lvr_fact()
    assert f.source_ids == ["rbnz_lvr"] and f.slots["lvr_pct"].value == 90 and f.slots["lvr_pct"].kind == "pct"
    answer = {"answered": True, "answer": [{"text": f"Your LVR is {{{{{f.id}.lvr_pct}}}}.", "fact_ids": [f.id]}], "links": []}
    out, fake = run(monkeypatch, "What is my LVR?", cls(), answers=[answer])
    assert fake.tasks == ["classify", "answer"] and fake.searches == 0
    # the model only names the fact: the sentence shown is the program's own (prompt.PERSONAL_LVR_TEXT)
    # shown without a source (the figure is worked out from what the buyer entered), then what the Reserve Bank says
    # about a loan that high, with its sources
    assert [s["text"] for s in out["answer"]] == [LVR_SENTENCE, "Your loan is high-LVR lending (borrowing over 80% of the price), which the Reserve Bank lets banks offer only in limited amounts."]
    assert out["answer"][0]["source_ids"] == [] and out["answer"][1]["source_ids"] and out["warnings"] == []
    # in Chinese, the same ratio in the sentence code writes
    zh = {"answered": True, "answer": [{"text": f"你的 LVR 是{{{{{f.id}.lvr_pct}}}}。", "fact_ids": [f.id]}], "links": []}
    out, fake = run(monkeypatch, "我的 LVR 是多少？", cls("zh-Hant"), answers=[zh])
    assert fake.searches == 0 and out["language"] == "zh-Hant"
    assert [s["text"] for s in out["answer"]] == [facts_prompt.PERSONAL_LVR_ZH[0].format(v="90%")]


LVR_SENTENCE = "Your LVR, the loan as a share of the price, is 90%, calculated from the price and deposit you entered."


def test_the_ratio_is_allowed_only_in_the_program_sentence():
    f, ev = lvr_fact()
    assert f.program and not ev.typed_computed  # nothing is added to the request's shared evidence
    s = lambda text: V.check_sentence(V.Sentence(text=text, source_ids=["rbnz_lvr"]), ev)  # noqa: E731
    # under the source id in general, the computed ratio stays blocked whatever the words around it
    for text in ("Your LVR is 90%.", "Your LVR is 90% and your DTI is 90%.", "Your DTI is 90.",
                 "Your annual income is $90."):
        assert V.COMPUTED_VALUE in s(text), text
    assert V.NUMBER_NOT_IN_CITED_SOURCES in s("Your LVR is 91%.")
    # the program check allows it in the code-written sentence, with the fact's own slots
    prog = lambda text: facts_prompt.check_program(V.Sentence(text=text, source_ids=["rbnz_lvr"]), ev, f.slots)  # noqa
    assert f.draft() == LVR_SENTENCE and not prog(LVR_SENTENCE)
    assert V.COMPUTED_VALUE in prog("Your loan would be $720,000.")
    assert V.NUMBER_NOT_IN_CITED_SOURCES in prog("Your LVR is 91%.")


def test_a_model_sentence_that_gives_the_ratio_another_meaning_is_never_shown(monkeypatch):
    # Codex's re-review case: the same placeholder twice, the second labelled DTI. Naming the program fact gives its own
    # sentence; citing another fact cannot fill the placeholder; a typed number is a computed value
    f, _ = lvr_fact()
    for text, ids in ((f"Your LVR is {{{{{f.id}.lvr_pct}}}} and your DTI is {{{{{f.id}.lvr_pct}}}}.", [f.id]),
                      (f"Your LVR is {{{{{f.id}.lvr_pct}}}} and your DTI is {{{{{f.id}.lvr_pct}}}}.", ["F2"]),
                      ("Your LVR is 90% and your DTI is 90%.", ["F2"])):
        answer = {"answered": True, "answer": [{"text": text, "fact_ids": ids}], "links": []}
        out, fake = run(monkeypatch, "What is my LVR?", cls(), answers=[answer])
        texts = [s["text"] for s in out["answer"]]
        assert not any("DTI is 90" in t for t in texts), texts
        assert LVR_SENTENCE in texts and fake.searches == 0  # a blocked sentence's facts give their drafts


def test_lvr_questions_that_are_not_about_the_buyers_own_figure(monkeypatch):
    for q in ("What is my LVR?", "我的 LVR 是多少？", "What is my current LVR?"):
        assert followup.personal_lvr_question(q)
    for q in ("What is LVR?", "What is the LVR limit?", "What does my bank say about LVR?", "我想知道 LVR 是什麼"):
        assert not followup.personal_lvr_question(q)
    # "What is LVR?" gets no personal fact: a definition the facts do not hold is searched for, as before
    out, fake = run(monkeypatch, "What is LVR?", cls(), answers=[UNANSWERED], search_text="LVR is the loan-to-value ratio.")
    assert fake.searches == 1 and out["web"]["text"] == "LVR is the loan-to-value ratio."
    assert not any(facts_prompt.PERSONAL_LVR in c["prompt"] for c in fake.calls)


def test_my_lvr_without_loan_data_says_there_is_not_enough_data_and_does_not_search(monkeypatch):
    b = copy.deepcopy(body("edge1"))
    b["user_inputs"].pop("deposit", None)
    b["user_inputs"].pop("household_income", None)
    out, fake = run(monkeypatch, "What is my LVR?", cls(), b=b, answers=[UNANSWERED])
    assert fake.searches == 0 and "web" not in out
    assert out["answer"][0]["text"] == followup.INSUFFICIENT_EN and "not entered" in out["answer"][1]["text"]
