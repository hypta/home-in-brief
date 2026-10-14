"""Council page relay: a tiny, single-purpose HTTP service that runs on a Raspberry Pi in New Zealand.

Auckland Council answers 406 to cloud hosts outside New Zealand. This service fetches one council page per
request (the property page for a rates key), from a New Zealand address, with the project's honest User-Agent.
It is NOT a general proxy: the only upstream URL is built here from a validated rates key. Standard library only.
"""
from __future__ import annotations

import gzip
import io
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

# Same page and same User-Agent as services/property-api/app/council.py and app/http.py (kept in step by hand).
RATES_PAGE = "https://www.aucklandcouncil.govt.nz/en/property-rates-valuations/find-property-rates-valuation/{key}.html"
USER_AGENT = "home-in-brief/0.1 (+https://github.com/hypta/home-in-brief; non-commercial)"
KEY_RE = re.compile(r"[0-9]{5,15}")  # a rates assessment number: digits only (observed: 11 digits)
UPSTREAM_TIMEOUT = 8  # seconds
MAX_BODY = 10_000_000  # bytes, after decompression; the raw page was about 5.4 MB on 2026-10-14, almost all inline scripts
_DROP = re.compile(r"<(script|style|svg|noscript)\b[^>]*>.*?</\1\s*>", re.S | re.I)  # comments stay: they split numbers from units
MIN_TOKEN_LEN = 16
log = logging.getLogger("council-relay")


@dataclass(frozen=True)
class Config:
    token: str
    rate_per_minute: int = 20
    cache_ttl_s: int = 24 * 3600
    cache_max: int = 300

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        token = env.get("RELAY_TOKEN", "")
        if len(token) < MIN_TOKEN_LEN:
            raise SystemExit(f"RELAY_TOKEN must be set (at least {MIN_TOKEN_LEN} characters); refusing to start")
        return cls(token=token, rate_per_minute=int(env.get("RATE_LIMIT_PER_MINUTE", 20)),
                   cache_ttl_s=int(env.get("CACHE_TTL_SECONDS", 24 * 3600)), cache_max=int(env.get("CACHE_MAX_ENTRIES", 300)))


class UpstreamError(Exception):
    def __init__(self, status: Optional[int], reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # any redirect becomes an HTTPError (3xx): never leave the page
        return None


def fetch_council(url: str) -> str:
    """GET one council URL and return the body. Raises UpstreamError for anything but a plain 200."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "text/html",
                                                             "Accept-Encoding": "gzip"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=UPSTREAM_TIMEOUT) as r:
            if r.status != 200:
                raise UpstreamError(r.status, "upstream_status")
            raw = r.read(MAX_BODY + 1)  # gzip on the wire was about 275 kB on 2026-10-14
            if len(raw) > MAX_BODY:
                raise UpstreamError(r.status, "upstream_too_large")
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_BODY + 1)
                except (OSError, EOFError):
                    raise UpstreamError(r.status, "upstream_bad_encoding") from None
            if len(raw) > MAX_BODY:
                raise UpstreamError(r.status, "upstream_too_large")
            return slim(raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise UpstreamError(e.code, "upstream_status") from None
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        raise UpstreamError(None, "upstream_timeout" if "timed out" in str(e) else "upstream_unreachable") from None


def slim(page: str) -> str:
    """Drop scripts, styles and inline SVG (HTML comments stay, because the page uses them between a number and its unit): the page keeps its text (what property-api parses) at about
    30 kB instead of about 5 MB, so the relay sends little over a home connection and caches little."""
    return _DROP.sub("", page)


class Relay:
    """The whole behaviour, without sockets, so tests can call handle() directly."""

    def __init__(self, cfg: Config, fetch: Callable[[str], str] = fetch_council, clock: Callable[[], float] = time.monotonic):
        self.cfg, self._fetch, self._clock = cfg, fetch, clock
        self._cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._calls: deque = deque()  # times of recent upstream fetches
        self._lock = threading.Lock()  # one fetch at a time: no duplicate fetch of the same key, gentle on the council

    def _json(self, code: int, /, **body) -> tuple[int, str, str]:
        return code, "application/json", json.dumps(body)

    def handle(self, target: str, headers) -> tuple[int, str, str]:
        """(status, content type, body) for a GET request target such as '/v1/property-page?key=123'."""
        parts = urlsplit(target)
        if parts.path == "/healthz":
            return self._json(200, status="ok")
        if parts.path != "/v1/property-page":
            return self._json(404, error="not_found")
        if not hmac.compare_digest((headers.get("X-Relay-Token") or "").encode(), self.cfg.token.encode()):
            return self._json(401, error="unauthorized")
        values = parse_qs(parts.query).get("key", [])
        if len(values) != 1 or not KEY_RE.fullmatch(values[0]):
            return self._json(400, error="invalid_key")
        key = values[0]
        with self._lock:
            now = self._clock()
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cfg.cache_ttl_s:
                self._cache.move_to_end(key)
                log.info("property-page key=%s cache=hit status=200", key)
                return 200, "text/html; charset=utf-8", hit[1]
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) >= self.cfg.rate_per_minute:
                log.warning("property-page key=%s status=429", key)
                return self._json(429, error="rate_limited")
            self._calls.append(now)
            try:
                page = self._fetch(RATES_PAGE.format(key=key))
            except UpstreamError as e:
                log.warning("property-page key=%s cache=miss upstream=%s reason=%s", key, e.status, e.reason)
                return self._json(504 if e.reason == "upstream_timeout" else 502, error=e.reason, upstream_status=e.status)
            self._cache[key] = (now, page)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cfg.cache_max:
                self._cache.popitem(last=False)
            log.info("property-page key=%s cache=miss status=200 bytes=%d", key, len(page))
            return 200, "text/html; charset=utf-8", page


def make_server(relay: Relay, host: str = "0.0.0.0", port: int = 8787) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "council-relay"
        sys_version = ""

        def do_GET(self):
            status, ctype, body = relay.handle(self.path, self.headers)
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # the default log would print the request line; relay.handle logs what matters
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
    cfg = Config.from_env()
    port = int(os.environ.get("PORT", 8787))
    log.info("starting on port %d, %d upstream fetches/min, cache %ds", port, cfg.rate_per_minute, cfg.cache_ttl_s)
    make_server(Relay(cfg), port=port).serve_forever()


if __name__ == "__main__":
    main()
