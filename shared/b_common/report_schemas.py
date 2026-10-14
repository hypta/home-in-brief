"""Request and response models for report-api (the Gemini layer).

The three input documents are kept whole: rules-api's narrow models (schemas.py) drop most of a
PropertyContext, but the report validator needs sources, hazards and every property field. Typed
read accessors cover what the validator and prompt read; everything else stays in the raw dicts.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer

REPORT_SCHEMA_VERSION = "0.1"


class _View(BaseModel):
    """A typed read-only view over part of an input document. Unknown keys are kept."""

    model_config = ConfigDict(extra="allow")


class SourceRef(_View):
    id: str
    title: Optional[str] = None


class LinkRef(_View):
    id: str
    label: Optional[str] = None


class HazardView(_View):
    layer: str
    priority: Optional[str] = None
    status: Optional[str] = None
    reason: Optional[str] = None
    overlap_pct: Optional[float] = None
    crosses_parcel: Optional[bool] = None
    classes: list[str] = Field(default_factory=list)
    susceptibility: Optional[str] = None
    source_id: Optional[str] = None
    data_date: Optional[str] = None


class FieldView(_View):
    """One `property.<field>` entry of a PropertyContext."""

    value: Any = None
    status: Optional[str] = None
    reason: Optional[str] = None
    source_id: Optional[str] = None


def _dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x: Any) -> list:
    return x if isinstance(x, list) else []


class ReportRequest(BaseModel):
    """Body of POST /v1/report. The three keys are required JSON objects, kept as sent."""

    model_config = ConfigDict(extra="allow")

    property_context: dict[str, Any]
    user_inputs: dict[str, Any]
    rules_output: dict[str, Any]

    # ---- PropertyContext ------------------------------------------------------------------
    def sources(self) -> list[SourceRef]:
        return [SourceRef.model_validate(s) for s in _list(self.property_context.get("sources"))
                if isinstance(s, dict) and isinstance(s.get("id"), str)]

    def links(self) -> list[LinkRef]:
        return [LinkRef.model_validate(s) for s in _list(self.property_context.get("links"))
                if isinstance(s, dict) and isinstance(s.get("id"), str)]

    def hazards(self) -> list[HazardView]:
        out = []
        for h in _list(self.property_context.get("hazards")):
            if isinstance(h, dict) and isinstance(h.get("layer"), str):
                try:
                    out.append(HazardView.model_validate(h))
                except ValueError:
                    continue
        return out

    def property_fields(self) -> dict[str, FieldView]:
        out = {}
        for name, raw in _dict(self.property_context.get("property")).items():
            if isinstance(raw, dict):
                try:
                    out[name] = FieldView.model_validate(raw)
                except ValueError:
                    continue
        return out

    # ---- UserInputs -----------------------------------------------------------------------
    def chat_supplied(self) -> dict[str, Any]:
        return {k: v for k, v in _dict(self.user_inputs.get("chat_supplied")).items() if v is not None}

    # ---- RulesOutput ----------------------------------------------------------------------
    def kb_items(self) -> list[str]:
        return [i for i in _list(self.rules_output.get("kb_items")) if isinstance(i, str)]

    def kb_items_from_user_input(self) -> list[str]:
        return [i for i in _list(self.rules_output.get("kb_items_from_user_input")) if isinstance(i, str)]

    def selected_kb_ids(self) -> list[str]:
        """kb_items ∪ kb_items_from_user_input, in order, without duplicates."""
        return list(dict.fromkeys(self.kb_items() + self.kb_items_from_user_input()))

    def warnings(self) -> list[str]:
        return [w for w in _list(self.rules_output.get("warnings")) if isinstance(w, str)]

    def flags(self) -> list[str]:
        return [f.get("id") for f in _list(self.rules_output.get("flags"))
                if isinstance(f, dict) and isinstance(f.get("id"), str)]

    def scenarios(self) -> list[dict]:
        return [s for s in _list(self.rules_output.get("scenarios")) if isinstance(s, dict)]

    def rules_source_ids(self) -> set[str]:
        """Every `source_ids` entry anywhere in RulesOutput (flags, scenarios, ...)."""
        found: set[str] = set()

        def walk(x: Any) -> None:
            if isinstance(x, dict):
                for k, v in x.items():
                    if k == "source_ids" and isinstance(v, list):
                        found.update(i for i in v if isinstance(i, str))
                    else:
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(self.rules_output)
        return found


class Sentence(BaseModel):
    text: str
    source_ids: list[str] = Field(default_factory=list)
    web: bool = False  # it states a value a web search found (the web prefix): checked as worded, the value is not

    @model_serializer(mode="wrap")
    def _web_only_when_true(self, handler):
        out = handler(self)
        if not self.web:
            out.pop("web", None)
        return out


class NextStep(BaseModel):
    text: str
    source_ids: list[str]
    kb_item: str


class NotFoundNote(BaseModel):
    field: str  # the codes, comma-separated, as before (for example "cv,year_built" or "hazards.flood_plain")
    text: str
    reason: Optional[str] = None
    # Added later, additive: the same fields as lists, so a page need not split `field` or turn codes into names.
    # `fields[i]` is a code, `labels[i]` its display name (sentence case, the words `text` uses, for example
    # "Council valuation (CV)", "Year built", "Flood plains"). Same order and length.
    fields: list[str] = []
    labels: list[str] = []


class Report(BaseModel):
    schema_version: str = REPORT_SCHEMA_VERSION
    generated_at: str
    model: str
    summary: list[Sentence]
    next_steps: list[NextStep]
    not_found_notes: list[NotFoundNote]
    warnings: list[str]
    dropped: int
