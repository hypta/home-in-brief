"""report-api: the Gemini layer. Writes the report summary and builds the rest of the report by code."""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from b_common.report_schemas import Report, ReportRequest
from b_common.schemas import ErrorResponse

from . import cache, gemini, obs, protection
from .followup import FollowupRequest, FollowupResponse, answer_followup
from .service import Debug, ReportError, build_report, load_kb

obs.setup_logging()

log = logging.getLogger("report-api")
app = FastAPI(title="report-api", version="0.1.0", on_startup=[protection.warm_up])
app.add_middleware(obs.CompletionLog)  # one JSON completion record per /v1 request
ERRORS = {c: {"model": ErrorResponse, "description": d} for c, d in {
    422: "Validation error", 500: "Internal server error", 502: "Model output invalid or no sentence passed",
    503: "Model or knowledge base unavailable, or busy (all generation slots taken; Retry-After)"}.items()}
BUSY_HEADERS = {"Retry-After": "5", "Cache-Control": "no-store"}


def _error(status: int, code: str, message: str, fields: Optional[list[dict]] = None,
           headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(status_code=status, headers=headers,
                        content={"error": {"code": code, "message": message, "fields": fields or []}})


@app.exception_handler(protection.Busy)
def _busy(_: Request, exc: protection.Busy) -> JSONResponse:
    return _error(503, "busy", "The service is busy; try again in a few seconds.", headers=BUSY_HEADERS)


@app.exception_handler(RequestValidationError)
def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [{"field": ".".join(str(x) for x in e["loc"] if x != "body"), "message": e["msg"]}
              for e in exc.errors()]
    return _error(422, "validation_error", "Request validation failed.", fields)


@app.exception_handler(ReportError)
def _report_error(_: Request, exc: ReportError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(StarletteHTTPException)
def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(Exception)
def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    # class and frames only, never the message (Codex R7; obs.safe_traceback)
    # no exc_info: any handler, not only obs.JsonFormatter, would print the message with the traceback
    log.error("unhandled error", extra={"json_fields": {"exception": obs.safe_traceback(exc)}})
    return _error(500, "internal_error", "Internal server error.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _degraded_header(response: Response) -> None:
    """X-Report-Degraded-Reason (Codex R3): only when the answer fell back to drafts after a refused or failed
    model call or for lack of time (obs.DEGRADED_REASONS)."""
    reason = obs.degraded_reason()
    if reason:
        response.headers["X-Report-Degraded-Reason"] = reason


@app.post("/v1/report", response_model=Report, responses=ERRORS)
def report(req: ReportRequest, request: Request, response: Response) -> Report:
    response.headers["Cache-Control"] = "no-store"
    # One deadline for the whole request (Codex R1): REQUEST_DEADLINE_S, or web's X-Request-Deadline-Ms if smaller.
    # The KB read, the wait for an identical request in flight, the reservation and the call all count against it.
    with gemini.request_budget(request.headers) as budget:
        kbd = load_kb(req, budget)
        if not cache.cacheable(req.property_context, req.user_inputs):
            return _generate_report(req, response, kbd)
        k = cache.key(req.property_context, req.user_inputs, req.rules_output, gemini.model_name(),
                      cache.kb_fingerprint(kbd.items, kbd.sources))
        return _cached_report(req, response, kbd, k, budget)


def _cached_report(req: ReportRequest, response: Response, kbd, k: str, budget: gemini.Budget) -> Report:
    hit = cache.REPORTS.get(k)
    leader, flight = (False, None) if hit is not None else cache.REPORTS.join(k)
    if hit is None and not leader:
        # an identical request is generating: its result, without a slot or a call; a follower that gives up
        # generates with what is left of this request's deadline, never a fresh budget
        hit = cache.wait(flight, min(cache.FOLLOWER_WAIT_S, max(0.0, budget.left())))
    if hit is not None:
        obs.note_cache("hit")
        response.headers["X-Report-Mode"] = "gemini"
        return Report.model_validate(hit)
    obs.note_cache("miss")
    value = None
    try:
        rep = _generate_report(req, response, kbd)
        if leader and response.headers["X-Report-Mode"] == "gemini" and \
                protection.GENERATION_DEGRADED not in rep.warnings:
            value = rep.model_dump(mode="json")
        return rep
    finally:
        if leader:
            cache.REPORTS.finish(k, value)  # also after busy or an error: the duplicates stop waiting


def _generate_report(req: ReportRequest, response: Response, kbd=None) -> Report:
    with protection.generation_slot():  # 503 busy at once when GENERATION_SLOTS requests are running (Codex F03)
        rep = build_report(req, debug=obs.attach(Debug()), kbd=kbd)
    response.headers["X-Report-Mode"] = obs.mode()  # gemini | drafts | mock (deploy smoke test, Codex F06)
    _degraded_header(response)
    return rep


FOLLOWUP_ERRORS = ERRORS | {504: {"model": ErrorResponse, "description": "Request deadline exceeded"}}


@app.post("/v1/followup", response_model=FollowupResponse, response_model_exclude_none=True, responses=FOLLOWUP_ERRORS)
def followup(req: FollowupRequest, request: Request, response: Response,
             refusal_mode: Literal["exempt", "strict"] = "exempt") -> FollowupResponse:
    with gemini.request_budget(request.headers), protection.generation_slot():
        resp = answer_followup(req, refusal_mode, debug=obs.attach({}), web_search=True)
    obs.note_web(resp.web is not None)
    response.headers["X-Report-Mode"] = obs.mode()
    _degraded_header(response)
    response.headers["Cache-Control"] = "no-store"
    return resp
