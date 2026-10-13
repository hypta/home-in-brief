"""CJK year wordings in the field-relevance check; the flood-plain question; web-supplied year kept in the summary."""
import pytest
from test_followup import cls
from test_followup_routing_fixes import no_advice, texts
from test_followup_search_routes import ADVICE_WEB, UNANSWERED, no_network, run  # noqa: F401

from app import followup


@pytest.mark.parametrize("text", [
    "이 집은 1971년에 지어졌습니다.", "1970년대에 지어진 주택입니다.",
    "この家は1971年に建てられました。", "1970年代の建築です。",
    "這棟房子建於1971年。", "房子是1970年代建的。", "約1971年建成。"])
def test_cjk_year_wordings_are_on_topic_for_year_built(text):
    assert followup.web_unusable(text, fields=["year_built"]) is None


@pytest.mark.parametrize("text", [
    "이 지역은 교통이 편리합니다.", "この地域は交通が便利です。", "這個地區交通方便。", "Go ahead and purchase the house."])
def test_year_check_still_blocks_off_topic(text):
    assert followup.web_unusable(text, fields=["year_built"]) == "off_topic"


# ---- the flood-plain question asked in a buying context ---------------------------------------------------------------
@pytest.mark.parametrize("question, lang, declined", [
    ("Is a flood plain a problem when buying?", "en", followup.DECLINE_EN),
    ("買房子時，在淹水區有問題嗎？", "zh-Hant", followup.DECLINE_ZH[0])])
@pytest.mark.parametrize("case", ["case1", "case4"])
def test_flood_plain_question_when_buying_gets_the_flood_facts_and_the_decline(monkeypatch, question, lang, declined, case):
    # classify says no advice and the answer step finds no fact (the real failure: "not enough data")
    out, fake = run(monkeypatch, question, cls(lang), case=case, answers=[UNANSWERED], search_text=ADVICE_WEB)
    got = texts(out)
    assert fake.searches == 0 and "web" not in out and out["out_of_scope"] is False and "answer" not in fake.tasks
    assert got[0] == declined and followup.INSUFFICIENT_EN not in got
    assert any("flood" in t.lower() or "淹水" in t for t in got[1:])
    no_advice(out)


def test_should_i_buy_this_house_is_still_declined_without_a_search(monkeypatch):
    out, fake = run(monkeypatch, "Should I buy this house?", cls(), answers=[UNANSWERED], search_text=ADVICE_WEB)
    assert fake.searches == 0 and fake.tasks == ["classify"]
    assert out["out_of_scope"] is True and texts(out) == [followup.REFUSAL_EN]
