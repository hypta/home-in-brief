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

from b_common.report_schemas import ReportRequest  # noqa: E402

from app import kb  # noqa: E402
from app.validator import build_evidence  # noqa: E402

FIXTURES = HERE.parents[1] / "fixtures"
CASES = sorted(p.stem for p in (FIXTURES / "inputs").glob("*.json"))
REPO = HERE.parents[3]


def load_input(case: str) -> dict:
    return json.loads((FIXTURES / "inputs" / f"{case}.json").read_text(encoding="utf-8"))


def load_golden(writer: str, case: str) -> dict:
    return json.loads((FIXTURES / "golden" / writer / f"{case}.report.json").read_text(encoding="utf-8"))


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
    kb.clear_cache()
    yield
