"""web: the only public service. Serves the report page, relays API calls to the private services and asks
report-api for the summary and the answers to follow-up questions."""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

PROPERTY = os.environ.get("PROPERTY_API_URL", "http://localhost:8081").rstrip("/")
RULES = os.environ.get("RULES_API_URL", "http://localhost:8080").rstrip("/")
REPORT = os.environ.get("REPORT_API_URL", "http://localhost:8090").rstrip("/")
# The relayed part of the public API: browser path -> (private service, its path). POST /api/report and /api/followup are the only other paths.
ROUTES = {
    ("GET", "addresses"): (PROPERTY, "/v1/addresses"),
    ("GET", "property-context"): (PROPERTY, "/v1/property-context"),
    ("GET", "map"): (PROPERTY, "/v1/map"),
    ("POST", "rules"): (RULES, "/v1/rules"),
    ("GET", "rates/latest"): (RULES, "/v1/rates/latest"),
    ("GET", "kb/items"): (RULES, "/v1/kb/items"),
    ("GET", "sources"): (RULES, "/v1/sources"),
}
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
# property-context makes several upstream calls of up to 8 s each. /api/report and /api/followup each share one such
# limit between their calls.
TIMEOUT = 30
MAX_BODY = 256 * 1024  # a PropertyContext is about 13 kB
# What report-api says about its answer in headers, passed on to the browser: how the answer was made (the deploy check
# reads the first two to tell whether Gemini wrote it), when to ask again, and that it must not be stored.
PASSED_ON = ("X-Report-Mode", "X-Report-Degraded-Reason", "Retry-After", "Cache-Control")
_tokens: dict[str, tuple[float, str]] = {}
# What one visitor may ask of /api/: (requests at once, seconds until one more), for a report or an answer, which cost
# model calls, and for everything else. Counted in this instance's memory: with several instances a visitor gets this
# much from each, so it slows a flood down and report-api's daily allowance stays the hard limit.
LIMITS = {True: (6, 10.0), False: (20, 1.0)}
COSTLY = ("/api/report", "/api/followup")
_buckets: dict[tuple[str, bool], tuple[float, float]] = {}  # (visitor, costly) -> (requests left, when that was counted)
# address id -> (expiry, PropertyContext as the page was given it), so the summary is written from what the page shows
CONTEXT_TTL, CONTEXT_MAX = 600, 100
_contexts: dict[str, tuple[float, bytes]] = {}
# A council property category such as RD199B: characters 3-5 are the build decade (LINZ Rating Valuations Rules
# 2008, Table 21). 191 (before 1914), 192 (1914-1929) and XXX (mixed ages) are valid but are not a decade.
_CATEGORY = re.compile(r"\s*(?:R[ACDFHNR]|LI)([0-9]{3}|XXX)[ABC]?\s*", re.I)
log = logging.getLogger("web")
mimetypes.add_type("font/woff2", ".woff2")  # the image's Python (3.12) does not know it and serves application/octet-stream

app = FastAPI(openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)


def _visitor(request: Request) -> str:
    """The address a request came from. Google's front end adds it to X-Forwarded-For after anything the sender wrote
    there, so the list is read from its end; an address that is not public (a hop inside Google's network) is passed
    over. Not confirmed for Cloud Run in Google's documentation: check the header of a deployed request."""
    for hop in reversed(request.headers.get("x-forwarded-for", "").split(",")):
        try:
            if ipaddress.ip_address(hop.strip()).is_global:
                return hop.strip()
        except ValueError:
            pass
    return request.client.host if request.client else ""


def _wait(visitor: str, costly: bool) -> float:
    """0 when this visitor's request may go ahead (it is counted), else the seconds until one may."""
    burst, every = LIMITS[costly]
    now = time.monotonic()
    left, at = _buckets.get((visitor, costly), (burst, now))
    left = min(burst, left + (now - at) / every)
    if len(_buckets) > 10_000:
        _buckets.clear()  # ponytail: everyone starts afresh when the table is full; evict the oldest if that is ever abused
    _buckets[(visitor, costly)] = (left - 1 if left >= 1 else left, now)
    return 0.0 if left >= 1 else (1 - left) * every


@app.middleware("http")
async def edge(request: Request, call_next):
    if request.method == "POST" and int(request.headers.get("content-length") or MAX_BODY + 1) > MAX_BODY:
        return _error(413, "too_large", "Request body is missing its length or is too large.")
    if request.url.path.startswith("/api/"):
        wait = _wait(_visitor(request), request.url.path in COSTLY)
        if wait:
            refused = _error(429, "rate_limited", "Too many requests. Wait a few seconds, then try again.")
            refused.headers["Retry-After"] = str(math.ceil(wait))
            return refused
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        # page files must not go stale across a deploy; a font file keeps its content for as long as it keeps its name
        response.headers["Cache-Control"] = "public, max-age=2592000" if request.url.path.startswith("/fonts/") and response.status_code == 200 else "no-cache"
    return response


def _error(status: int, code: str, message: str) -> JSONResponse:
    """The rules-api error shape, so the page reads one format whichever service failed."""
    return JSONResponse({"error": {"code": code, "message": message, "fields": []}}, status)


def _auth(base: str) -> dict:
    """ID token for a private Cloud Run service (DEPLOY.md section 11). Plain-http local services need none."""
    if not base.startswith("https://"):
        return {}
    expires, token = _tokens.get(base, (0.0, ""))
    if time.time() >= expires:
        r = requests.get(METADATA, params={"audience": base}, headers={"Metadata-Flavor": "Google"}, timeout=5)
        r.raise_for_status()
        token = r.text
        _tokens[base] = (time.time() + 50 * 60, token)  # tokens live for about an hour
    return {"Authorization": f"Bearer {token}"}


def _has_error_body(r: requests.Response) -> bool:
    try:
        body = r.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "error" in body


def _call(method: str, base: str, upstream: str, params=None, payload: Optional[dict] = None,
          timeout: float = TIMEOUT, headers: Optional[dict] = None) -> Response:
    try:
        r = requests.request(method, base + upstream, params=params, json=payload,
                             headers={**_auth(base), **(headers or {})}, timeout=timeout)
    except requests.RequestException as e:
        log.warning("%s %s failed: %s", method, upstream, type(e).__name__)
        return _error(502, "upstream_unavailable", "The service behind this request did not answer.")
    if r.status_code in (401, 403):  # our own service-to-service setup is wrong; not the visitor's problem
        _tokens.pop(base, None)
        log.warning("%s %s refused the call: %s", method, upstream, r.status_code)
        return _error(502, "upstream_auth", "The service behind this request refused the call.")
    if r.status_code >= 400 and not _has_error_body(r):
        log.warning("%s %s answered %s", method, upstream, r.status_code)
        return _error(r.status_code, "upstream_error", "The request could not be completed.")
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"),
                    headers={k: r.headers[k] for k in PASSED_ON if k in r.headers})


def _relay(method: str, path: str, request: Request, payload: Optional[dict] = None) -> Response:
    target = ROUTES.get((method, path))
    if not target:
        return _error(404, "not_found", "Unknown API path.")
    return _call(method, *target, params=request.query_params.multi_items(), payload=payload)


def _remember(context: bytes) -> None:
    """Kept under the address id the document itself carries: a request can name one address and be answered for another."""
    try:
        address_id = str(json.loads(context)["address"]["linz_address_id"])
    except (ValueError, KeyError, TypeError):
        return
    _contexts.pop(address_id, None)
    _contexts[address_id] = (time.monotonic() + CONTEXT_TTL, context)
    for oldest in list(_contexts)[:-CONTEXT_MAX]:
        _contexts.pop(oldest, None)


def _year_built(typed):
    """What the visitor typed, or for a council property category its decade (None when it has none); rules-api
    reads years and decades only."""
    m = _CATEGORY.fullmatch(typed) if isinstance(typed, str) else None
    if not m:
        return typed
    return f"{m[1]}0s" if "193" <= m[1] <= "209" else None


def _inputs(payload: dict) -> dict:
    """The visitor's user_inputs, with a council property category read as its decade."""
    inputs = payload.get("user_inputs")
    if not isinstance(inputs, dict):
        return {}
    chat = inputs.get("chat_supplied")
    if isinstance(chat, dict) and "year_built" in chat:
        chat["year_built"] = _year_built(chat["year_built"])
    return inputs


@app.get("/config.json")
def config() -> dict:
    """The basemap key is sent to browsers on purpose: they fetch the map tiles themselves."""
    return {"linz_basemaps_key": os.environ.get("LINZ_BASEMAPS_KEY")}


@app.get("/api/{path:path}")
def relay_get(path: str, request: Request) -> Response:
    r = _relay("GET", path, request)
    if path == "property-context" and r.status_code == 200:
        _remember(r.body)
    return r


def _ask(upstream: str, payload: dict, **asked) -> Response:
    """report-api's answer for one address. The browser names the address and sends the visitor's inputs: the
    documents report-api reads are fetched here, because a browser could send altered ones."""
    deadline = time.monotonic() + TIMEOUT

    def left() -> float:
        return max(1.0, deadline - time.monotonic())

    address_id = str(payload.get("address_id", ""))
    expires, context = _contexts.get(address_id, (0.0, b""))
    if time.monotonic() >= expires:
        r = _call("GET", PROPERTY, "/v1/property-context", params={"address_id": address_id}, timeout=left())
        if r.status_code != 200:
            return r
        context = r.body
        _remember(context)
    documents = {"property_context": json.loads(context), "user_inputs": _inputs(payload)}
    r = _call("POST", RULES, "/v1/rules", payload=documents, timeout=left())
    if r.status_code != 200:
        return r
    # report-api is told how long this request has left, counted here, so it stops spending on an answer nobody waits for
    remaining = left()
    return _call("POST", REPORT, upstream, payload={**documents, "rules_output": json.loads(r.body), **asked},
                 timeout=remaining, headers={"X-Request-Deadline-Ms": str(int(remaining * 1000))})


@app.post("/api/report")
def report(payload: dict) -> Response:
    """The summary and next steps for one address."""
    return _ask("/v1/report", payload)


@app.post("/api/followup")
def followup(payload: dict) -> Response:
    """The answer to one question about that report, or to the page's button that searches the web for one field the
    records lack (search_field, with the language to answer in). The question and the earlier turns are the visitor's
    own words; report-api checks their size and never reads them as facts."""
    return _ask("/v1/followup", payload, **{k: payload[k] for k in ("question", "history", "search_field", "language")
                                            if payload.get(k) is not None})


@app.post("/api/{path:path}")
def relay_post(path: str, request: Request, payload: dict) -> Response:
    _inputs(payload)
    return _relay("POST", path, request, payload)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
