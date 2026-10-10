"""Shared exterior-cladding table: the one list the web form and the report-api follow-up both use.

Source: LINZ Rating Valuations Rules 2008, Table 10 (13 wall materials), as used by teammate A's form
(services/web/app/static/index.html, the #cladding <select>). tests/test_cladding_consistency.py in report-api
fails if the form's option values drift from VALUES below.

Only "monolithic" (Roughcast, stucco or texture coat) counts as monolithic cladding; it is the only value that
selects kb_monolithic_cladding (kb/seed/cladding.json, applies_when cladding_in ["monolithic"]).

TO CONFIRM (not decided yet, both are treated as NOT monolithic for now):
  1. fibrous_cement: some fibre-cement systems are installed as direct-fixed or plastered sheets.
  2. plastic: EIFS / polystyrene-backed plaster is usually called a monolithic system.
If either is confirmed, change is_monolithic here and the kb applies_when together.

Chinese keywords are kept only to understand Chinese questions; nothing here outputs Chinese.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional


class Cladding(NamedTuple):
    value: str
    label: str  # text of the form's option
    is_monolithic: bool


CLADDINGS: tuple[Cladding, ...] = (
    Cladding("aluminium", "Aluminium", False),
    Cladding("brick", "Brick", False),
    Cladding("concrete", "Concrete", False),
    Cladding("fibrous_cement", "Fibrous cement", False),  # TO CONFIRM, see module docstring
    Cladding("glass", "Glass", False),
    Cladding("iron", "Iron or steel", False),
    Cladding("fabric", "Fabric, bitumen or butyl rubber", False),
    Cladding("plastic", "Plastic", False),  # TO CONFIRM (EIFS), see module docstring
    Cladding("monolithic", "Roughcast, stucco or texture coat", True),
    Cladding("stone", "Stone", False),
    Cladding("tiles", "Tiles", False),
    Cladding("wood", "Wood", False),
    Cladding("mixed", "Mixed or other", False),
)
VALUES: tuple[str, ...] = tuple(c.value for c in CLADDINGS)
_BY_VALUE = {c.value: c for c in CLADDINGS}

# Keywords per value (regex, case-insensitive). When one text matches several values, the order of this
# list decides: monolithic first (surfacing a possible leaky-building warning is the safe side).
_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("monolithic", r"\bmonolithic\b|\bmonolith\b|\broughcast\b|\bstucco\b|\bplaster(?:ed|work)?\b|"
                   r"\btexture[- ]?coat(?:ed|ing)?\b|\brender(?:ed)?\b|灰泥|整體式|單體|抹灰|粗灰泥"),
    ("fibrous_cement", r"\bfib(?:re|er|rous)[- ]cement\b|\bfibrolite\b|\bfibro\b|\bhardi\w*|\bhardiflex\b|纖維水泥|纤维水泥"),
    # TO CONFIRM: EIFS is kept under plastic until the monolithic question is decided
    ("plastic", r"\beifs\b|\bpolystyrene\b|\bplastic\b|\bvinyl\b|\bpvc\b|塑膠|塑料"),
    ("wood", r"\bweatherboards?\b|\bclapboard\b|\btimber\b|\bwood(?:en)?\b|\bcedar\b|木板|護牆板|木外牆|木造|木頭"),
    ("brick", r"\bbricks?\b|\bbrickwork\b|\bbrick veneer\b|磚"),
    ("concrete", r"\bconcrete\b|\btilt[- ]?slab\b|\bprecast\b|混凝土|水泥牆"),
    ("iron", r"\bmetal\b|\bsteel\b|\biron\b|\bcolou?r[- ]?steel\b|\bzincalume\b|\bcorrugated\b|鐵皮|鋼板|鐵|鋼"),
    ("aluminium", r"\balumini?um\b|\balloy\b|鋁"),
    ("glass", r"\bglass\b|\bglazed\b|玻璃"),
    ("fabric", r"\bfabric\b|\bbitumen\b|\bbutyl\b|\bmembrane\b|\bcanvas\b|瀝青|帆布"),
    ("stone", r"\bstone(?:work)?\b|\bschist\b|\bbluestone\b|石材|石頭|石牆"),
    ("tiles", r"\btiles?\b|\btiled\b|磁磚|瓷磚"),
    ("mixed", r"\bmixed\b|\bmultiple materials\b|\bcombination of\b|混合|多種材料"),
)
_COMPILED = tuple((v, re.compile(p, re.I)) for v, p in _KEYWORDS)
assert {v for v, _ in _KEYWORDS} == set(VALUES)


def label(value: str) -> str:
    """Display text for a value; an unknown value is returned as written."""
    c = _BY_VALUE.get(value)
    return c.label if c else value


def is_monolithic(value: Optional[str]) -> bool:
    c = _BY_VALUE.get(value or "")
    return bool(c and c.is_monolithic)


def find_all(text: str) -> list[str]:
    """Every value whose keyword appears in the text, in table priority order."""
    return [v for v, rx in _COMPILED if rx.search(text or "")]


def normalize(text: str) -> Optional[str]:
    """Free text -> a value from VALUES, or None. An exact value or label is accepted too."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    if low in _BY_VALUE:
        return low
    for c in CLADDINGS:
        if low == c.label.lower():
            return c.value
    hits = find_all(t)
    return hits[0] if hits else None
