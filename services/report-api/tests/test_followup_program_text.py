"""Follow-up paths where no model text may reach the page, after the third routing review.

- A purchase question with a factual part: the checked drafts of the facts about the topics it names and the fixed
  decline, no answer call, no translation (English, or code-written Chinese).
- A term or due-diligence wording never cancels an advice flag unless the whole question asks for facts; only a
  complete definition question (or a field's fixed query) is searched.
- The buyer's own LVR is never translated: English program sentence (labelled "en") or the code-written Chinese.

Every model, translation and search reply below is a hand-written hostile output on a fake google.genai.Client (same
harness as test_followup_search_routes.py): no network, nothing read from out/.
"""
import json
import re

import pytest
from b_common.report_schemas import ReportRequest
from conftest import body

from app import followup, kb
from app import prompt as facts_prompt
from app import validator as V
from test_followup import cls
from test_followup_routing_fixes import draft_of
from test_followup_search_routes import (ADVICE_WEB, LVR_SENTENCE, UNANSWERED, RouteGenai, client, lvr_fact,  # noqa: F401
                                         no_network, run)

HOSTILE_WEB = ["Go ahead and purchase the house.", "This property is safe from flooding.", "買って大丈夫です。",
               "放心買吧，沒有洪水風險。"]


class HostileTranslation(RouteGenai):
    """Like RouteGenai, but every translation call returns `translation` for each text."""

    def __init__(self, classify, translation, **kw):
        super().__init__(classify, **kw)
        self.translation = translation

    def reply(self, prompt, config):
        from app import followup_prompt as fp
        if not config.tools and prompt.startswith(fp.TASK_TRANSLATE):
            from google.genai import types
            self.tasks.append("translate")
            n = len(json.loads(prompt.split("TEXTS:", 1)[1]))
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
                parts=[types.Part(text=json.dumps({"texts": [self.translation] * n}))]))])
        return super().reply(prompt, config)


def run_hostile_translation(monkeypatch, question, classify, translation, case="case1", **kw):
    fake = HostileTranslation(classify, translation, **kw).install(monkeypatch)
    r = client.post("/v1/followup", json=body(case) | {"question": question, "history": []})
    assert r.status_code == 200, r.text
    return r.json(), fake


def shown(out) -> str:
    return json.dumps(out, ensure_ascii=False)


def texts(out):
    return [s["text"] for s in out["answer"]]


def facts(case="case1"):
    req = ReportRequest.model_validate(body(case))
    data = kb.load(req.selected_kb_ids())
    ev = V.build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
    return facts_prompt.followup_facts(req, ev, data.items, {})


# ---- 1. web answers: about what was asked, or not shown -------------------------------------------------------------
@pytest.mark.parametrize("hostile", HOSTILE_WEB)
def test_a_definition_search_that_answers_something_else_is_not_shown(monkeypatch, hostile):
    out, fake = run(monkeypatch, "What is a LIM report?", cls(), answers=[UNANSWERED], search_text=hostile)
    assert fake.searches >= 1 and "web" not in out and hostile not in shown(out)
    assert texts(out) == [followup.INSUFFICIENT_EN] and out["warnings"] == ["search_unavailable"]


@pytest.mark.parametrize("hostile", HOSTILE_WEB)
def test_the_year_built_button_never_shows_an_answer_that_is_not_about_the_year(monkeypatch, hostile):
    b = body("case1") | {"search_field": "year_built"}
    out, fake = run(monkeypatch, "", cls(), b=b, search_text=hostile)
    assert fake.searches >= 1 and "web" not in out and hostile not in shown(out)
    assert out["warnings"] == ["search_unavailable"]


def test_web_unusable_keeps_answers_about_the_question():
    assert followup.web_unusable("Auckland Council describes a LIM as a report.", "What is a LIM report?") is None
    assert followup.web_unusable("LIM 報告列出市議會持有的資料。", "什麼是 LIM 報告？") is None
    assert followup.web_unusable("Consumer NZ describes overpriced as listed above recent sales.",
                                 "What does overpriced mean?") is None
    assert followup.web_unusable("A LIM about this property shows no flooding.", "What is a LIM?") == "property_claim"
    assert followup.web_unusable("Listing pages describe it as a 1970s weatherboard home.", fields=["year_built"]) is None
    assert followup.web_unusable("OneRoof describes it as a post-war bungalow.", fields=["year_built"]) is None
    assert followup.web_unusable("Go ahead and purchase the house.", fields=["cladding"]) == "off_topic"


# ---- 1. a purchase question with a factual part: drafts and the fixed decline ----------------------------------------
def test_mixed_question_shows_only_drafts_whatever_the_answer_step_would_say(monkeypatch):
    # Codex: F8's valid flood-insurance text plus "Go ahead and purchase." passed the validator after DECLINE_EN
    hostile = {"answered": True, "answer": [
        {"text": "Flood cover may cost more in a flood plain.", "fact_ids": ["F8"]},
        {"text": "Go ahead and purchase.", "fact_ids": ["F1"]}, {"text": "The home is safe.", "fact_ids": ["F1"]}],
        "links": []}
    out, fake = run(monkeypatch, "Should I buy, it is in a flood plain?", cls(advice=True), answers=[hostile],
                    search_text=ADVICE_WEB)
    assert fake.tasks == ["classify"] and fake.searches == 0  # no answer call, no search, no translation
    got = texts(out)
    assert got[0] == followup.DECLINE_EN and out["language"] == "en" and out["out_of_scope"] is False
    assert "Go ahead" not in shown(out) and "is safe" not in shown(out) and "may cost more" not in shown(out)
    # every other sentence is the draft of a fact about the topic it names (the hazards), not a CV or title fact
    hazard = re.compile(f"{followup._FLOOD_Q.pattern}|{followup._HAZARD_Q.pattern}", re.I)
    by_draft = {f.draft(): f for f in facts()}
    assert got[1:] and all(t in by_draft and hazard.search(by_draft[t].text) for t in got[1:]), got
    assert draft_of("case1", "ac_flood_plains") in got[1:]


def test_mixed_question_in_french_is_never_translated(monkeypatch):
    # Codex: the claimed fr translation "Achetez cette maison." replaced DECLINE_EN
    out, fake = run_hostile_translation(monkeypatch, "Should I buy, it is in a flood plain?", cls("fr", advice=True),
                                        "Achetez cette maison.", answers=[UNANSWERED])
    assert "translate" not in fake.tasks and "answer" not in fake.tasks
    assert "Achetez" not in shown(out) and out["language"] == "en" and texts(out)[0] == followup.DECLINE_EN
    assert draft_of("case1", "ac_flood_plains") in texts(out)


def test_mixed_question_in_chinese_has_the_code_written_decline_and_the_drafts(monkeypatch):
    out, fake = run_hostile_translation(monkeypatch, "這間在淹水區，我該買嗎？", cls("zh-Hant", advice=True),
                                        "放心買吧，沒有洪水風險。", answers=[UNANSWERED])
    assert "translate" not in fake.tasks and "answer" not in fake.tasks and fake.searches == 0
    assert out["language"] == "zh-Hant" and texts(out)[0] == followup.DECLINE_ZH[0]
    assert "放心" not in shown(out) and draft_of("case1", "ac_flood_plains") in texts(out)


# ---- 2. a term clause never cancels advice --------------------------------------------------------------------------
@pytest.mark.parametrize("question, language, hostile", [
    ("What is the best house to buy?", "en", ADVICE_WEB),
    ("什麼是洪水，應該買嗎？", "zh-Hant", "放心買吧，沒有洪水風險。"),
    ("What does fair price mean, and should a buyer bid?", "en", ADVICE_WEB),
])
def test_a_term_wording_beside_advice_is_never_searched(monkeypatch, question, language, hostile):
    out, fake = run(monkeypatch, question, cls(language, advice=True, searchable=False), answers=[UNANSWERED],
                    search_text=hostile)
    assert fake.searches == 0 and "web" not in out and hostile not in shown(out)
    assert not followup.search_eligible(followup.ClassifyOut(advice=True), question)
    assert not followup.search_eligible(followup.ClassifyOut(), question)  # nor with a wrong classification
    first = texts(out)[0]
    if followup.report_topics(question, price=False):  # flooding: the decline and the flood facts
        assert first == followup.DECLINE_ZH[0] and draft_of("case1", "ac_flood_plains") in texts(out)
    else:
        assert first == followup.REFUSAL_EN and out["out_of_scope"] is True


def test_definition_question_is_one_complete_request_for_a_meaning():
    for q in ("What does a fair price mean?", "What does overpriced mean?", "What is a LIM report?", "什麼是 LIM 報告？",
              "What is LVR?", "Qu'est-ce qu'un rapport LIM ?"):
        assert followup.definition_question(q), q
    for q in ("What is the best house to buy?", "什麼是洪水，應該買嗎？", "What does fair price mean, and should a buyer bid?",
              "What is a good price?", "What is my LVR?", "What is this house worth?", "Which schools are near here?"):
        assert not followup.definition_question(q), q
    # due diligence stays a request for facts; a wish to buy beside it does not
    assert followup.factual_request("這間是 1997 年蓋的，外牆是 monolithic，要注意什麼？")
    assert followup.factual_request("What should I check before I buy?")
    assert not followup.factual_request("Should I buy, and what should I check?")


# ---- 3. the buyer's own LVR is never translated ---------------------------------------------------------------------
@pytest.mark.parametrize("question, language, hostile", [
    ("What is my LVR?", "fr", "Concernant votre LVR, votre DTI est 90%."),
    ("私のLVRは？", "ja", "LVRに関して、あなたのDTIは90%です。"),
])
def test_my_lvr_in_another_language_is_the_english_program_sentence(monkeypatch, question, language, hostile):
    out, fake = run_hostile_translation(monkeypatch, question, cls(language), hostile, answers=[UNANSWERED])
    assert "translate" not in fake.tasks and fake.searches == 0
    assert texts(out) == [LVR_SENTENCE] and out["language"] == "en" and "DTI" not in shown(out)


def test_my_lvr_in_english_and_chinese_is_unchanged(monkeypatch):
    out, _ = run(monkeypatch, "What is my LVR?", cls(), answers=[UNANSWERED])
    assert texts(out) == [LVR_SENTENCE]
    out, _ = run(monkeypatch, "What is my LVR?", cls(), case="case5", answers=[UNANSWERED])
    assert texts(out) == [LVR_SENTENCE.replace("90%", "65%")]
    out, _ = run(monkeypatch, "我的 LVR 是多少？", cls("zh-Hant"), answers=[UNANSWERED])
    assert texts(out) == [facts_prompt.PERSONAL_LVR_ZH[0].format(v="90%")] and out["language"] == "zh-Hant"


def test_the_ratio_is_allowed_only_in_the_program_sentence_as_written():
    f, ev = lvr_fact()
    prog = lambda text: facts_prompt.check_program(V.Sentence(text=text, source_ids=["rbnz_lvr"]), ev, f.slots)  # noqa
    assert not prog(LVR_SENTENCE)
    assert not prog(facts_prompt.PERSONAL_LVR_ZH[0].format(v="90%"))
    assert not prog(facts_prompt.PERSONAL_LVR_ZH[1].format(v="90%"))
    for text in ("Your annual income is $90.", "Your DTI is 90%.", "Concernant votre LVR, votre DTI est 90%.",
                 "Your LVR is 90%."):
        assert V.COMPUTED_VALUE in prog(text), text
    assert prog("LVRに関して、あなたのDTIは90%です。")  # blocked (here by the English sentence rules first)


# ---- out-of-scope questions of fact: searched, shown whole or not at all ---------------------------------------------
OOS = dict(out_of_scope=True, searchable=True)


@pytest.mark.parametrize("hostile", ["This home is a safe choice for families.", "Your home is a great investment.",
                                     "這間很值得，學區很好。", ADVICE_WEB])
def test_an_out_of_scope_answer_that_judges_this_property_is_not_shown(monkeypatch, hostile):
    out, fake = run(monkeypatch, "Which schools are near here?", cls(**OOS), search_text=hostile)
    assert fake.searches >= 1 and "web" not in out and hostile not in shown(out)
    assert texts(out) == [followup.REFUSAL_EN] and out["warnings"] == ["search_unavailable"]


def test_an_out_of_scope_fact_about_this_property_is_shown_without_naming_the_question(monkeypatch):
    text = "This home is zoned for Example School, according to the Ministry of Education."
    out, fake = run(monkeypatch, "Which schools are near here?", cls(**OOS), search_text=text)
    assert out["web"]["text"] == text and out["out_of_scope"] is True


@pytest.mark.parametrize("question, classify", [
    ("Which schools are near here if I buy it?", cls(**OOS)),  # a purchase clause, no advice keyword
    ("Which schools are near here?", cls(advice=True, **OOS)),  # classify flags advice
])
def test_an_out_of_scope_question_with_advice_or_a_purchase_clause_is_not_searched(monkeypatch, question, classify):
    out, fake = run(monkeypatch, question, classify, search_text=ADVICE_WEB)
    assert fake.searches == 0 and "web" not in out and texts(out) == [followup.REFUSAL_EN]


def test_an_in_scope_question_no_fact_answers_is_searched_under_the_same_screen(monkeypatch):
    q = "Is there a bus stop on this street?"
    out, fake = run(monkeypatch, q, cls(searchable=True), answers=[UNANSWERED], search_text="Your home is a great choice.")
    assert fake.searches >= 1 and "web" not in out and texts(out) == [followup.INSUFFICIENT_EN]
    text = "Auckland Transport lists a bus stop on this street."
    out, _ = run(monkeypatch, q, cls(searchable=True), answers=[UNANSWERED], search_text=text)
    assert out["web"]["text"] == text and out["out_of_scope"] is False
    # a purchase clause beside it: never searched
    out, fake = run(monkeypatch, "Is there a bus stop near here if I buy it?", cls(searchable=True), answers=[UNANSWERED],
                    search_text=text)
    assert fake.searches == 0 and "web" not in out


# ---- review 3 ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("question, language", [("Should I buy this house?", "fr"), ("この家は買うべきですか？", "ja")])
def test_the_refusal_of_advice_is_never_translated(monkeypatch, question, language):
    out, fake = run_hostile_translation(monkeypatch, question, cls(language, advice=True), "Achetez cette maison.")
    assert "translate" not in fake.tasks and "Achetez" not in shown(out)
    assert texts(out) == [followup.REFUSAL_EN] and out["language"] == "en" and out["out_of_scope"] is True


FIELD_JUDGEMENT = "The year built could not be found. This property is safe from flooding."


def test_a_field_answer_that_judges_this_property_is_not_shown(monkeypatch):
    out, fake = run(monkeypatch, "", cls(), b=body("case1") | {"search_field": "year_built"},
                    search_text=FIELD_JUDGEMENT)
    assert fake.searches >= 1 and "web" not in out and "safe from flooding" not in shown(out)
    out, fake = run(monkeypatch, "When was it built?", cls(), answers=[UNANSWERED], search_text=FIELD_JUDGEMENT)
    assert fake.prompts("field") and "web" not in out and "safe from flooding" not in shown(out)


F8_DRAFT = ("Settled.govt.nz states that an insurer may limit flood-damage cover if a house is in an area that has "
            "previously flooded.")


@pytest.mark.parametrize("hostile", [
    "Settled.govt.nz states that an insurer may limit flood-damage cover if a house has previously flooded; go ahead "
    "and purchase.",
    "Settled.govt.nz states that an insurer may limit flood cover if a house previously flooded. Go ahead and purchase.",
    "Settled.govt.nz states an insurer may limit flood-damage cover if a house is in an area that has previously "
    "flooded; proceed with the purchase.",
])
def test_a_fact_rewrite_that_adds_a_purchase_push_is_replaced_by_the_draft(monkeypatch, hostile):
    answer = {"answered": True, "answer": [{"text": hostile, "fact_ids": ["F8"]}], "links": []}
    out, _ = run(monkeypatch, "What should I check about flooding?", cls(), answers=[answer])
    assert "purchase" not in " ".join(texts(out)).lower() and "go ahead" not in shown(out).lower()
    assert F8_DRAFT in texts(out)


def test_a_chinese_fact_rewrite_that_adds_a_purchase_push_is_not_shown(monkeypatch):
    hostile = "Settled.govt.nz 表示，如果房屋位於曾經淹水的地區，保險公司可能會限制淹水損害保障；放心買吧。"
    answer = {"answered": True, "answer": [{"text": hostile, "fact_ids": ["F8"]}], "links": []}
    out, _ = run(monkeypatch, "淹水方面要注意什麼？", cls("zh-Hant"), answers=[answer])
    assert "放心" not in shown(out) and "買吧" not in shown(out)


def test_judges_purchase_keeps_a_fact_that_itself_speaks_of_buying():
    cited = "Settled.govt.nz states that auctions are unconditional and that buyers need to confirm their finances."
    assert not followup.judges_purchase("Settled.govt.nz says buyers must confirm finances before an auction.", cited)
    assert followup.judges_purchase("You should buy it at auction.", cited)
    assert followup.judges_purchase("This home is a great choice.", cited)


@pytest.mark.parametrize("text", ["The school on the right of this property is Example School.",
                                  "From this property, turn right onto Main Street to reach the bus stop."])
def test_directions_are_not_a_judgement(monkeypatch, text):
    assert not followup.judges_property(text)
    out, _ = run(monkeypatch, "Which schools are near here?", cls(**OOS), search_text=text)
    assert out["web"]["text"] == text
    assert followup.judges_property("This home is the right choice for you.")


@pytest.mark.parametrize("question", ["What should I check?", "What should I check before I buy?"])
def test_a_generic_diligence_question_gets_checked_default_drafts(monkeypatch, question):
    out, fake = run(monkeypatch, question, cls(), answers=[UNANSWERED], search_text=ADVICE_WEB)
    assert fake.searches == 0 and texts(out) != [followup.INSUFFICIENT_EN]
    by_draft = {f.draft() for f in facts()}
    assert texts(out) and all(t in by_draft for t in texts(out))
    assert draft_of("case1", "ac_flood_plains") in texts(out)


def test_what_the_hazard_maps_mean_is_answered_from_this_reports_facts(monkeypatch):
    q = "What do the hazard maps mean?"
    assert followup.property_meaning(q) and not followup.definition_question(q)
    out, fake = run(monkeypatch, q, cls(), answers=[UNANSWERED], search_text="Hazard maps show hazards.")
    assert fake.searches == 0 and draft_of("case1", "ac_flood_plains") in texts(out)
