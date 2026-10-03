"""rules-api: deterministic calculations and knowledge-base lookups."""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query

from b_common.schemas import (
    KbItem,
    RatesSnapshot,
    RulesOutput,
    RulesRequest,
    Source,
)

from . import calc
from .repo import get_repo
from .select import SelectContext, matches

app = FastAPI(title="rules-api", version="0.1.0")


def _include_draft() -> bool:
    return os.environ.get("INCLUDE_DRAFT", "").lower() in ("1", "true", "yes")


def _visible(items: list[KbItem]) -> list[KbItem]:
    allowed = {"verified", "draft"} if _include_draft() else {"verified"}
    return [i for i in items if i.status in allowed]


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/rules", response_model=RulesOutput)
def rules(req: RulesRequest) -> RulesOutput:
    repo = get_repo()
    ctx, ui = req.property_context, req.user_inputs
    out = calc.compute(ctx, ui, repo.rates())
    p = ctx.property
    sctx = SelectContext(
        title_type=p.title_type.value,
        hazards=[h.model_dump() for h in ctx.hazards],
        lvr_pct=out.lvr_pct,
        dti=out.dti,
        year_built=p.year_built.value if p.year_built.value is not None else ui.chat_supplied.year_built,
        cladding=p.cladding.value if p.cladding.value is not None else ui.chat_supplied.cladding,
    )
    out.kb_items = [i.id for i in _visible(repo.kb_items()) if matches(i.applies_when, sctx)]
    out.kb_includes_draft = _include_draft()
    return out


@app.get("/v1/kb/items", response_model=list[KbItem])
def kb_items(ids: str = Query("", description="comma-separated ids")) -> list[KbItem]:
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    items = _visible(get_repo().kb_items())
    if wanted:
        by_id = {i.id: i for i in items}
        return [by_id[i] for i in wanted if i in by_id]
    return items


@app.get("/v1/rates/latest", response_model=RatesSnapshot)
def rates_latest() -> RatesSnapshot:
    snap = get_repo().rates()
    if snap is None:
        raise HTTPException(404, "no rates snapshot available")
    return snap


@app.get("/v1/sources", response_model=list[Source])
def sources() -> list[Source]:
    return get_repo().sources()
