"""Follow-up answers in the question's language (the report stays English).

English program sentences (program facts, comparison sentences, fact drafts) are written in Chinese by code or
translated by a restricted Gemini call that sees only the sentences; a translation that changes a number, adds a
URL or a domain, drops a negation or breaks a language rule is not used, and the whole sentence stays English.
Also: property-api sources with `accessed_at: null`.
"""
import copy
import json

import pytest
from b_common.report_schemas import ReportRequest
from conftest import body
from fastapi.testclient import TestClient
from test_leaky_user_facts import LEAKY, council_off

from app import followup, gemini, kb
from app import followup_prompt as fp
from app import prompt
from app.main import app
from app.validator import build_evidence

client = TestClient(app, raise_server_exceptions=False)

YEAR_EN = ("the year built you gave is 1998, a period Settled.govt.nz's guidance on leaky buildings covers; council "
           "records could not confirm it.")
YEAR_ZH = "你提供的建造年份是 1998 年，屬於 Settled.govt.nz 漏水屋指引涵蓋的時期；市議會紀錄無法確認。"
CLAD_ZH = "沒有免費的公開來源記錄外牆材質，因此無法確認；如果你知道，可以在表單中填寫。"


def fake(language, translate, answer=(), **classify):
    calls = {"translate": []}

    def gen(p, schema, timeout_s):
        if p.startswith(fp.TASK_CLASSIFY):
            return json.dumps({"language": language, "out_of_scope": False, "year_built": None, "cladding": None,
                               "hypothetical": False, "links": [], **classify})
        if p.startswith(fp.TASK_TRANSLATE):
            calls["translate"].append(p)
            return json.dumps({"texts": translate(json.loads(p.split("TEXTS:", 1)[1]))})
        return json.dumps({"answer": list(answer), "links": []})
    gen.calls = calls
    return gen


@pytest.fixture
def no_rules(monkeypatch):
    def rules(pc, ui, timeout):
        raise AssertionError("rules-api must not be called")
    monkeypatch.setattr(followup, "call_rules", rules)


def ask(monkeypatch, b, question, gen):
    monkeypatch.setattr(followup, "gemini_json", gen)
    r = client.post("/v1/followup", json=b | {"question": question})
    assert r.status_code == 200, r.text
    return r.json()


def test_year_program_fact_in_chinese_with_the_chinese_prefix(monkeypatch, no_rules):
    gen = fake("zh-Hant", lambda texts: [YEAR_ZH])
    out = ask(monkeypatch, council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY), "這間房子是哪一年蓋的？", gen)
    assert out["language"] == "zh-Hant"
    assert out["answer"][0] == {"text": "依你提供的資訊，" + YEAR_ZH, "source_ids": ["ac_property_page", "kb_leaky_build_era"]}
    t = gen.calls["translate"][0]
    assert "哪一年蓋" not in t and "The year built you gave is 1998" in t  # only the fixed sentence is sent


def test_cladding_program_fact_without_a_source_in_chinese(monkeypatch, no_rules):
    # the cladding question searches the web first; no key here, so the real wrapper would stop the request
    monkeypatch.setattr(followup, "gemini_search", lambda prompt, timeout_s: None)
    out = ask(monkeypatch, council_off(), "外牆是什麼材質？", fake("zh-Hant", lambda texts: [CLAD_ZH]))
    assert out["answer"] == [{"text": CLAD_ZH, "source_ids": []}]


@pytest.mark.parametrize("bad", [
    YEAR_ZH.replace("1998", "1999"),                              # a number changed
    YEAR_ZH.replace("無法確認", "已確認"),                          # the negation dropped
    YEAR_ZH + "詳見 https://evil.example。",                         # a URL added
    YEAR_ZH.replace("Settled.govt.nz", "evil.example"),          # a different domain
    YEAR_ZH.replace("市議會紀錄無法確認", "建議你請市議會確認，這間應該安全"),  # advice and verdict words
    YEAR_ZH * 3,                                                   # content added (too long)
    "The year built you gave is 1998; council records could not confirm it.",  # not translated
])
def test_translation_that_changes_the_sentence_falls_back_to_english(monkeypatch, no_rules, bad):
    out = ask(monkeypatch, council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY), "這間房子是哪一年蓋的？",
              fake("zh-Hant", lambda texts: [bad]))
    # no usable translation: the fixed-language decision (Chinese asker, no code-written Chinese for this sentence)
    assert out["language"] == "zh-Hant"
    assert [s["text"] for s in out["answer"]] == [followup.FALLBACK_NONE["zh-Hant"]]  # no English sentence, no mix


def test_wrong_number_of_translations_or_a_failed_call_keeps_english(monkeypatch, no_rules):
    b = council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY)
    out = ask(monkeypatch, b, "這間房子是哪一年蓋的？", fake("zh-Hant", lambda texts: [YEAR_ZH, YEAR_ZH, YEAR_ZH]))
    assert [s["text"] for s in out["answer"]] == [followup.FALLBACK_NONE["zh-Hant"]]

    def broken(texts):
        raise AssertionError  # never reached: the call itself fails below

    gen = fake("zh-Hant", broken)

    def failing(p, schema, timeout_s):
        if p.startswith(fp.TASK_TRANSLATE):
            raise gemini.ModelUnavailable("quota")
        return gen(p, schema, timeout_s)
    out = ask(monkeypatch, b, "這間房子是哪一年蓋的？", failing)
    assert [s["text"] for s in out["answer"]] == [followup.FALLBACK_NONE["zh-Hant"]]


def test_japanese_program_fact_and_translated_prefix(monkeypatch, no_rules):
    ja = "ご入力の建築年は 1998 年で、Settled.govt.nz の漏水住宅に関するガイダンスが扱う時期ですが、市議会の記録では確認できませんでした。"
    gen = fake("ja", lambda texts: [ja, "ご提供いただいた情報によると"])
    out = ask(monkeypatch, council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY), "この家は何年に建てられましたか？",
              gen)
    assert out["language"] == "ja"
    assert out["answer"][0]["text"] == "ご提供いただいた情報によると、" + ja
    assert "Based on what you told us" in gen.calls["translate"][0]


def test_japanese_comparison_sentence_is_translated(monkeypatch, no_rules):
    ja = "ご入力の建築年は 1985 年ですが、市議会の記録は 1990 年代（1990–1999）で、この回答は市議会の記録を使います。"
    out = ask(monkeypatch, body("case5"), "この家は1985年に建てられました。",
              fake("ja", lambda texts: [ja], year_built="1985"))
    assert out["answer"][0] == {"text": ja, "source_ids": ["ac_property_page"]}
    assert out["user_supplied"] == {"year_built": 1985}


def test_japanese_comparison_with_a_changed_year_stays_english(monkeypatch, no_rules):
    ja = "ご入力の建築年は 1986 年ですが、市議会の記録は 1990 年代（1990–1999）で、この回答は市議会の記録を使います。"
    out = ask(monkeypatch, body("case5"), "この家は1985年に建てられました。",
              fake("ja", lambda texts: [ja], year_built="1985"))
    # the translation failed the checks: English content, so `language` is en (fixed-language decision)
    assert out["language"] == "en" and out["answer"][0]["text"] == followup.FALLBACK_LEAD["en"]
    assert out["answer"][1]["text"] == ("You gave the year built as 1985; the council records the 1990s (1990–1999), "
                                        "and this answer uses the council's record.")


def test_translation_prompt_has_only_the_sentences_and_the_language():
    p = fp.translate_prompt(["No free public source records the cladding."], "zh-Hant")
    assert p.startswith(fp.TASK_TRANSLATE) and "QUESTION" not in p and "HISTORY" not in p
    assert '"zh-Hant"' in p and "Traditional" in p and "First Home Loan" in p


def test_report_stays_english_for_a_chinese_user(monkeypatch):
    """/v1/report has no language input: the summary prompt is unchanged (English)."""
    from app import prompt as summary_prompt
    req = ReportRequest.model_validate(body("case1"))
    data = kb.load(req.selected_kb_ids())
    ev = build_evidence(req, data.items, data.sources.keys())
    p = summary_prompt.build_prompt(req, ev, data.items, {})
    assert "zh-Hant" not in p and 'language "' not in p


# ---- property-api sources with accessed_at: null (58d0cbb) ------------------------------------------------
def _null_accessed(case):
    b = copy.deepcopy(body(case))
    for s in b["property_context"]["sources"]:
        s["accessed_at"] = None
    return b


@pytest.mark.parametrize("case", ["case1", "case5"])
def test_null_accessed_at_reads_and_validates(case):
    b = _null_accessed(case)
    assert all(s["accessed_at"] is None for s in b["property_context"]["sources"])
    req = ReportRequest.model_validate(b)
    assert {s.id for s in req.sources()} == {s["id"] for s in b["property_context"]["sources"]}
    data = kb.load(req.selected_kb_ids())
    ev = build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
    ref = ReportRequest.model_validate(body(case))
    ev_ref = build_evidence(ref, data.items, data.sources.keys(), source_urls=data.sources.urls)
    assert ev.known_ids == ev_ref.known_ids and ev.allowed == ev_ref.allowed
    assert [f.draft() for f in prompt.followup_facts(req, ev, data.items)] == \
        [f.draft() for f in prompt.followup_facts(ref, ev_ref, data.items)]


def test_null_accessed_at_report_and_followup_routes(monkeypatch):
    b = _null_accessed("case1")
    monkeypatch.setattr(gemini, "gemini_generate", lambda p, t: json.dumps({"summary": []}))
    r = client.post("/v1/report", json=b)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["summary"] and all(s["source_ids"] for s in rep["summary"] if s["text"])
    gen = fake("zh-Hant", lambda texts: texts, answer=[{"text": "市議會的 Flood Plains 圖層涵蓋這塊地的{{F1.flood_plain_pct}}。",
                                                        "fact_ids": ["F1"]}])
    monkeypatch.setattr(followup, "gemini_json", gen)
    r = client.post("/v1/followup", json=b | {"question": "淹水風險是什麼？"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"][0]["text"] == "市議會的 Flood Plains 圖層涵蓋這塊地的 51.2%。"


CLAD_USER_EN = ("Based on what you told us, the cladding you gave is monolithic, which Settled.govt.nz discusses in its "
                "guidance on leaky buildings; public data could not confirm it.")


@pytest.mark.parametrize("zh,kept", [
    ("你提供的外牆是 monolithic，Settled.govt.nz 在漏水屋指引中討論過這種外牆；公開資料無法確認。", True),
    ("你提供的外牆是整塊式，Settled.govt.nz 在漏水屋指引中討論過這種外牆；公開資料無法確認。", False),  # value translated
])
def test_translation_keeps_the_values_as_written(monkeypatch, no_rules, zh, kept):
    b = council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY)
    out = ask(monkeypatch, b, "外牆是什麼材質？",
              fake("zh-Hant", lambda texts: [zh if "cladding you gave" in t else t for t in texts]))
    if kept:
        assert out["answer"][0]["text"] == "依你提供的資訊，" + zh
    else:  # the translated value is not used and there is no other translation: the fixed Chinese decision
        assert [s["text"] for s in out["answer"]] == [followup.FALLBACK_NONE["zh-Hant"]]


def test_number_words_in_a_translation_are_blocked():
    ev = build_evidence(ReportRequest.model_validate(body("case1")), {}, [])
    orig = followup.Sentence(text="No existing debts were entered, so the DTI assumes none.", source_ids=["ac_flood_plains"])
    assert followup.check_translation(orig, "既存の債務は入力されなかったため、DTI はゼロとみなされます。", "ja", ev)
    assert not followup.check_translation(orig, "既存の債務は入力されなかったため、DTI は債務なしと仮定しています。", "ja", ev)


def test_a_cladding_given_in_this_question_does_not_also_get_the_no_source_fact():
    b = council_off({"cladding": "monolithic"}, LEAKY[1:])
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    ev = build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
    used = prompt.followup_facts(req, ev, data.items, {"cladding": "monolithic"})
    assert not any(f.topic == "program:cladding" for f in used)  # the user_said fact states it
    assert any(f.topic == "user_said" for f in used)
    before = prompt.followup_facts(req, ev, data.items, {})  # entered earlier: the program fact states it
    assert any(f.topic == "program:cladding" and "you gave" in f.text for f in before)


def test_a_rejected_translation_is_retried_once_with_the_words_to_copy(monkeypatch, no_rules):
    tries = []

    def tr(texts):
        tries.append(texts)
        return [YEAR_ZH.replace("1998", "1999") if len(tries) == 1 else YEAR_ZH]
    gen = fake("zh-Hant", tr)
    out = ask(monkeypatch, council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY), "這間房子是哪一年蓋的？", gen)
    assert out["answer"][0]["text"] == "依你提供的資訊，" + YEAR_ZH and len(tries) == 2
    assert "Copy each of these exactly as written" in gen.calls["translate"][1] and '"1998"' in gen.calls["translate"][1]
    assert "哪一年蓋" not in gen.calls["translate"][1]


@pytest.mark.parametrize("text,mixed", [
    ("Settled.govt.nz 指出 leaky 建築的指南中討論了此類外牆。", True),
    ("Settled.govt.nz 的漏水建築指南討論了 monolithic cladding。", True),
    ("市議會的 Flood Plains 圖層涵蓋這塊地的 51.2%，也有 overland flow path 穿過。", False),
    ("LINZ 記錄的產權是 Unit Title；Settled.govt.nz 指出業主會自動成為 body corporate 的成員。", False),
    ("你提供的外牆是 Roughcast, stucco or texture coat。", False),
])
def test_english_words_in_a_chinese_sentence_must_be_kept_terms_names_or_values(text, mixed):
    ev = build_evidence(ReportRequest.model_validate(body("case1")), {}, [])
    fails = followup.check_answer(followup.Sentence(text=text, source_ids=["ac_flood_plains"]), ev, "zh-Hant")
    assert (followup.MIXED_LANGUAGE in fails) == mixed


def test_decades_read_naturally_in_cjk():
    assert followup.tidy("建築物歸入 1990s 年代。", "zh-Hant") == "建築物歸入 1990 年代。"
    assert followup.tidy("建物は1990sに建てられた。", "ja") == "建物は 1990 年代に建てられた。"
    assert followup.tidy("built in the 1990s.", "en") == "built in the 1990s."


def test_a_chinese_rewrite_with_english_words_falls_back_to_the_translated_draft(monkeypatch):
    zh = "市議會地圖顯示 Flood Plains 涵蓋這塊地的 51.2%，Flood Prone Areas 涵蓋 50.8%，並有 overland flow path 穿過。"
    monkeypatch.setattr(followup, "call_rules", lambda *a: (_ for _ in ()).throw(AssertionError))
    gen = fake("zh-Hant", lambda texts: [zh], answer=[
        {"text": "市議會的 Flood Plains 圖層涵蓋這塊地的{{F1.flood_plain_pct}}，flooding is common here。", "fact_ids": ["F1"]}])
    out = ask(monkeypatch, body("case1"), "淹水風險是什麼？", gen)
    assert out["answer"][0]["text"] == zh and out["dropped"] == 1


# ---- the translation retry gets no usable answer: the fixed-language decision applies to the whole response ----

def _bad_then(error):
    """classify, answer, translate (fails the checks), then every later translate call raises `error` (None: returns [])."""
    inner = fake("ja", lambda texts: [t.replace("1998", "1999") for t in texts])  # first translate: a number changed
    state = {"translate": 0}

    def gen(p, schema, timeout_s):
        if p.startswith(fp.TASK_TRANSLATE):
            state["translate"] += 1
            if state["translate"] > 1 and error is not None:
                raise error
            if state["translate"] > 1:
                return json.dumps({"texts": []})
        return inner(p, schema, timeout_s)
    gen.state = state
    return gen


def _bad_then_zh(error):
    inner = fake("zh-Hant", lambda texts: [YEAR_ZH.replace("1998", "1999")])
    state = {"translate": 0}

    def gen(p, schema, timeout_s):
        if p.startswith(fp.TASK_TRANSLATE):
            state["translate"] += 1
            if state["translate"] > 1:
                raise error
        return inner(p, schema, timeout_s)
    gen.state = state
    return gen


@pytest.mark.parametrize("error", [gemini.ModelUnavailable("429 RESOURCE_EXHAUSTED", "quota"),
                                   gemini.ModelUnavailable("ReadTimeout", "timeout"),
                                   gemini.ModelUnavailable("503", "server_error")])
def test_japanese_retry_refused_or_failed_returns_english_labelled_en(monkeypatch, no_rules, error):
    gen = _bad_then(error)
    out = ask(monkeypatch, council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY),
              "この家は何年に建てられましたか？", gen)
    assert gen.state["translate"] >= 2  # the first translation failed the checks and a retry was attempted
    assert out["language"] == "en"
    assert out["answer"][0]["text"] == followup.FALLBACK_LEAD["en"]
    assert all(s["text"].isascii() for s in out["answer"])  # English content only, matching language=en


def test_chinese_retry_refused_matches_the_first_translation_refused_path(monkeypatch, no_rules):
    b = council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY)
    quota = gemini.ModelUnavailable("429 RESOURCE_EXHAUSTED", "quota")
    gen = _bad_then_zh(quota)
    out = ask(monkeypatch, b, "這間房子是哪一年蓋的？", gen)
    assert gen.state["translate"] >= 2  # the retry was attempted and refused

    inner = fake("zh-Hant", lambda texts: [YEAR_ZH])

    def first_refused(p, schema, timeout_s):
        if p.startswith(fp.TASK_TRANSLATE):
            raise quota
        return inner(p, schema, timeout_s)
    expected = ask(monkeypatch, b, "這間房子是哪一年蓋的？", first_refused)
    assert out["language"] == expected["language"] == "zh-Hant"
    assert out["answer"] == expected["answer"]
    assert out["answer"][0]["text"] in (followup.FALLBACK_LEAD["zh-Hant"], followup.FALLBACK_NONE["zh-Hant"])
    assert not any("the year built you gave" in s["text"] for s in out["answer"])  # no English program sentence


def test_japanese_retry_returning_nothing_is_the_same_decision(monkeypatch, no_rules):
    gen = _bad_then(None)  # the retry call answers with an empty list, i.e. no usable translation
    out = ask(monkeypatch, council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY),
              "この家は何年に建てられましたか？", gen)
    assert out["language"] == "en" and out["answer"][0]["text"] == followup.FALLBACK_LEAD["en"]


def test_japanese_first_translation_timeout_is_the_same_decision(monkeypatch, no_rules):
    inner = fake("ja", lambda texts: texts)

    def timing_out(p, schema, timeout_s):
        if p.startswith(fp.TASK_TRANSLATE):
            raise gemini.ModelUnavailable("ReadTimeout", "timeout")
        return inner(p, schema, timeout_s)
    out = ask(monkeypatch, council_off({"year_built": 1998, "cladding": "monolithic"}, LEAKY),
              "この家は何年に建てられましたか？", timing_out)
    assert out["language"] == "en" and out["answer"][0]["text"] == followup.FALLBACK_LEAD["en"]
