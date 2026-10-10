"""report-api: the Gemini layer. Writes the report summary and builds the rest of the report by code."""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from b_common.report_schemas import Report, ReportRequest
from b_common.schemas import ErrorResponse

from .followup import FollowupRequest, FollowupResponse, answer_followup
from .service import ReportError, build_report

log = logging.getLogger("report-api")
app = FastAPI(title="report-api", version="0.1.0")
ERRORS = {c: {"model": ErrorResponse, "description": d} for c, d in {
    422: "Validation error", 500: "Internal server error", 502: "Model output invalid or no sentence passed",
    503: "Model or knowledge base unavailable"}.items()}


def _error(status: int, code: str, message: str, fields: Optional[list[dict]] = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message, "fields": fields or []}})


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
    log.exception("unhandled error")
    return _error(500, "internal_error", "Internal server error.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/report", response_model=Report, responses=ERRORS)
def report(req: ReportRequest) -> Report:
    return build_report(req)


FOLLOWUP_ERRORS = ERRORS | {504: {"model": ErrorResponse, "description": "Request deadline exceeded"}}


@app.post("/v1/followup", response_model=FollowupResponse, responses=FOLLOWUP_ERRORS)
def followup(req: FollowupRequest, refusal_mode: Literal["exempt", "strict"] = "exempt") -> FollowupResponse:
    return answer_followup(req, refusal_mode)
