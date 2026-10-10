"""Fact list + Gemini rewrite: validator.check_rewrite and rewrite.compose."""
import pytest
from b_common.report_schemas import Sentence
from conftest import CASES, evidence

from app import prompt, rewrite
from app import validator as V
from app.facts import Fact, Slot


def facts(case, followup=False):
    req, data, ev = evidence(case)
    fs = prompt.followup_facts(req, ev, data.items) if followup else prompt.fact_list(req, ev, data.items)
    return fs, ev


def check(case, text, ids, followup=False):
    fs, ev = facts(case, followup)
    return V.check_rewrite({"text": text, "fact_ids": ids}, fs, ev)


def by_text(fs, needle):
    return next(f for f in fs if needle in f.text)


# ---- the fact list --------------------------------------------------------------------------------
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("followup", [False, True])
def test_every_fact_text_passes_as_its_own_rewrite(case, followup):
    """The fact text itself is a valid rewrite, and no fact text has a digit outside a placeholder."""
    fs, ev = facts(case, followup)
    assert len({f.id for f in fs}) == len(fs)
    for f in fs:
        r = V.check_rewrite({"text": f.text, "fact_ids": [f.id]}, fs, ev, max_words=None)
        assert r.fails == [], (f.id, f.text, r.fails)
        assert r.sentence.source_ids == f.source_ids
        assert not any(c.isdigit() for c in prompt.facts.template_words(f.text)), f.text


@pytest.mark.parametrize("case", CASES)
def test_must_facts_are_the_summary_drafts_and_at_most_five(case):
    fs, ev = facts(case)
    musts = [f for f in fs if f.must]
    assert 1 <= len(musts) <= prompt.MAX_TOPICS
    for f in musts:
        assert V.check_sentence(Sentence(text=prompt.draft_text(f), source_ids=f.source_ids), ev) == []


def test_lending_and_user_input_meaning_facts_have_no_computed_values():
    fs, _ = facts("case2")
    topics = {f.topic for f in fs}
    assert {"lending", "first_home_loan", "user_input"} <= topics
    rate = by_text(fs, "interest rate you entered")
    assert not rate.slots and not rate.must
    assert by_text(fs, "cap for {{").slots["household"].value == "a single buyer with dependants"


# ---- check_rewrite: blocks ----------------------------------------------------------------------------
def test_placeholder_of_a_fact_the_sentence_does_not_name_is_blocked():
    r = check("case1", "LINZ records the title as {{F5.title_type}}; Settled.govt.nz says you own the land and "
                       "generally anything built on it.", ["F1"])
    assert V.FOREIGN_PLACEHOLDER in r.fails and r.sentence is None


def test_unknown_slot_and_unknown_fact_are_blocked():
    assert V.FOREIGN_PLACEHOLDER in check("case1", "The title is {{F5.year}}.", ["F5"]).fails
    assert V.UNKNOWN_FACT_ID in check("case1", "The title is set.", ["F99"]).fails
    assert V.NO_FACT_IDS in check("case1", "The title is set.", []).fails
    assert V.MALFORMED_PLACEHOLDER in check("case1", "The title is {F5.title_type}.", ["F5"]).fails


@pytest.mark.parametrize("text", [
    "Council maps show Flood Plains over 51.2% of this parcel, Flood Prone Areas over {{F1.flood_prone_area_pct}} "
    "and an overland flow path crossing it.",
    "Council maps show Flood Plains over {{F1.flood_plain_pct}} of this parcel in 2026, Flood Prone Areas over "
    "{{F1.flood_prone_area_pct}} and an overland flow path crossing it.",
    "Council maps show Flood Plains over ５１% of this parcel and an overland flow path crossing it.",
])
def test_bare_numbers_are_blocked(text):
    assert V.BARE_NUMBER in check("case1", text, ["F1"]).fails


def test_missing_negation_is_blocked():
    r = check("case4", "The council's flood plain, flood prone area, overland flow path and coastal inundation "
                       "layers show any of these for this parcel.", ["F1"])
    assert V.NEGATION_CHANGED in r.fails


def test_added_negation_is_blocked():
    r = check("case1", "A First Home Loan is not shown because your income is within Kāinga Ora's cap; it is exempt "
                       "from Reserve Bank LVR limits.", ["F3"])
    assert V.NEGATION_CHANGED in r.fails


def test_title_string_without_placeholder_is_blocked():
    r = check("case1", "LINZ records the title as Freehold; Settled.govt.nz says you own the land and generally "
                       "anything built on it.", ["F5"])
    assert V.RAW_FIELD_VALUE in r.fails
    r = check("case1", "The council valuation describes the improvements as reserve land and the land use as "
                       "{{F6.land_use}}; the year built could not be checked.", ["F6"])
    assert V.RAW_FIELD_VALUE in r.fails


def test_title_word_inside_the_fact_text_is_allowed():
    # the cross-lease KB clause itself says "freehold title"
    r = check("case3", "LINZ records the title as {{F5.title_type}}; Settled.govt.nz says you own a share of the "
                       "freehold title with the other cross leaseholders.", ["F5"])
    assert r.fails == []


def test_missing_qualifier_attribution_and_order_are_blocked():
    assert V.MISSING_QUALIFIER in check(
        "case3", "The council's shallow landslide layer rates this area {{F2.rating}}; Auckland Council says regional "
                 "ratings do not measure risk.", ["F2"]).fails  # "on individual properties" dropped
    assert V.MISSING_ATTRIBUTION in check(
        "case3", "The shallow landslide layer rates this area {{F2.rating}}, though regional ratings do not measure "
                 "risk on individual properties.", ["F2"]).fails
    assert V.QUALIFIER_ORDER in check(
        "case1", "A First Home Loan, it is exempt from Reserve Bank LVR limits, is shown because your income is "
                 "within Kāinga Ora's cap.", ["F3"]).fails
    assert V.MISSING_QUALIFIER in check(
        "case1", "A First Home Loan is shown because your income is within Kāinga Ora's cap; it is exempt from "
                 "Reserve Bank limits.", ["F3"]).fails  # "LVR" dropped widens the claim


def test_added_cause_and_added_says_are_blocked():
    assert V.ADDED_CAUSE in check(
        "case2", "A First Home Loan is shown because your income is within Kāinga Ora's cap, making it is exempt "
                 "from Reserve Bank LVR and DTI limits.", ["F6"]).fails
    assert V.ADDED_ATTRIBUTION in check(
        "edge1", "Auckland Council says its flood plain, flood prone area, overland flow path and coastal inundation "
                 "layers could not be checked, so these hazards are unknown.", ["F1"]).fails


def test_user_fact_merged_with_other_fact_is_blocked():
    r = check("case5", "The council dates the building to the {{F4.decade}}, a period covered by Settled.govt.nz's "
                       "leaky-building guidance, and the year you gave falls in the same decade.", ["F4", "F5"])
    assert V.MIXED_USER_FACTS in r.fails


def test_rules_of_check_sentence_still_apply_after_filling():
    assert V.BANNED_WORD in check("case1", "LINZ records the title as {{F5.title_type}}, so it is safe; "
                                           "Settled.govt.nz says you own the land and generally anything built on it.",
                                  ["F5"]).fails
    assert V.TOO_LONG in check(
        "case1", "LINZ records the title as {{F5.title_type}}; Settled.govt.nz says that you own the land and also "
                 "generally anything at all that has been built on it over the years.", ["F5"]).fails


# ---- check_rewrite: passes and fills --------------------------------------------------------------------
def test_merged_facts_are_filled_with_their_own_values():
    _, ev = facts("case1")
    fs = [Fact("F1", "The council's Flood Plains layer covers {{F1.pct}} of this parcel.", ["ac_flood_plains"],
               {"pct": Slot("pct", 51.2, "pct", "share", "ac_flood_plains", label="Flood Plains")}),
          Fact("F2", "LINZ records the title as {{F2.title_type}}.", ["linz_titles"],
               {"title_type": Slot("title_type", "Freehold", "text", "title", "linz_titles", strict=True)})]
    r = V.check_rewrite({"text": "The council's Flood Plains layer covers {{F1.pct}} of the parcel, and LINZ records "
                                 "the title as {{F2.title_type}}.", "fact_ids": ["F1", "F2"]}, fs, ev)
    assert r.fails == []
    assert r.sentence.text == ("The council's Flood Plains layer covers 51.2% of the parcel, and LINZ records the "
                               "title as Freehold.")
    assert r.sentence.source_ids == ["ac_flood_plains", "linz_titles"]


def test_case1_hazard_rewrite_and_formats():
    fs, ev = facts("case1")
    r = V.check_rewrite({"text": "On council maps, Flood Plains cover {{F1.flood_plain_pct}} of the parcel and Flood "
                                 "Prone Areas {{F1.flood_prone_area_pct}}, and an overland flow path crosses it.",
                         "fact_ids": ["F1"]}, fs, ev)
    assert r.fails == []
    assert "Flood Plains cover 51.2% of the parcel and Flood Prone Areas 50.8%" in r.sentence.text
    # a percentage whose nearest layer name is the other layer is blocked (number bound to the wrong field)
    swapped = V.check_rewrite({"text": "On council maps, Flood Prone Areas cover {{F1.flood_plain_pct}} of the parcel "
                                       "and Flood Plains {{F1.flood_prone_area_pct}}, and an overland flow path crosses "
                                       "it.", "fact_ids": ["F1"]}, fs, ev)
    assert V.SLOT_CONTEXT in swapped.fails
    fs3, _ = facts("case3")
    assert by_text(fs3, "income_cap").draft().endswith("for a single buyer, $95,000.")
    fs5, _ = facts("case5")
    assert "the 1990s" in by_text(fs5, "decade}}").draft()


def test_user_prefix_written_by_the_model_is_removed_and_flagged_by_the_fact():
    fs, ev = facts("edge4")
    r = V.check_rewrite({"text": "Based on what you told us, the cladding is monolithic, which Settled.govt.nz "
                                 "discusses in leaky guidance, though public data could not confirm it.",
                         "fact_ids": ["F6"]}, fs, ev)
    assert r.fails == [] and not r.sentence.text.startswith("Based on")
    assert fs[5].user


# ---- compose: fallback and must facts --------------------------------------------------------------------
GOOD_F1 = ("Council maps show Flood Plains over {{F1.flood_plain_pct}} and Flood Prone Areas over "
           "{{F1.flood_prone_area_pct}}, plus an overland flow path crossing it.")


def test_blocked_sentence_falls_back_to_its_fact_drafts():
    fs, ev = facts("case1")
    checked = [V.check_rewrite({"text": GOOD_F1, "fact_ids": ["F1"]}, fs, ev),
               V.check_rewrite({"text": "Your loan would be $720,000 and First Home Loans are great.",
                                "fact_ids": ["F2", "F3"]}, fs, ev)]
    assert checked[0].sentence and checked[1].sentence is None
    items = rewrite.compose(checked, fs, 5, add_musts=False)
    assert [it.kind for it in items] == ["rewrite", "draft", "draft"]
    assert items[1].sentence.text == fs[1].draft() and items[2].sentence.text == fs[2].draft()
    assert items[1].sentence.source_ids == fs[1].source_ids


def test_missing_must_facts_are_added_and_order_is_display_order():
    fs, ev = facts("case1")
    checked = [V.check_rewrite({"text": "LINZ records the title as {{F5.title_type}}, and Settled.govt.nz says you "
                                        "own the land and generally anything built on it.", "fact_ids": ["F5"]}, fs, ev)]
    items = rewrite.compose(checked, fs, 5)
    assert [it.fact_ids for it in items] == [["F1"], ["F2"], ["F3"], ["F5"], ["F6"]]
    assert [it.kind for it in items] == ["must_added"] * 3 + ["rewrite", "must_added"]
    st = rewrite.stats(items, checked)
    assert st == {"rewritten": 1, "fallback_drafts": 0, "must_added": 4, "blocked": 0, "model_sentences": 1}


def test_over_five_drops_optional_then_keeps_every_must():
    fs, ev = facts("case2")  # 6 must facts? no: 5 musts plus optional extras
    optional = [f for f in fs if not f.must]
    checked = [V.check_rewrite({"text": f.text, "fact_ids": [f.id]}, fs, ev) for f in fs]
    items = rewrite.compose(checked, fs, 5)
    assert len(items) == 5 and optional
    assert {i for it in items for i in it.fact_ids} == {f.id for f in fs if f.must}


def test_drafts_that_fail_the_final_check_are_never_shown():
    fs, ev = facts("case1")
    checked = [V.check_rewrite({"text": "bad 1", "fact_ids": ["F2"]}, fs, ev)]
    items = rewrite.compose(checked, fs, 5, add_musts=False, check_draft=lambda s: ["forced"])
    assert items == []


def test_user_fact_draft_is_marked_for_the_prefix():
    fs, ev = facts("edge4")
    items = rewrite.compose([], fs, 5)
    user = [it for it in items if it.user]
    assert [it.fact_ids for it in user] == [["F5"], ["F6"]]
    assert prompt.draft_text(fs[4]).startswith(V.PREFIX)


def test_council_refused_year_built_is_one_short_optional_fact():
    from conftest import body
    from b_common.report_schemas import ReportRequest
    from app import kb
    from app.validator import build_evidence
    b = body("case1")
    b["property_context"]["property"]["year_built"].update(value=None, status="unavailable",
                                                           reason="council_site_refuses_this_server")
    req = ReportRequest.model_validate(b)
    data = kb.load(req.selected_kb_ids())
    fs = prompt.fact_list(req, build_evidence(req, data.items, data.sources.keys()), data.items)
    hit = [f for f in fs if f.topic == "council_not_readable"]
    assert len(hit) == 1 and not hit[0].must and len(hit[0].text.split()) <= 22 and "in the form" in hit[0].text
    assert not any(f.topic == "not_checked" and "year built" in f.text for f in fs)
