"""Shared cladding table (shared/b_common/cladding.py) and its consistency with the web form."""
import re
from pathlib import Path

import pytest

from b_common import cladding as C

REPO = Path(__file__).resolve().parents[3]
INDEX_HTML = REPO / "services/web/app/static/index.html"

EXPECTED = ["aluminium", "brick", "concrete", "fibrous_cement", "glass", "iron", "fabric", "plastic", "monolithic",
            "stone", "tiles", "wood", "mixed"]


def test_thirteen_values_in_form_order():
    assert list(C.VALUES) == EXPECTED and len(set(C.VALUES)) == 13
    assert C.label("monolithic") == "Roughcast, stucco or texture coat"


def test_only_monolithic_is_monolithic():
    assert [c.value for c in C.CLADDINGS if c.is_monolithic] == ["monolithic"]
    assert C.is_monolithic("monolithic") and not C.is_monolithic("plastic") and not C.is_monolithic(None)
    assert not C.is_monolithic("fibrous_cement")


@pytest.mark.parametrize("text,value", [
    ("The walls are stucco", "monolithic"), ("roughcast", "monolithic"), ("plaster finish", "monolithic"),
    ("texture coat", "monolithic"), ("monolithic cladding", "monolithic"), ("外牆是灰泥", "monolithic"),
    ("weatherboard", "wood"), ("timber", "wood"), ("Wood", "wood"), ("木板", "wood"),
    ("brick veneer", "brick"), ("磚造", "brick"),
    ("fibre cement", "fibrous_cement"), ("fiber cement", "fibrous_cement"), ("Fibrolite", "fibrous_cement"),
    ("hardiflex sheets", "fibrous_cement"),
    ("EIFS", "plastic"), ("polystyrene", "plastic"),
    ("colorsteel", "iron"), ("metal", "iron"), ("steel", "iron"), ("corrugated iron", "iron"),
    ("concrete", "concrete"), ("aluminum", "aluminium"), ("glass", "glass"), ("stone", "stone"),
    ("clay tiles", "tiles"), ("bitumen", "fabric"), ("mixed", "mixed"),
    ("Roughcast, stucco or texture coat", "monolithic"), ("Iron or steel", "iron"), ("tiles", "tiles"),
])
def test_normalize_keywords(text, value):
    assert C.normalize(text) == value


@pytest.mark.parametrize("text", ["", None, "   ", "the cladding", "what is the cladding?", "外牆", "environment"])
def test_normalize_unknown_is_none(text):
    assert C.normalize(text) is None


def test_every_value_is_reachable_from_its_own_value_and_label():
    for c in C.CLADDINGS:
        assert C.normalize(c.value) == c.value and C.normalize(c.label) == c.value


def test_form_options_match_shared_table():
    html = INDEX_HTML.read_text(encoding="utf-8")
    sel = re.search(r'<select[^>]*id="cladding"[^>]*>(.*?)</select>', html, re.S)
    assert sel, "cladding <select> not found in services/web/app/static/index.html"
    options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', sel.group(1))
    form_values = [v for v, _ in options if v]
    assert form_values == list(C.VALUES), "web form cladding options differ from shared/b_common/cladding.py"
    assert {v: t.strip() for v, t in options if v} == {c.value: c.label for c in C.CLADDINGS}
