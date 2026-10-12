"""Structured logs for Cloud Logging: single-line JSON on stdout, one completion record per
/v1/report and /v1/followup request, and one WARNING per failed Gemini call with its error category.

Never logged: prompts, the question, history, Search answer text, the user's amounts or income, API keys, and
exception messages: a failed Gemini call logs its exception class, category, HTTP status and the
provider's request id; any logged traceback (ours, or uvicorn's "Exception in ASGI application") keeps only the
exception classes and the file:line:function of each frame. The completion record has counts, timings, the model
name and categories only.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("report-api")

ENDPOINTS = {"/v1/report": "report", "/v1/followup": "followup"}
# error categories (worst last wins in the record only by order of occurrence: the last failure is kept)
TIMEOUT, QUOTA, HTTP_4XX, HTTP_5XX, SCHEMA, OTHER, NONE = "timeout", "quota", "http_4xx", "http_5xx", "schema", "other", "none"
CONFIG = "config"  # GEMINI_API_KEY is not set
ALLOWANCE = "allowance"  # protection.admit refused the call (allowance, REPORT_DEGRADED, prompt size): drafts
SEARCH_ALLOWANCE = "search_allowance"  # the daily Search allowance is used up: only that search is skipped
DEADLINE = "deadline"  # no time left for a model call (before it, or after its reservation)
MODEL_ERROR = "model_error"
# X-Report-Degraded-Reason values: only on a response that fell back to drafts
DEGRADED_REASONS = ("allowance_daily", "allowance_event", "disabled", "backend_error", "prompt_too_large",
                    MODEL_ERROR, DEADLINE)
_MODEL_ERRORS = {TIMEOUT, QUOTA, HTTP_4XX, HTTP_5XX, SCHEMA, OTHER, CONFIG}


def safe_traceback(exc_info) -> str:
    """A traceback without any exception message: the exception classes (chained ones too) and file:line in
    function for each frame. Messages can echo request content."""
    etype, value, tb = exc_info if isinstance(exc_info, tuple) else (type(exc_info), exc_info, exc_info.__traceback__)
    out: list[str] = []
    seen: set[int] = set()
    while value is not None and id(value) not in seen:
        seen.add(id(value))
        frames = traceback.extract_tb(value.__traceback__ if value is not None else tb)
        out.append(type(value).__name__ + " at " + " < ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(frames)))
        value = value.__cause__ or value.__context__
    return " | caused by ".join(out) or (etype.__name__ if etype else "")


class SanitizeExceptions(logging.Filter):
    """For loggers we do not format (uvicorn.error logs "Exception in ASGI application" with the traceback): the
    traceback is replaced by safe_traceback before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            safe = safe_traceback(record.exc_info)
            record.exc_info, record.exc_text = None, None
            record.msg = f"{record.getMessage()} [{safe}]"
            record.args = ()
        return True


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
            out["exception"] = safe_traceback(record.exc_info)  # never the message
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
    for name in ("uvicorn.error", "uvicorn"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, SanitizeExceptions) for f in lg.filters):
            lg.addFilter(SanitizeExceptions())


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
    allowance: str = "ok"      # protection: ok, or the last reason a Gemini call was refused
    busy: bool = False         # refused with 503 busy (no free generation slot)
    cache: str = "none"        # /v1/report cache: none (not cacheable), miss, hit (stored or from a duplicate in flight)
    degraded_reason: Optional[str] = None  # last reason a model call was refused or failed (DEGRADED_REASONS)


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
    if category in _MODEL_ERRORS:
        note_degraded(MODEL_ERROR)


def note_degraded(reason: str) -> None:
    """Why the response may be drafts (X-Report-Degraded-Reason); the last reason recorded wins."""
    st = _stats.get()
    if st is not None and reason in DEGRADED_REASONS:
        st.degraded_reason = reason


def degraded_reason(st: Optional[Stats] = None) -> Optional[str]:
    """The X-Report-Degraded-Reason value: only when the response is drafts and a reason was recorded."""
    st = st or _stats.get()
    if st is None or mode(st) != "drafts":
        return None
    return st.degraded_reason


def note_allowance(reason: str) -> None:
    st = _stats.get()
    if st is not None and reason != "ok":
        st.allowance = reason


def note_busy() -> None:
    st = _stats.get()
    if st is not None:
        st.busy = True
        st.error = "busy"


def note_cache(state: str) -> None:
    st = _stats.get()
    if st is not None:
        st.cache = state


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


def http_status(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "code", None)
    code = code if isinstance(code, int) else getattr(exc, "status_code", None)
    return code if isinstance(code, int) and 100 <= code <= 599 else None


_REQUEST_ID_HEADERS = ("x-goog-request-id", "x-request-id", "x-cloud-trace-context")


def provider_request_id(exc: BaseException) -> Optional[str]:
    """The provider's request id from the error's HTTP response headers, if any (safe characters only)."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    for name in _REQUEST_ID_HEADERS:
        try:
            v = headers.get(name)
        except Exception:
            return None
        if v:
            return re.sub(r"[^A-Za-z0-9._/-]", "", str(v))[:64] or None
    return None


def gemini_failed(logger: logging.Logger, what: str, exc: BaseException) -> tuple[str, str]:
    """Records and logs (WARNING) a failed Gemini call: exception class, category, HTTP status and the provider's
    request id only, never the exception's message (it can echo the prompt, a URL or model text).
    Returns that safe description and the category, for the exception the caller raises."""
    category = classify(exc)
    note_error(category)
    status, rid = http_status(exc), provider_request_id(exc)
    fields: dict[str, Any] = {"error": category, "exception": type(exc).__name__}
    if status is not None:
        fields["http_status"] = status
    if rid:
        fields["provider_request_id"] = rid
    msg = f"{type(exc).__name__} ({category}" + (f", HTTP {status}" if status is not None else "") + ")"
    logger.warning("%s failed: %s", what, msg, extra={"json_fields": fields})
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
    if st is not None and (sentence_counts(st)["passed"] or st.web_answer or st.cache == "hit"):
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
        "allowance": st.allowance, "busy": st.busy, "cache": st.cache,
        "degraded_reason": degraded_reason(st),
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
