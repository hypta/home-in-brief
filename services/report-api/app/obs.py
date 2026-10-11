"""Structured logs for Cloud Logging (Codex F07): single-line JSON on stdout, one completion record per
/v1/report and /v1/followup request, and one WARNING per failed Gemini call with its error category.

Never logged: prompts, the question, history, Search answer text, the user's amounts or income, API keys. The
completion record has counts, timings, the model name and categories only.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("report-api")

ENDPOINTS = {"/v1/report": "report", "/v1/followup": "followup"}
# error categories (worst last wins in the record only by order of occurrence: the last failure is kept)
TIMEOUT, QUOTA, HTTP_4XX, HTTP_5XX, SCHEMA, OTHER, NONE = "timeout", "quota", "http_4xx", "http_5xx", "schema", "other", "none"
CONFIG = "config"  # GEMINI_API_KEY is not set


class JsonFormatter(logging.Formatter):
    """{"severity", "message", "logger", "request_id"?, ...record.json_fields}: Cloud Logging reads severity and
    message from a JSON line on stdout."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        st = _stats.get()
        if st is not None:
            out["request_id"] = st.request_id
        out.update(getattr(record, "json_fields", None) or {})
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Application loggers at INFO, as JSON on stdout. uvicorn keeps its own handlers (access log: propagate off)."""
    root = logging.getLogger()
    if not any(getattr(h, "_obs_json", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        h._obs_json = True  # type: ignore[attr-defined]
        root.addHandler(h)
    root.setLevel(level)


@dataclass
class Stats:
    request_id: str
    endpoint: str
    t0: float = field(default_factory=time.monotonic)
    gemini_calls: int = 0
    searches: int = 0
    error: str = NONE
    debug: Any = None          # service.Debug (report) or the follow-up debug dict
    budget: Any = None         # the request's gemini.Budget (remaining time)
    web_answer: bool = False   # the follow-up returned a Google Search answer


_stats: contextvars.ContextVar[Optional[Stats]] = contextvars.ContextVar("obs_stats", default=None)


def current() -> Optional[Stats]:
    return _stats.get()


def request_id(headers: dict[str, str]) -> str:
    """X-Cloud-Trace-Context ("TRACE/SPAN;o=1": the trace id), else X-Request-Id, else a new id."""
    trace = headers.get("x-cloud-trace-context", "").split("/", 1)[0].strip()
    rid = trace or headers.get("x-request-id", "").strip()
    return re.sub(r"[^A-Za-z0-9._-]", "", rid)[:64] or uuid.uuid4().hex


# ---- hooks called by the service code (no-ops outside a request, e.g. in eval runs and unit tests) -----------
def attach(debug: Any) -> Any:
    st = _stats.get()
    if st is not None:
        st.debug = debug
    return debug


def track_budget(budget: Any) -> None:
    st = _stats.get()
    if st is not None and st.budget is None:
        st.budget = budget


def note_call(search: bool = False) -> None:
    st = _stats.get()
    if st is not None:
        st.gemini_calls += 1
        st.searches += search


def note_error(category: str) -> None:
    st = _stats.get()
    if st is not None:
        st.error = category


def note_web(answered: bool) -> None:
    st = _stats.get()
    if st is not None:
        st.web_answer = answered


def classify(exc: BaseException) -> str:
    """Error category of a failed Gemini call: timeout, quota (429 / RESOURCE_EXHAUSTED), http_4xx, http_5xx, other."""
    text = f"{type(exc).__name__} {exc}".lower()
    code = getattr(exc, "code", None)
    code = code if isinstance(code, int) else getattr(exc, "status_code", None)
    if "timeout" in text or "timed out" in text or "deadline" in text:
        return TIMEOUT
    if code == 429 or re.search(r"\b429\b|resource_exhausted|quota", text):
        return QUOTA
    if (isinstance(code, int) and 400 <= code < 500) or re.search(r"\b4\d\d\b", text):
        return HTTP_4XX
    if (isinstance(code, int) and 500 <= code < 600) or re.search(r"\b5\d\d\b", text):
        return HTTP_5XX
    return OTHER


def gemini_failed(logger: logging.Logger, what: str, exc: BaseException, key: str) -> tuple[str, str]:
    """Records and logs (WARNING, with the category) a failed Gemini call. Returns the error text with the key
    masked, at most 300 characters, and the category, for the exception the caller raises."""
    category = classify(exc)
    note_error(category)
    msg = (str(exc).replace(key, "***") if key else str(exc))[:300]
    logger.warning("%s failed: %s: %s", what, type(exc).__name__, msg,
                   extra={"json_fields": {"error": category, "exception": type(exc).__name__}})
    return msg, category


# ---- the completion record ---------------------------------------------------------------------------------
def _get(d: Any, name: str) -> Any:
    return d.get(name) if isinstance(d, dict) else getattr(d, name, None)


def mock_mode() -> bool:
    return os.environ.get("REPORT_MOCK", "").lower() in ("1", "true", "yes")


def sentence_counts(st: Stats) -> dict[str, int]:
    rw = _get(st.debug, "rewrite") or {}
    program = _get(st.debug, "program") or []
    return {"passed": int(rw.get("rewritten", 0)), "blocked": int(rw.get("blocked", 0)),
            "drafts": int(rw.get("fallback_drafts", 0)) + int(rw.get("must_added", 0)) + len(program)}


def mode(st: Optional[Stats] = None) -> str:
    """gemini: at least one sentence is a Gemini rewrite that passed (or a Search answer); drafts: only program
    drafts and fixed sentences; mock: REPORT_MOCK."""
    st = st or _stats.get()
    if mock_mode() or (st is not None and _get(st.debug, "mode") == "mock"):
        return "mock"
    if st is not None and (sentence_counts(st)["passed"] or st.web_answer):
        return "gemini"
    return "drafts"


def completion_fields(st: Stats, status: int) -> dict[str, Any]:
    from . import gemini  # imported here: gemini imports this module

    out: dict[str, Any] = {
        "event": "request_complete", "request_id": st.request_id, "endpoint": st.endpoint, "status": status,
        "elapsed_ms": round((time.monotonic() - st.t0) * 1000),
        "model": "mock" if mode(st) == "mock" else gemini.model_name(),
        "gemini_calls": st.gemini_calls, "searches": st.searches, "error": st.error,
        "sentences": sentence_counts(st), "mode": mode(st),
        "remaining_s": round(st.budget.left(), 1) if st.budget is not None else None,
    }
    lang = _get(st.debug, "language")
    if st.endpoint == "followup":
        out["language"] = lang.get("used") if isinstance(lang, dict) else None
    return out


class CompletionLog:
    """ASGI middleware: one INFO record per /v1/report and /v1/followup request, after the response (or the
    unhandled error, logged with status 500)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        endpoint = ENDPOINTS.get(scope.get("path", "")) if scope.get("type") == "http" else None
        if endpoint is None:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        st = Stats(request_id=request_id(headers), endpoint=endpoint)
        token = _stats.set(st)
        status = 500

        async def send_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            log.info("request complete", extra={"json_fields": completion_fields(st, status)})
            _stats.reset(token)
