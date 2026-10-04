"""web: the only public service. Serves the report page and relays API calls to the private services."""
from __future__ import annotations

import logging
import os
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
# The whole public API: browser path -> (private service, its path). Nothing else is relayed.
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
TIMEOUT = 30  # property-context makes several upstream calls of up to 8 s each
MAX_BODY = 256 * 1024  # a PropertyContext is about 13 kB
_tokens: dict[str, tuple[float, str]] = {}
log = logging.getLogger("web")

app = FastAPI(openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def edge(request: Request, call_next):
    if request.method == "POST" and int(request.headers.get("content-length") or MAX_BODY + 1) > MAX_BODY:
        return _error(413, "too_large", "Request body is missing its length or is too large.")
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"  # page files must not go stale across a deploy
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


def _relay(method: str, path: str, request: Request, payload: Optional[dict] = None) -> Response:
    target = ROUTES.get((method, path))
    if not target:
        return _error(404, "not_found", "Unknown API path.")
    base, upstream = target
    try:
        r = requests.request(method, base + upstream, params=request.query_params.multi_items(), json=payload,
                             headers=_auth(base), timeout=TIMEOUT)
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
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


@app.get("/config.json")
def config() -> dict:
    """The basemap key is sent to browsers on purpose: they fetch the map tiles themselves."""
    return {"linz_basemaps_key": os.environ.get("LINZ_BASEMAPS_KEY")}


@app.get("/api/{path:path}")
def relay_get(path: str, request: Request) -> Response:
    return _relay("GET", path, request)


@app.post("/api/{path:path}")
def relay_post(path: str, request: Request, payload: dict) -> Response:
    return _relay("POST", path, request, payload)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
