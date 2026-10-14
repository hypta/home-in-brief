"""One HTTP entry point for every upstream call, so tests can replay recorded responses."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

import requests

TIMEOUT = 8  # seconds; normal answers take under 3
_KEY = re.compile(r";key=[^/]+")
_SCRIPT = re.compile(r"<script.*?</script>", re.S)
_SESSION = requests.Session()
# Auckland Council answers 406 to default client user agents.
_SESSION.headers.update({
    "User-Agent": "home-in-brief/0.1 (+https://github.com/hypta/home-in-brief; non-commercial)",
    "Accept": "application/json, text/html"})


def redact(url: str) -> str:
    return _KEY.sub(";key=KEY", url)


def signature(method: str, url: str, params: Optional[dict]) -> str:
    """Stable id of a request, with the LINZ key removed."""
    raw = json.dumps([method, redact(url), params or {}], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def request(method: str, url: str, params: Optional[dict] = None, *, as_json: bool = True) -> Any:
    """GET sends params in the query string, POST in the form body. Raises on any failure."""
    kw = {"params": params} if method == "GET" else {"data": params}
    r = _SESSION.request(method, url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    body = r.json() if as_json else _SCRIPT.sub("", r.text)
    if as_json and isinstance(body, dict) and "error" in body:  # ArcGIS reports errors with HTTP 200
        raise RuntimeError(f"upstream error: {body['error']}")
    return body


RELAY_TIMEOUT = 6  # seconds; web gives the whole request 30


def relay_page(base_url: str, key: str, token: str) -> str:
    """The council property page for a rates key, fetched through the New Zealand relay (relay/council-relay).

    Raises on a timeout, on any status but 200 (the relay answers 429 when it is busy, 502 when the council fails)
    and on a redirect. The token only travels in the header, so it is never in an exception message."""
    r = requests.get(base_url.rstrip("/") + "/v1/property-page", params={"key": key},
                     headers={"X-Relay-Token": token, "Accept": "text/html"}, timeout=RELAY_TIMEOUT,
                     allow_redirects=False)
    r.raise_for_status()
    if r.status_code != 200:
        raise RuntimeError(f"relay answered {r.status_code}")
    return _SCRIPT.sub("", r.text)
