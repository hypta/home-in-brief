"""The parts of a report built by code: not_found_notes, next_steps, warnings and fixed sentences."""
from __future__ import annotations

import re
from typing import Optional

from b_common.report_schemas import NextStep, NotFoundNote, ReportRequest, Sentence

MAX_NEXT_STEPS = 6
MISSING = ("not_found", "unavailable")
PRIMARY_FLOOD_LAYERS = ("flood_plain", "flood_prone_area", "coastal_inundation")
LEAKY_KB = ("kb_leaky_build_era", "kb_monolithic_cladding")
HAZARD_DISCLAIMER = "This does not mean there is no hazard."

# ---- fixed sentences (no source id; exempt from provenance checks) ------------------------------
NO_LOAN_SENTENCES = {
    "price_missing": "No price was entered and the council valuation could not be used, "
                     "so no loan scenario could be calculated.",
    "deposit_exceeds_price": "Your deposit is larger than the price used, so no loan or repayment scenario "
                             "applies; check both amounts.",
    "cash_purchase_no_loan": "Your deposit equals the price, so no loan or repayment scenario applies.",
}
MOCK_SENTENCE = "This is a sample summary from the report service's mock mode; no model was called."


def no_loan_sentence(req: ReportRequest) -> Optional[Sentence]:
    warnings = req.warnings()
    for code, text in NO_LOAN_SENTENCES.items():
        if code in warnings:
            return Sentence(text=text, source_ids=[])
    return None


# ---- warnings -------------------------------------------------------------------------------------
_DROP_PREFIXES = ("lem_base_unverified:", "bank_skipped:", "kb_hash_mismatch:", "kb_verification_incomplete:",
                  "hazard_status_missing:", "hazard_status_unknown:", "hazard_priority_unknown:")


def reader_warnings(req: ReportRequest) -> list[str]:
    """rules_output.warnings without the panel-level and internal codes (CODES.md "not user-facing")."""
    out = [w for w in req.warnings() if not w.startswith(_DROP_PREFIXES)]
    if req.rules_output.get("kb_includes_draft") is True and "kb_includes_draft" not in out:
        out.append("kb_includes_draft")
    return list(dict.fromkeys(out))


# ---- not_found_notes ------------------------------------------------------------------------------
FIELD_LABELS = {
    "cv": "Council valuation (CV)", "title_type": "Title type", "year_built": "Year built",
    "cladding": "Cladding", "last_sale": "Last sale", "annual_rates": "Annual rates",
    "land_value": "Land value", "improvements_value": "Improvements value", "rateability": "Rateability",
    "improvement_description": "Improvement description", "land_use": "Land use", "land_area_m2": "Land area",
    "floor_area_m2": "Floor area", "building_site_coverage_m2": "Building site coverage",
    "legal_description": "Legal description", "record_of_title": "Record of title",
    "valuation_number": "Valuation number", "local_board": "Local board",
}
LAYER_LABELS = {
    "flood_plain": "Flood plains", "flood_prone_area": "Flood prone areas",
    "overland_flow_path": "Overland flow paths", "coastal_inundation": "Coastal inundation",
    "landslide_shallow": "Shallow landslide susceptibility", "landslide_large_scale": "Large-scale landslide susceptibility",
    "liquefaction": "Liquefaction", "tsunami_evacuation_zone": "Tsunami evacuation zone",
    "coastal_erosion": "Coastal erosion",
}
# (field, reason) -> text after "<Label>: ". Field-specific wording first, then by reason.
FIELD_REASON_TEXT = {
    ("year_built", "property_category_has_no_build_decade"):
        "the council's property category for this address has no build decade.",
    ("cladding", "no_free_source"): "no free public source has this; check the listing or ask the agent.",
    ("last_sale", "no_licensed_source"):
        "we have no licensed source for sale prices; see the Homes.co.nz and OneRoof links.",
    ("annual_rates", "non_rateable_property"): "the council lists this property as non-rateable.",
    ("floor_area_m2", "not_on_council_page"): "not shown on the council property page.",
    # Neutral on purpose: the same reason also comes with Leasehold and unresolved titles.
    ("land_area_m2", "council_shows_0_for_cross_lease_or_unit_title"):
        "the council entry does not provide a usable land area for this property.",
}
REASON_TEXT = {
    "no_unique_council_property_match": "the council records could not be matched to a single property at this address.",
    "service_timeout": "the data service did not respond in time, so this could not be checked.",
    # property-api reason when it skips the council property page on a cloud host (COUNCIL_PAGE=off). Neutral:
    # the data exists, we just could not read it from here.
    "council_site_refuses_this_server": "council property records could not be read from our server.",
    # What property-api sends for any read failure unless REASON_DETAIL=on.
    "source_unavailable": "could not be retrieved this time.",
}
STATUS_TEXT = {
    "not_found": "not found in the public records we checked.",
    "unavailable": "could not be retrieved this time.",
}
HAZARD_REASON_TEXT = {
    "service_timeout": "the council map service did not respond in time, so {it} could not be checked.",
    "source_unavailable": "could not be retrieved this time.",
}
HAZARD_STATUS_TEXT = {
    "unavailable": "could not be checked this time.",
    "not_found": "no result was returned for this parcel, so {it} could not be checked.",
}


def _label(field: str) -> str:
    return FIELD_LABELS.get(field) or field.removesuffix("_m2").replace("_", " ").capitalize()


def _join(labels: list[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _field_text(field: str, reason: Optional[str], status: str) -> str:
    return (FIELD_REASON_TEXT.get((field, reason or "")) or REASON_TEXT.get(reason or "")
            or STATUS_TEXT.get(status) or STATUS_TEXT["unavailable"])


COUNCIL_REFUSED = "council_site_refuses_this_server"
SOURCE_UNAVAILABLE = "source_unavailable"
# Fields read from the council property page (property-api context.py: year_built and _PAGE_FACTS), and the local
# board, which property-api now reads from GeoMaps with the page as fallback: unreadable is worded the same.
PAGE_FIELDS = ("year_built", "floor_area_m2", "building_site_coverage_m2", "local_board")
PAGE_UNREADABLE_TEXT = "council property records could not be retrieved."


def page_unreadable(field: str, reason: Optional[str]) -> bool:
    """A council-page field the server could not read (refused in development, source_unavailable live)."""
    return field in PAGE_FIELDS and reason in (COUNCIL_REFUSED, SOURCE_UNAVAILABLE)


def _council_hint(req: ReportRequest) -> str:
    """Form hint for the fields the user has not already entered; empty when they entered both."""
    given = req.chat_supplied()
    todo = [lab for key, lab in (("year_built", "the year built"), ("cladding", "cladding")) if key not in given]
    return f" You can enter {' and '.join(todo)} in the form if you know {'them' if len(todo) > 1 else 'it'}." if todo else ""


def not_found_notes(req: ReportRequest) -> list[NotFoundNote]:
    """One note per (template, reason): property fields and primary hazard layers whose status is
    not_found or unavailable. Fields sharing the same wording and reason are merged into one note;
    `field` then lists the fields, comma-separated."""
    groups: dict[tuple, dict] = {}

    def put(key: tuple, field: str, label: str, reason: str) -> None:
        g = groups.setdefault(key, {"fields": [], "labels": [], "reason": reason})
        g["fields"].append(field)
        g["labels"].append(label)

    for name, f in req.property_fields().items():
        if f.status in MISSING:
            text = _field_text(name, f.reason, f.status)
            if page_unreadable(name, f.reason):
                if f.reason == SOURCE_UNAVAILABLE:  # the real cause is only in the log: stay neutral
                    text = PAGE_UNREADABLE_TEXT
                text += _council_hint(req)
            put(("field", text, f.reason or f.status), name, _label(name), f.reason or f.status)
    for h in req.hazards():
        if h.priority == "primary" and h.status in MISSING:
            tmpl = HAZARD_REASON_TEXT.get(h.reason or "") or HAZARD_STATUS_TEXT.get(h.status) or HAZARD_STATUS_TEXT["unavailable"]
            label = LAYER_LABELS.get(h.layer) or h.layer.replace("_", " ").capitalize()
            put(("hazard", tmpl, h.reason or h.status), f"hazards.{h.layer}", label, h.reason or h.status)

    notes = []
    for (kind, text, _), g in groups.items():
        labels = g["labels"]
        if kind == "hazard":
            many = len(labels) > 1
            body = text.format(it="these layers" if many else "this layer", It="These layers" if many else "This layer")
            label = _join([labels[0]] + [lab[:1].lower() + lab[1:] for lab in labels[1:]])
            out = f"{label} {'layers' if many else 'layer'}: {body} {HAZARD_DISCLAIMER}"
        else:
            label = _join([labels[0]] + [lab if lab[1:2].isupper() or "(" in lab else lab[:1].lower() + lab[1:]
                                         for lab in labels[1:]])
            out = f"{label}: {text}"
        notes.append(NotFoundNote(field=",".join(g["fields"]), text=out, reason=g["reason"],
                                  fields=list(g["fields"]), labels=list(labels)))
    return notes


# ---- next_steps -----------------------------------------------------------------------------------
def _intersecting(req: ReportRequest) -> set[str]:
    return {h.layer for h in req.hazards() if h.priority == "primary" and h.status == "intersects"}


def _rank(item_id: str, item: dict, req: ReportRequest, layers: set[str]) -> int:
    """Program ordering after the items cited in the summary."""
    topic = item.get("topic")
    if item_id == "kb_flood_insurance_limit" and layers & set(PRIMARY_FLOOD_LAYERS):
        return 1
    if topic == "hazard" and item_id not in ("kb_flood_insurance_limit", "kb_lim_next_step", "kb_lim_definition",
                                           "kb_landslide_map_context"):
        return 2  # selected because a primary layer intersects (overland flow, coastal inundation)
    if topic in ("build_era", "cladding"):
        return 3
    if item_id in ("kb_rbnz_lvr", "kb_rbnz_dti"):
        return 4
    if item_id in ("kb_lim_next_step", "kb_lim_definition"):
        return 5
    if topic == "title_type" or item_id == "kb_unit_title_body_corporate_insurance":
        return 6
    if topic == "lending":
        return 7
    if item_id == "kb_auction_unconditional":
        return 9
    return 8


# Words every First Home Loan step has (or that only frame it): left out, what remains is what the step is about
# ("criteria", "premium", "income", ...).
_STEP_STOP = frozenset("a an the to of for and or with your you it its how what which that this their about at on in "
                       "is are be would will if any each ask check first home loan lender participating lending "
                       "application applications".split())
SIMILAR_STEP = 0.3  # share of topic words two First Home Loan steps have in common to count as the same next step


def _step_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STEP_STOP}


def _merge_similar(steps: list[NextStep]) -> list[NextStep]:
    """Merge First Home Loan steps that say nearly the same thing ("Check the First Home Loan criteria with a
    participating lender" and "... income criteria ..."; each KB item has its own wording). The earlier step keeps
    its place; it takes the more complete text (more content words), `kb_item` follows the text, `source_ids` is
    the union. Other steps are untouched: identical texts were merged already."""
    out: list[NextStep] = []
    for step in steps:
        words = _step_words(step.text)
        twin = next((o for o in out if "First Home Loan" in o.text and "First Home Loan" in step.text
                     and words and len(words & _step_words(o.text)) / len(words | _step_words(o.text)) >= SIMILAR_STEP),
                    None)
        if twin is None:
            out.append(step)
            continue
        twin.source_ids.extend(i for i in step.source_ids if i not in twin.source_ids)
        if len(words) > len(_step_words(twin.text)):
            twin.text, twin.kb_item = step.text, step.kb_item
    return out


def next_steps(req: ReportRequest, kb_items: dict[str, dict], summary: list[Sentence],
               no_loan: bool) -> list[NextStep]:
    """next_step_en of selected KB items, verbatim; items cited in the summary first, then program
    order; identical texts merged (kb_item = first, source_ids = all); at most MAX_NEXT_STEPS."""
    selected = [i for i in req.selected_kb_ids() if i in kb_items and kb_items[i].get("next_step_en")]
    if no_loan:
        selected = [i for i in selected if kb_items[i].get("topic") != "lending"]
    cited = list(dict.fromkeys(i for s in summary for i in s.source_ids if i in selected))
    layers = _intersecting(req)
    rest = sorted((i for i in selected if i not in cited),
                  key=lambda i: (_rank(i, kb_items[i], req, layers), selected.index(i)))
    order = cited + rest

    merged: dict[str, NextStep] = {}
    for i in order:
        text = kb_items[i]["next_step_en"]
        if text in merged:
            merged[text].source_ids.append(i)
        else:
            merged[text] = NextStep(text=text, source_ids=[i], kb_item=i)
    steps = _merge_similar(list(merged.values()))

    # Items that must be shown: flood insurance when a flood layer intersects, and a leaky-building item the
    # user's own input triggered (the summary must mention it). Each one past the cap takes the last free slot.
    pinned = (["kb_flood_insurance_limit"] if layers & set(PRIMARY_FLOOD_LAYERS) else []) + \
        [i for i in LEAKY_KB if i in req.kb_items_from_user_input()]
    for kb_id in pinned:
        idx = next((k for k, s in enumerate(steps) if kb_id in s.source_ids), None)
        if idx is None or idx < MAX_NEXT_STEPS:
            continue
        free = [k for k in range(MAX_NEXT_STEPS) if not set(pinned) & set(steps[k].source_ids)]
        if not free:
            continue
        item = steps.pop(idx)
        steps.insert(MAX_NEXT_STEPS, steps[free[-1]])  # the displaced item moves just past the cap
        steps[free[-1]] = item
    return steps[:MAX_NEXT_STEPS]
