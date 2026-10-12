"""Four cases: a loan question the report has no loan facts for, near-duplicate First Home Loan
next steps, display names in not_found_notes, and the CODES.md wording for the two unavailable property codes."""
import copy

import pytest
from b_common.report_schemas import NotFoundNote
from conftest import CASES, REPO, body, request
from test_followup import ans, cls, fake, post
from test_followup import client as _client  # noqa: F401  (same app)

from app import assemble, followup, kb
from app.validator import build_evidence


def no_loan_details(case="edge1"):
    """The user has entered no deposit and no household income (edge1 has no price either)."""
    b = copy.deepcopy(body(case))
    b["user_inputs"].pop("deposit", None)
    b["user_inputs"].pop("household_income", None)
    return b


def post_body(b, question, **extra):
    return _client.post("/v1/followup", json=b | {"question": question, "history": []} | extra)


@pytest.fixture
def no_rules(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("rules-api must not be called")
    monkeypatch.setattr(followup, "call_rules", boom)


@pytest.fixture(autouse=True)
def search(monkeypatch):
    """Google Search is a fake: search.reply = WebAnswer | None (no answer, as when the search names no source)."""
    class S:
        reply = None
        prompts: list = []

    s = S()
    s.prompts = []
    monkeypatch.setattr(followup, "gemini_search", lambda prompt, timeout_s: s.prompts.append(prompt) or s.reply)
    return s


# ---- 1. a topic with no facts is answered "not enough data", never with an unrelated fact -----------------------
def test_a_loan_question_without_loan_details_is_searched_for_first(monkeypatch, no_rules, search):
    search.reply = followup.WebAnswer(text="Kāinga Ora describes the First Home Loan as a loan with a 5% deposit.",
                                      sources=[followup.WebSource(title="kaingaora.govt.nz", url="https://example.com/a")],
                                      search_suggestions="<div>chips</div>")
    gen = fake(cls(), [ans(("x", ["F1"]))])
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post_body(no_loan_details(), "What is a First Home Loan?").json()
    assert out["web"]["text"] == search.reply.text and out["answer"] == [] and out["out_of_scope"] is False
    assert len(search.prompts) == 1 and not gen.calls["answer"] and out["warnings"] == []


def test_first_home_loan_question_without_loan_details_says_it_cannot_answer(monkeypatch, no_rules):
    # the model would answer with the hazard fact F1: the program must not even ask it
    gen = fake(cls(searchable=True), [ans(("The council could not check these hazard layers.", ["F1"]))])
    monkeypatch.setattr(followup, "gemini_json", gen)
    r = post_body(no_loan_details(), "Can I get a First Home Loan?")
    assert r.status_code == 200, r.text
    out = r.json()
    text = " ".join(s["text"] for s in out["answer"])
    assert "does not have enough data to answer" in text
    assert "the price, the deposit and the household income were not entered" in text and "First Home Loan" in text
    assert not any(w in text.lower() for w in ("hazard", "flood", "landslide", "council", "title"))  # no unrelated fact
    assert all(s["source_ids"] == [] for s in out["answer"])
    assert out["language"] == "en" and out["out_of_scope"] is False and not gen.calls["answer"]
    assert out["warnings"] == [followup.SEARCH_UNAVAILABLE]  # the search was tried first and gave nothing


def test_missing_only_the_income(monkeypatch, no_rules):
    b = copy.deepcopy(body("case1"))
    b["user_inputs"].pop("household_income")
    b["rules_output"]["scenarios"] = [s for s in b["rules_output"]["scenarios"] if s["id"] != "first_home_loan"]
    b["rules_output"]["flags"] = []
    b["rules_output"]["kb_items"] = [i for i in b["rules_output"]["kb_items"] if not i.startswith("kb_fhl")]
    monkeypatch.setattr(followup, "gemini_json", fake(cls(), [ans(("x", ["F1"]))]))
    out = post_body(b, "Am I eligible for the First Home Loan?").json()
    assert any("the household income was not entered" in s["text"] for s in out["answer"])


def test_general_loan_question_without_loan_details(monkeypatch, no_rules):
    gen = fake(cls(), [ans(("The council could not check these hazard layers.", ["F1"]))])
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post_body(no_loan_details(), "How much would my mortgage repayments be?").json()
    assert "does not have enough data" in out["answer"][0]["text"] and "cannot assess the loan" in out["answer"][1]["text"]
    assert not gen.calls["answer"]


def test_cash_purchase_says_there_is_no_loan(monkeypatch, no_rules):
    monkeypatch.setattr(followup, "gemini_json", fake(cls(), [ans(("x", ["F1"]))]))
    out = post("edge2", "Can I use a First Home Loan?").json()
    assert "the deposit covers the whole price, so there is no loan" in out["answer"][1]["text"]


@pytest.mark.parametrize("lang,question,expect", [
    ("zh-Hant", "我可以申請 First Home Loan 嗎？", ("這裡沒有足夠的資料可以回答這個問題。", "價格、頭期款、家庭收入")),
    ("zh-Hans", "我可以申请 First Home Loan 吗？", ("这里没有足够的资料可以回答这个问题。", "价格、头期款、家庭收入")),
])
def test_chinese_fixed_sentences(monkeypatch, no_rules, lang, question, expect):
    gen = fake(cls(lang), [ans(("x", ["F1"]))])
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post_body(no_loan_details(), question).json()
    assert out["language"] == lang and out["answer"][0]["text"] == expect[0] and expect[1] in out["answer"][1]["text"]
    assert not gen.calls["answer"] and not gen.calls["translate"]  # written by code, no model call


JA = {"This page does not have enough data to answer this question.":
      "このレポートには、この質問に答えるためのデータが足りません。"}


def translate_ja(texts):
    return [JA.get(t, "First Home Loan を評価できません。価格、頭金、世帯収入が入力されていません。") for t in texts]


def test_other_language_is_translated_or_english_with_matching_language(monkeypatch, no_rules):
    monkeypatch.setattr(followup, "gemini_json", fake(cls("ja"), [ans(("x", ["F1"]))], translate=translate_ja))
    out = post_body(no_loan_details(), "First Home Loan は使えますか？").json()
    assert out["language"] == "ja" and out["answer"][0]["text"] == JA[followup.INSUFFICIENT_EN]
    # a translation that fails the checks (unchanged English) is not delivered: English, and the language says so
    monkeypatch.setattr(followup, "gemini_json", fake(cls("ja"), [ans(("x", ["F1"]))]))
    out = post_body(no_loan_details(), "First Home Loan は使えますか？").json()
    assert out["language"] == "en" and out["answer"][0]["text"] == followup.INSUFFICIENT_EN


def test_first_home_loan_question_with_loan_facts_is_still_answered(monkeypatch, no_rules):
    gen = fake(cls(), [ans(("A First Home Loan is shown because your income is within Kāinga Ora's cap.", ["F2"]))])
    monkeypatch.setattr(followup, "gemini_json", gen)
    r = post("case1", "Can I get a First Home Loan?")
    assert r.status_code == 200 and gen.calls["answer"]
    assert "does not have enough data" not in r.text


def test_fhl_question_when_only_other_loan_facts_exist(monkeypatch, no_rules):
    # case5: a loan with no First Home Loan result (the report has lending facts only)
    gen = fake(cls(), [ans(("x", ["F1"]))])
    monkeypatch.setattr(followup, "gemini_json", gen)
    out = post("case5", "Can I get a First Home Loan?").json()
    assert "no First Home Loan result" in out["answer"][1]["text"] and not gen.calls["answer"]
    gen2 = fake(cls(), [ans(("x", ["F1"]))])
    monkeypatch.setattr(followup, "gemini_json", gen2)
    post("case5", "What is my loan deposit?")  # a general loan question has lending facts: the model is asked
    assert gen2.calls["answer"]


def test_mixed_topic_question_is_left_to_the_model(monkeypatch, no_rules):
    gen = fake(cls(), [ans(("The council could not check these hazard layers.", ["F1"]))])
    monkeypatch.setattr(followup, "gemini_json", gen)
    post_body(no_loan_details(), "Does flood risk change my loan?")
    assert gen.calls["answer"]


# ---- 2. near-identical First Home Loan next steps ---------------------------------------------------------------
def steps(case="case1", cap=None, monkeypatch=None):
    req = request(case)
    data = kb.load(req.selected_kb_ids())
    ev = build_evidence(req, data.items, data.sources.keys())
    return assemble.next_steps(req, data.items, [], ev.no_loan)


def test_first_home_loan_criteria_steps_are_one(monkeypatch):
    monkeypatch.setattr(assemble, "MAX_NEXT_STEPS", 99)
    for case in ("case1", "case4", "edge3"):
        fhl = [s for s in steps(case) if "First Home Loan" in s.text]
        crit = [s for s in fhl if "criteria" in s.text]
        assert len(crit) == 1, [s.text for s in fhl]
        assert {"kb_fhl_deposit_5pct", "kb_fhl_lvr_not_restricted"} <= set(crit[0].source_ids)
        assert crit[0].kb_item in crit[0].source_ids
        assert len(fhl) == 2  # criteria, premium; the income-basis step does not name the First Home Loan
        assert len({s.text for s in steps(case)}) == len(steps(case))


def test_merge_keeps_the_more_complete_text_in_the_first_place():
    from b_common.report_schemas import NextStep
    a = NextStep(text="Check the First Home Loan criteria with a participating lender.", source_ids=["a"], kb_item="a")
    b = NextStep(text="Ask your lawyer to check the title.", source_ids=["b"], kb_item="b")
    c = NextStep(text="Check the First Home Loan income criteria with a participating lender.", source_ids=["c"],
                 kb_item="c")
    d = NextStep(text="Ask your lender how the First Home Loan premium would apply to your loan amount.",
                 source_ids=["d"], kb_item="d")
    out = assemble._merge_similar([a, b, c, d])
    assert [s.kb_item for s in out] == ["c", "b", "d"]
    assert out[0].source_ids == ["a", "c"] and "income criteria" in out[0].text


# ---- 3. not_found_notes carry the field names -------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES)
def test_every_note_has_field_codes_and_display_names(case):
    for n in assemble.not_found_notes(request(case)):
        assert n.fields and n.fields == n.field.split(",")
        assert len(n.labels) == len(n.fields) and all(lab.strip() for lab in n.labels)


def test_note_labels_are_the_names_the_text_uses():
    n = {x.field: x for x in assemble.not_found_notes(request("edge1"))}
    assert n["title_type,year_built"].labels == ["Title type", "Year built"]
    hz = n["hazards.flood_plain,hazards.flood_prone_area,hazards.overland_flow_path,hazards.coastal_inundation"]
    assert hz.labels[:2] == ["Flood plains", "Flood prone areas"]
    cv = next(x for x in assemble.not_found_notes(request("case2")) if "cv" in x.fields)
    assert cv.labels[0] == "Council valuation (CV)" and cv.field.startswith("cv,")


def test_new_note_fields_are_additive():
    old = NotFoundNote(field="cv", text="t")  # a producer that does not send them still validates
    assert old.fields == [] and old.labels == []
    assert set(old.model_dump()) == {"field", "text", "reason", "fields", "labels"}


# ---- 4. CODES.md: the two unavailable codes ----------------------------------------------------------------------
def test_codes_md_year_built_and_cladding_unavailable():
    rows = {}
    for line in (REPO / "CODES.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("| `year_built_unavailable`") or line.startswith("| `cladding_unavailable`"):
            rows[line.split("`")[1]] = line
    assert set(rows) == {"year_built_unavailable", "cladding_unavailable"}
    for code, line in rows.items():
        assert "Ask the user" not in line and "do not guess" in line.lower()
        assert "once" in line and "This home" in line and "form" in line, line


def test_chinese_fixed_sentences_do_not_call_the_output_a_report():
    # the page and the English sentences say "this tool" / "this page"; the Chinese ones must not say 報告 / 报告
    req = request("edge1")
    texts = [followup.REFUSAL_ZH, followup.MOCK_ANSWER_ZH, *followup.INSUFFICIENT_ZH]
    for kind in ("fhl", "loan"):
        _, zh_t, zh_s = followup.insufficient_texts(req, kind)
        texts += zh_t + zh_s
    assert len(texts) > 6 and not [t for t in texts if "報告" in t or "报告" in t]
    assert "本工具" in followup.REFUSAL_ZH
    no_result = followup.insufficient_texts(request("case1"), "fhl")[1][1]  # nothing missing: "no result for the details"
    assert no_result == "目前填寫的資料沒有得出First Home Loan結果。"
