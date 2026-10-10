import pytest
from b_common.report_schemas import Sentence
from conftest import CASES, evidence, load_golden

from app import validator as V

# Claude golden sentences that the validator drops on purpose: with no loan (10/10 decision) the program
# appends a fixed sentence and drops any model sentence about loans. The goldens were written before that.
CLAUDE_SUPERSEDED = {("edge1", 3): [V.LENDING_WITHOUT_LOAN], ("edge2", 3): [V.LENDING_WITHOUT_LOAN]}

# Codex golden sentences dropped for known problems (out/golden/COMPARISON_zh.md):
# internal jargon ("standard loan scenario", "rules assessment"), region-wide landslide classes below High
# (D5) and lending sentences in no-loan cases (D6).
CODEX_KNOWN = {
    ("case1", 2): [V.LANDSLIDE_RULE],
    ("case1", 3): [V.INTERNAL_JARGON],
    ("case1", 4): [V.INTERNAL_JARGON],
    ("case2", 3): [V.INTERNAL_JARGON],
    ("case2", 4): [V.INTERNAL_JARGON],
    ("case3", 3): [V.INTERNAL_JARGON],
    ("case4", 1): [V.BANNED_WORD, V.LANDSLIDE_RULE],  # "Unlikely" liquefaction class, Very Low landslide
    ("case4", 2): [V.INTERNAL_JARGON],
    ("case4", 3): [V.INTERNAL_JARGON],
    ("case5", 1): [V.BANNED_WORD, V.LANDSLIDE_RULE],
    ("edge1", 3): [V.LENDING_WITHOUT_LOAN],
    ("edge2", 3): [V.LENDING_WITHOUT_LOAN],
    ("edge3", 3): [V.INTERNAL_JARGON],
    ("edge3", 4): [V.INTERNAL_JARGON],
}


def _failures(writer, max_words):
    out = {}
    for case in CASES:
        _, _, ev = evidence(case)
        for n, s in enumerate(load_golden(writer, case)["summary"], 1):
            fails = V.check_sentence(Sentence(**s), ev, max_words)
            if fails:
                out[(case, n)] = fails
    return out


def test_claude_golden_passes_every_rule_except_superseded_no_loan_sentences():
    assert _failures("claude", None) == CLAUDE_SUPERSEDED


def test_claude_golden_length_is_the_only_extra_failure_at_25_words():
    # The goldens (10/05) allowed up to 40 words; the 25-word limit was decided on 10/10.
    fails = _failures("claude", V.MAX_WORDS)
    for key, f in fails.items():
        assert set(f) - {V.TOO_LONG} == set(CLAUDE_SUPERSEDED.get(key, [])), key
    assert sum(V.TOO_LONG in f for f in fails.values()) >= 30


def test_codex_golden_known_failures():
    assert _failures("codex", None) == CODEX_KNOWN


def chk(case, text, ids, max_words=V.MAX_WORDS):
    _, _, ev = evidence(case)
    return V.check_sentence(Sentence(text=text, source_ids=ids), ev, max_words)


@pytest.mark.parametrize("case,text,ids", [
    ("case1", "The council's Flood Plains layer covers 51.2% of this parcel.", ["ac_flood_plains"]),
    ("case1", "The council valued this property at $340,000 in 2024.", ["ac_valuation"]),
    ("case1", "Settled.govt.nz says an insurer may limit flood-damage cover in areas that flooded before.",
     ["kb_flood_insurance_limit"]),
    # kb claim numbers are valid even when one equals a computed value (80 vs the 80% LVR threshold here)
    ("case1", "The Reserve Bank says up to 25% of new loans can have an LVR above 80%.", ["kb_rbnz_lvr"]),
    ("case4", "The Kāinga Ora brochure lists a minimum deposit of 5% of the purchase price.", ["kb_fhl_deposit_5pct"]),
    ("case5", "The council's property category dates the building to the 1990s.", ["ac_property_page"]),
    ("case3", "A First Home Loan is not shown because your income is above the $95,000 single cap.", ["kainga_ora_fhl"]),
    ("case3", "The shallow landslide layer rates this area High; the council says this is not a property-level risk measure.",
     ["ac_landslide", "kb_landslide_map_context"]),
    ("case1", "Kāinga Ora says the Government will not fund part of the First Home Loan premium after 1 July 2025.",
     ["kainga_ora_fhl", "kb_fhl_govt_not_funding_premium"]),
    ("edge4", "LINZ records the title as Leasehold.", ["linz_titles"]),
])
def test_good_sentences_pass(case, text, ids):
    assert chk(case, text, ids) == []


@pytest.mark.parametrize("case,text,ids,code", [
    ("case1", "The council's Flood Plains layer covers 51.2% of this parcel.", ["made_up_source"], V.UNKNOWN_SOURCE_ID),
    ("case1", "Settled.govt.nz discusses leaky buildings.", ["kb_leaky_build_era"], V.KB_NOT_SELECTED),
    ("case1", "The council's Flood Plains layer covers 51.2% of this parcel.", [], V.NO_SOURCE_IDS),
    ("case1", "Your loan would be $720,000.", ["rbnz_lvr", "kb_rbnz_lvr"], V.COMPUTED_VALUE),
    ("case1", "Your LVR is 90%, above the Reserve Bank threshold.", ["rbnz_lvr"], V.COMPUTED_VALUE),
    ("case1", "Your DTI is 5.6.", ["rbnz_dti"], V.COMPUTED_VALUE),
    ("case1", "Repayments start at $4,129 a month.", ["bank_rates_snapshot"], V.COMPUTED_VALUE),
    ("case4", "Your deposit is 5% of the price.", ["kainga_ora_fhl"], V.COMPUTED_VALUE),
    ("case2", "The council's Flood Plains layer covers 0.0% of the parcel.", ["ac_flood_plains"], V.COMPUTED_VALUE),
    ("case1", "The council valued this property at $855,000.", ["ac_valuation"], V.NUMBER_NOT_IN_CITED_SOURCES),
    # 51.2 belongs to ac_flood_plains, not to the cited ac_flood_prone
    ("case1", "The council's Flood Prone Areas layer covers 51.2% of this parcel.", ["ac_flood_prone"],
     V.NUMBER_NOT_IN_CITED_SOURCES),
    ("case1", "The council valued this property in 2023.", ["ac_valuation"], V.NUMBER_NOT_IN_CITED_SOURCES),
    ("case1", "The Reserve Bank limits loans above 80% LVR.", ["rbnz_lvr"], V.NUMBER_NOT_IN_CITED_SOURCES),
    ("case5", "Based on what you told us, the building dates from 1998.", ["ac_property_page"],
     V.NUMBER_NOT_IN_CITED_SOURCES),
    ("case1", "The council valued the land at $4.9k per square metre.", ["ac_valuation"], V.ABBREVIATED_NUMBER),
    ("case1", "The council valued the land at 290 thousand dollars.", ["ac_valuation"], V.NUMBER_WORD),
    ("case1", "The council's Flood Plains layer covers about 51% of this parcel.", ["ac_flood_plains"], V.BANNED_WORD),
    ("case1", "The flood plain covers 51% of this parcel.", ["ac_flood_plains"], V.NUMBER_NOT_IN_CITED_SOURCES),
    ("case5", "This property looks safe from flooding.", ["ac_flood_plains"], V.BANNED_WORD),
    ("case5", "We recommend this property to first-home buyers.", ["ac_valuation"], V.BANNED_WORD),
    ("case5", "This is a good time to buy this unit.", ["ac_valuation"], V.BANNED_WORD),
    ("case5", "You should get a builder's report.", ["linz_titles"], V.BANNED_WORD),
    ("case5", "Values will rise in this suburb.", ["ac_valuation"], V.BANNED_WORD),
    ("case5", "The council layers show no risk of flooding.", ["ac_flood_plains"], V.BANNED_WORD),
    ("case1", "Kāinga Ora says the Government will fund the premium.", ["kainga_ora_fhl"], V.BANNED_WORD),
    ("case1", " ".join(["The council's Flood Plains layer covers this parcel"] * 4) + ".", ["ac_flood_plains"],
     V.TOO_LONG),
    ("case1", "See https://www.aucklandcouncil.govt.nz for the flood map.", ["ac_flood_plains"], V.URL_OR_HTML),
    ("case1", "See <a>the council flood map</a> for this parcel.", ["ac_flood_plains"], V.URL_OR_HTML),
    ("case1", "The lvr_pct field shows high-LVR lending.", ["rbnz_lvr"], V.INTERNAL_JARGON),
    ("case1", "The rules assessment marks this loan as high-LVR lending.", ["rbnz_lvr"], V.INTERNAL_JARGON),
    ("case1", "The landslide layer rates this area Very Low; the council says this is not a property-level measure.",
     ["ac_landslide", "kb_landslide_map_context"], V.LANDSLIDE_RULE),
    ("case3", "The council's shallow landslide layer gives this area a High rating.", ["ac_landslide"], V.LANDSLIDE_RULE),
    ("case1", "Auckland Council says landslide ratings do not measure risk on individual properties.",
     ["kb_landslide_map_context"], V.LANDSLIDE_RULE),
    ("case1", "The council's landslide map covers this area.", ["ac_valuation"], V.LANDSLIDE_RULE),
    ("edge2", "The Reserve Bank limits high-LVR lending.", ["rbnz_lvr"], V.LENDING_WITHOUT_LOAN),
    ("edge2", "No loan scenario applies to this purchase.", ["linz_titles"], V.LENDING_WITHOUT_LOAN),
    ("case1", "   ", ["ac_flood_plains"], V.EMPTY_TEXT),
])
def test_bad_sentences_are_dropped(case, text, ids, code):
    assert code in chk(case, text, ids)


def test_numbers_normalisation():
    assert V.numbers_in("NZ$1,450,000 and 51.2% in the 1990s, 1 July 2025") == [
        V.Decimal("1450000"), V.Decimal("51.2"), V.Decimal("1990"), V.Decimal("1"), V.Decimal("2025")]


def test_rounding_relaxation_uses_field_precision():
    _, _, ev = evidence("case1")
    ev.allowed["ac_flood_plains"] = [V.Allowed(V.Decimal("51.24"), V.P_PCT)]
    ok = Sentence(text="The Flood Plains layer covers 51.2% of this parcel.", source_ids=["ac_flood_plains"])
    bad = Sentence(text="The Flood Plains layer covers 51% of this parcel.", source_ids=["ac_flood_plains"])
    assert V.check_sentence(ok, ev) == []
    assert V.check_sentence(bad, ev) == [V.NUMBER_NOT_IN_CITED_SOURCES]


def test_validate_counts_dropped():
    _, _, ev = evidence("case1")
    v = V.validate([Sentence(text="LINZ records the title as Freehold.", source_ids=["linz_titles"]),
                    Sentence(text="Your loan is $720,000.", source_ids=["rbnz_lvr"])], ev)
    assert len(v.passed) == 1 and v.dropped == 1


@pytest.mark.parametrize("case,text,ids,expected", [
    ("case5", "The cladding is monolithic, which Settled.govt.nz discusses.", ["kb_monolithic_cladding"], True),
    ("edge4", "The year you gave differs from the council's property category, which gives 1975.",
     ["ac_property_page"], True),
    ("case5", "The building year you gave falls in the same decade.", ["ac_property_page"], True),
    ("case1", "LINZ records the title as Freehold.", ["linz_titles"], False),
    ("case1", "The deposit you provided puts this loan in high-LVR lending.", ["rbnz_lvr"], False),
])
def test_relies_on_user_input(case, text, ids, expected):
    _, _, ev = evidence(case)
    assert V.relies_on_user_input(Sentence(text=text, source_ids=ids), ev) is expected


def test_prefix_is_added_once():
    s = V.with_prefix(Sentence(text="The cladding is monolithic.", source_ids=["kb_monolithic_cladding"]))
    assert s.text == "Based on what you told us, the cladding is monolithic."
    again = V.with_prefix(Sentence(text="Based on what you told us, the cladding is monolithic.", source_ids=[]))
    assert again.text == s.text
    assert V.with_prefix(Sentence(text="LINZ lists it.", source_ids=[])).text == "Based on what you told us, LINZ lists it."
