"""Structured logs for Cloud Logging (same format as report-api's app/obs.py): single-line JSON on
stdout and one completion record per /v1 request (endpoint, status, elapsed_ms, warnings count). The request body
(prices, deposits, incomes, debts) is never logged."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from typing import Any, Optional

log = logging.getLogger("rules-api")
_request: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("obs_request", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        st = _request.get()
        if st is not None:
            out["request_id"] = st["request_id"]
        out.update(getattr(record, "json_fields", None) or {})
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if not any(getattr(h, "_obs_json", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        h._obs_json = True  # type: ignore[attr-defined]
        root.addHandler(h)
    root.setLevel(level)


def request_id(headers: dict[str, str]) -> str:
    trace = headers.get("x-cloud-trace-context", "").split("/", 1)[0].strip()
    rid = trace or headers.get("x-request-id", "").strip()
    return re.sub(r"[^A-Za-z0-9._-]", "", rid)[:64] or uuid.uuid4().hex


def note_warnings(n: int) -> None:
    st = _request.get()
    if st is not None:
        st["warnings"] = n


class CompletionLog:
    """ASGI middleware: one INFO record per /v1/* request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not path.startswith("/v1/"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        st = {"request_id": request_id(headers), "warnings": None}
        token, t0, status = _request.set(st), time.monotonic(), 500

        async def send_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            log.info("request complete", extra={"json_fields": {
                "event": "request_complete", "request_id": st["request_id"], "endpoint": path, "status": status,
                "elapsed_ms": round((time.monotonic() - t0) * 1000), "warnings": st["warnings"]}})
            _request.reset(token)
