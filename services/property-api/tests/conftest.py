import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[3] / "shared")]
os.environ.setdefault("LINZ_API_KEY", "test")

from app import http  # noqa: E402

FIXTURES = HERE.parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


def replay(method, url, params=None, *, as_json=True):
    """Answer from the responses recorded by tests/record_fixtures.py; never touch the network."""
    entry = MANIFEST.get(http.signature(method, url, params))
    assert entry, f"no recorded response for {method} {url}"
    text = (FIXTURES / entry["file"]).read_text()
    return json.loads(text) if as_json else text


@pytest.fixture(autouse=True)
def recorded_upstreams(monkeypatch):
    monkeypatch.setattr(http, "request", replay)
