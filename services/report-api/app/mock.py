"""REPORT_MOCK=true: per-fixture summaries from the Claude golden reports, no Gemini call.

A request matches a fixture when its matched address and its user_inputs equal the fixture's.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from b_common.report_schemas import ReportRequest, Sentence

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _key(property_context: dict, user_inputs: dict) -> str:
    addr = (property_context.get("address") or {}).get("matched")
    ui = {k: v for k, v in user_inputs.items() if v is not None and v != {}}
    return json.dumps([addr, ui], sort_keys=True, default=str)


@lru_cache(maxsize=1)
def _index() -> dict[str, str]:
    out = {}
    for p in sorted((FIXTURES / "inputs").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out[_key(d["property_context"], d["user_inputs"])] = p.stem
    return out


def match_case(req: ReportRequest) -> Optional[str]:
    return _index().get(_key(req.property_context, req.user_inputs))


def summary_for(case_id: str) -> list[Sentence]:
    d = json.loads((FIXTURES / "golden" / "claude" / f"{case_id}.report.json").read_text(encoding="utf-8"))
    return [Sentence.model_validate(s) for s in d["summary"]]
