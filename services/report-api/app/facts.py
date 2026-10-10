"""Fixed wording and the fact model for summary and follow-up facts.

Code builds a list of facts (prompt.fact_list); each fact is a draft sentence whose values are placeholders
such as {{F1.flood_plain_pct}}. Gemini rewrites the facts in its own words but may write values only as
placeholders, which code fills in (validator.check_rewrite checks every rewrite). A blocked rewrite falls back
to the facts' drafts, so the must-mention facts are always in the summary.

The KB clauses are short, attributed paraphrases of KB claims (REVIEW_real_gemini.md: free paraphrases became
"rules do not apply"). Each must stay true to the KB item's claim_en, keep its attribution, and be short enough
that a full sentence stays within validator.MAX_WORDS. Publisher names are followed by "says", "discusses" or
"guidance" so the validator's URL check accepts them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field

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

# kb id -> the words of its clause a rewrite must keep, so the claim is not cut to something empty
# ("Settled.govt.nz says you own a share.").
KB_KEEP = {
    "kb_overland_flow_path": ["enter homes"],
    "kb_title_freehold": ["own the land"],
    "kb_title_cross_lease": ["share of the freehold title"],
    "kb_title_unit_title": ["members of the body corporate"],
    "kb_coastal_inundation_meaning": ["sea"],
}

# Plain-language definitions, used inline the first time a term appears.
# {{$.lvr_threshold}} and {{$.dti_multiple}} are slots filled from LVR_THRESHOLD_PCT and DTI_MULTIPLE, quoted
# from kb_rbnz_lvr / kb_rbnz_dti, which the fact cites.
LVR_FLAGGED = "high-LVR (borrowing over {{$.lvr_threshold}} of the price)"
DTI_FLAGGED = "high-DTI (debt over {{$.dti_multiple}} times income)"
LVR_PLAIN = "high-LVR (loan-to-value)"
DTI_PLAIN = "high-DTI (debt-to-income)"
# Definition alone, placed after "lending" when only one flag is on.
LVR_DEF, DTI_DEF = "borrowing over {{$.lvr_threshold}} of the price", "debt over {{$.dti_multiple}} times income"

DEBTS_ASSUMED = "no existing debts were entered, so the DTI assumes none"

# ---- fact list (rewrite design) -------------------------------------------------------------------
# Code builds a list of facts; Gemini rewrites them freely but writes every value as a placeholder
# ({{F1.flood_plain_pct}}) that code fills in. A fact's `text` is its draft sentence with placeholders: filled in,
# it is the fallback sentence used when Gemini's rewrite is blocked. The checks are in validator.check_rewrite.
PLACEHOLDER_RE = re.compile(r"\{\{\s*(F\d+)\s*\.\s*([A-Za-z0-9_]+)\s*\}\}")
DRAFT_ID = "$"  # facts are built with {{$.name}}; prompt.fact_list replaces "$" with the fact id

# Numbers that fixed wording quotes from a cited KB claim (checked again by validator.check_sentence).
LVR_THRESHOLD_PCT = 80      # kb_rbnz_lvr: "an LVR above 80%"
DTI_MULTIPLE = 6            # kb_rbnz_dti: "debt over 6 times income"
FHL_MIN_DEPOSIT_PCT = 5     # kb_fhl_deposit_5pct

# Title names: a string field value that must never appear outside a placeholder (unless the fact text itself
# says it, as in the cross-lease KB clause "a share of the freehold title").
TITLE_WORDS = ("Freehold", "Leasehold", "Cross Lease", "Unit Title", "Stratum in Freehold", "Stratum in Leasehold",
               "Supplementary Record Sheet", "Composite")


def _plain(d: Decimal) -> str:
    return format(d.normalize(), "f")


@dataclass
class Slot:
    """One value a fact may show. `kind` decides the format: pct 51.2%, money $95,000, decade 1990s, year 1975,
    int 6, text as given."""
    name: str
    value: Any
    kind: str
    what: str                     # plain description for the prompt (never the value)
    source_id: str
    label: Optional[str] = None   # words naming the value; the nearest label to the placeholder must be this one
    before: Optional[str] = None  # regex the text right before the placeholder must end with
    after: Optional[str] = None   # regex the text right after the placeholder must start with
    strict: bool = False          # the value must never appear in the text outside a placeholder

    def render(self) -> str:
        v = self.value
        if self.kind == "text":
            return str(v)
        d = Decimal(str(v))
        if self.kind == "pct":
            return _plain(d) + "%"
        if self.kind == "money":
            return f"${int(d):,}"
        if self.kind == "decade":
            return f"{int(d)}s"
        return _plain(d)


@dataclass
class Fact:
    id: str
    text: str                     # draft sentence with {{Fn.slot}} placeholders, without the user prefix
    source_ids: list[str]
    slots: dict[str, Slot] = field(default_factory=dict)
    topic: str = ""
    priority: int = 99            # lower is more important
    user: bool = False            # relies on chat-supplied facts: code adds "Based on what you told us, "
    must: bool = False            # a RUBRIC must-mention: always in the summary (draft if not rewritten)
    order: int = 0                # display order (RUBRIC 1)
    keep: list[str] = field(default_factory=list)  # extra phrases a rewrite must keep (besides validator.QUALIFIERS)
    program: bool = False         # follow-up only: shown as its draft, never rewritten (prompt.program_facts)

    def draft(self) -> str:
        return fill(self.text, {self.id: self})


def fill(text: str, facts: dict[str, "Fact"]) -> str:
    """Replace every known placeholder with its formatted value; unknown placeholders are left as written."""
    def rep(m: re.Match) -> str:
        f = facts.get(m.group(1))
        slot = f.slots.get(m.group(2)) if f else None
        return slot.render() if slot else m.group(0)
    return PLACEHOLDER_RE.sub(rep, text)


def template_words(text: str) -> str:
    """The fact text with placeholders removed (what the fact itself says in words)."""
    return PLACEHOLDER_RE.sub(" ", text)


class RewriteSentence(BaseModel):
    """One sentence Gemini returns: text with placeholders and the facts it uses."""
    text: str
    fact_ids: list[str] = Field(default_factory=list)
