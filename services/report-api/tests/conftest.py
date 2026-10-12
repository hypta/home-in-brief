import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[3] / "shared")]
# Tests never touch the network: KB from kb/seed, no real Gemini key.
os.environ["KB_SOURCE"] = "local"
os.environ.pop("REPORT_MOCK", None)
os.environ.pop("GEMINI_API_KEY", None)
# Protection: a per-process allowance in memory, never Firestore, unless a test chooses otherwise.
os.environ["PROTECTION_BACKEND"] = "memory"
os.environ.pop("REPORT_DEGRADED", None)

from b_common.report_schemas import ReportRequest  # noqa: E402

from app import cache, kb, protection  # noqa: E402
from app.validator import build_evidence  # noqa: E402

FIXTURES = HERE.parents[1] / "fixtures"
CASES = sorted(p.stem for p in (FIXTURES / "inputs").glob("*.json"))
REPO = HERE.parents[3]


def load_input(case: str) -> dict:
    return json.loads((FIXTURES / "inputs" / f"{case}.json").read_text(encoding="utf-8"))


def body(case: str) -> dict:
    d = load_input(case)
    return {k: d[k] for k in ("property_context", "user_inputs", "rules_output")}


def request(case: str) -> ReportRequest:
    return ReportRequest.model_validate(body(case))


def evidence(case: str):
    req = request(case)
    data = kb.load(req.selected_kb_ids())
    return req, data, build_evidence(req, data.items, data.sources.keys())


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KB_SOURCE", "local")
    monkeypatch.delenv("REPORT_MOCK", raising=False)
    monkeypatch.delenv("REPORT_DEGRADED", raising=False)
    monkeypatch.setenv("PROTECTION_BACKEND", "memory")
    for name in protection.DEFAULTS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPORT_CACHE_SIZE", "0")  # report cache off; tests/test_cache.py turns it on
    kb.clear_cache()
    protection.reset()
    cache.REPORTS.clear()
    yield
