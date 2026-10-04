"""rules-api: deterministic calculations and knowledge-base lookups."""
from __future__ import annotations

import os

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from b_common.content_hash import content_hash
from b_common.schemas import (
    ErrorResponse,
    KbItem,
    RatesSnapshot,
    RulesOutput,
    RulesRequest,
    Source,
)

from . import calc
from .repo import get_repo
from .select import SelectContext, matches

log = logging.getLogger("rules-api")
app = FastAPI(title="rules-api", version="0.1.0")
# Declared so the OpenAPI document shows the real error shape, not FastAPI's default "detail".
ERRORS = {c: {"model": ErrorResponse, "description": d} for c, d in {
    405: "Method not allowed", 500: "Internal server error"}.items()}
ERRORS_422 = {422: {"model": ErrorResponse, "description": "Validation error"}, **ERRORS}


def _include_draft() -> bool:
    return os.environ.get("INCLUDE_DRAFT", "").lower() in ("1", "true", "yes")


def _visible(items: list[KbItem], warnings: Optional[list[str]] = None) -> list[KbItem]:
    """Items to serve. A verified item whose content no longer matches the hash that was
    reviewed counts as unreviewed: it is dropped and reported."""
    allowed = {"verified", "draft"} if _include_draft() else {"verified"}
    out = []
    for i in items:
        if i.status not in allowed:
            continue
        if i.status == "verified" and not (i.verified_by and i.verified_at):
            log.warning("kb item %s is verified without verified_by/verified_at", i.id)
            if warnings is not None:
                warnings.append(f"kb_verification_incomplete:{i.id}")
            continue
        if i.status == "verified" and i.verified_content_hash != content_hash(i.model_dump()):
            log.warning("kb hash mismatch for %s", i.id)
            if warnings is not None:
                warnings.append(f"kb_hash_mismatch:{i.id}")
            continue
        out.append(i)
    return out


@app.get("/healthz", responses=ERRORS)
@app.get("/health", responses=ERRORS)  # Cloud Run reserves some paths ending in "z"; use /health when deployed
def healthz() -> dict:
    return {"status": "ok"}


def _error(status: int, code: str, message: str, fields: Optional[list[dict]] = None) -> JSONResponse:
    """The single error shape for every 4xx/5xx: {"error": {code, message, fields}}."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "fields": fields or []}},
    )


@app.exception_handler(RequestValidationError)
def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # "input" and "ctx" are dropped: echoing NaN or Infinity back is not valid JSON
    fields = [
        {"field": ".".join(str(x) for x in e["loc"] if x != "body"), "message": e["msg"]}
        for e in exc.errors()
    ]
    return _error(422, "validation_error", "Request validation failed.", fields)


@app.exception_handler(calc.InputError)
def _input_error(_: Request, exc: calc.InputError) -> JSONResponse:
    return _error(422, "validation_error", "Request validation failed.",
                  [{"field": f"user_inputs.{exc.field_name}", "message": exc.reason}])


@app.exception_handler(StarletteHTTPException)
def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(Exception)
def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return _error(500, "internal_error", "Internal server error.")


def _safe(fn, default, warnings: list[str], tag: str):
    """Run a data-source read; on failure keep serving and record why."""
    try:
        return fn(), True
    except Exception as e:
        log.warning("%s failed: %s", tag, e)
        warnings.append(tag)
        return default, False


@app.post("/v1/rules", response_model=RulesOutput, responses=ERRORS_422)
def rules(req: RulesRequest) -> RulesOutput:
    repo = get_repo()
    ctx, ui = req.property_context, req.user_inputs
    load_warnings: list[str] = []
    snap, _ = _safe(repo.rates, None, load_warnings, "rates_unavailable")
    out, raw, norm = calc.compute_with_raw(ctx, ui, snap)
    if "rates_unavailable" in load_warnings and "rates_unavailable" not in out.warnings:
        out.warnings.append("rates_unavailable")

    def pick(items, with_chat: bool) -> list[str]:
        sctx = SelectContext(
            title_type=norm.title_type,
            hazards=norm.hazards,
            fhl_eligible=raw.fhl_eligible,
            lvr_pct=raw.lvr,
            dti=raw.dti,
            year_range=norm.year_range if with_chat or not _chat_year_used(ctx, norm) else None,
            cladding=norm.cladding if with_chat or not _chat_cladding_used(ctx, norm) else None,
        )
        return [i.id for i in items if matches(i.applies_when, sctx)]

    all_items, kb_ok = _safe(repo.kb_items, [], out.warnings, "kb_unavailable")
    items = _visible(all_items, out.warnings)
    out.kb_items = pick(items, True)
    without_chat = set(pick(items, False))
    out.kb_items_from_user_input = [i for i in out.kb_items if i not in without_chat]
    out.kb_includes_draft = _include_draft()
    if not out.kb_items:
        out.kb_empty_reason = (
            "kb_unavailable" if not kb_ok
            else "no_verified_items" if not items
            else "no_matching_items"
        )
    return out


def _chat_year_used(ctx, norm) -> bool:
    """True when the year came from chat rather than a found property value."""
    st = ctx.property.year_built
    return not (st.status == "found" and calc.parse_year(st.value) is not None)


def _chat_cladding_used(ctx, norm) -> bool:
    c = ctx.property.cladding
    return not (c.status == "found" and c.value is not None)


@app.get("/v1/kb/items", response_model=list[KbItem], responses=ERRORS_422)
def kb_items(ids: str = Query("", description="comma-separated ids")) -> list[KbItem]:
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    items = _visible(get_repo().kb_items())
    if wanted:
        by_id = {i.id: i for i in items}
        return [by_id[i] for i in wanted if i in by_id]
    return items


@app.get(
    "/v1/rates/latest", response_model=RatesSnapshot,
    responses={404: {"model": ErrorResponse, "description": "No rates snapshot"}, **ERRORS},
)
def rates_latest() -> RatesSnapshot:
    snap = get_repo().rates()
    if snap is None:
        raise HTTPException(404, "no rates snapshot available")
    return snap


@app.get("/v1/sources", response_model=list[Source], responses=ERRORS)
def sources() -> list[Source]:
    return get_repo().sources()
