"""Fixed wording for summary drafts: short, attributed paraphrases of KB claims and plain-language term
definitions. Code builds every topic sentence from these, so Gemini only smooths wording and never has to
paraphrase a claim itself (REVIEW_real_gemini.md: free paraphrases became "rules do not apply").

Each paraphrase must stay true to the KB item's claim_en, keep its attribution, and be short enough that a
full sentence stays within validator.MAX_WORDS. Publisher names are followed by "says", "discusses" or
"guidance" so the validator's URL check accepts them.
"""
from __future__ import annotations

# kb id -> clause that follows "; " after the main fact.
KB_CLAUSE = {
    "kb_overland_flow_path": "Auckland Council says water can build up and enter homes if the path is blocked",
    "kb_coastal_inundation_meaning": "Auckland Council describes this as land the sea may cover in large storms",
    "kb_landslide_map_context": "Auckland Council says regional ratings do not measure risk on individual properties",
    "kb_title_freehold": "Settled.govt.nz says you own the land and generally anything built on it",
    "kb_title_cross_lease": ("Settled.govt.nz says you own a share of the freehold title with the other "
                             "cross leaseholders"),
    "kb_title_unit_title": "Settled.govt.nz says owners automatically become members of the body corporate",
}

# Plain-language definitions, used inline the first time a term appears.
LVR_FLAGGED = "high-LVR (borrowing over 80% of the price)"   # 80 comes from kb_rbnz_lvr, which is cited
DTI_FLAGGED = "high-DTI (debt over 6 times income)"          # 6 comes from kb_rbnz_dti, which is cited
LVR_PLAIN = "high-LVR (loan-to-value)"
DTI_PLAIN = "high-DTI (debt-to-income)"
# Definition alone, placed after "lending" when only one flag is on.
LVR_DEF, DTI_DEF = "borrowing over 80% of the price", "debt over 6 times income"

DEBTS_ASSUMED = "no existing debts were entered, so the DTI assumes none"
