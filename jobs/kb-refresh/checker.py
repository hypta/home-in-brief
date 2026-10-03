"""Fetch a source page, check that the quote is still in it. Never rewrites item content."""
import hashlib
import io
import re
import urllib.robotparser
from urllib.parse import urlsplit
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

USER_AGENT = "home-buyer-copilot/0.1 (+https://github.com/hypta/home-buyer-copilot)"
ROBOTS_AGENT = "home-buyer-copilot"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-NZ,en;q=0.9",
}
TIMEOUT = 20.0
BLOCK_STATUSES = {401, 403, 406, 429, 503}
MIN_TEXT_LEN = 500  # shorter HTML text is a JS shell or challenge, not the real page
# Markers of CDN bot-challenge pages: they return 200 but contain none of the real page.
BLOCK_MARKERS = ("_incapsula_resource", "cf-browser-verification", "just a moment...", "attention required! | cloudflare")

_QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"'}
_DASHES = "‐‑‒–—―−"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for k, v in _QUOTES.items():
        text = text.replace(k, v)
    for d in _DASHES:
        text = text.replace(d, "-")
    return re.sub(r"\s+", " ", text.lower()).strip()


_INLINE = {"a", "span", "em", "strong", "b", "i", "u", "small", "sup", "sub", "abbr", "cite", "mark", "font"}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag not in _INLINE:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        if tag not in _INLINE:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextParser()
    p.feed(html)
    return "".join(p.parts)


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


@dataclass
class FetchResult:
    http_status: int | None
    text: str | None  # extracted page text; None when fetch failed or was blocked
    error: str | None
    network_error: bool = True  # True: failure is the network's/CDN's, not ours
    blocked: bool = False  # CDN block, challenge, empty JS shell or robots.txt: recorded, never worked around


def http_fetch(url: str, client: httpx.Client | None = None) -> FetchResult:
    own = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    try:
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            return FetchResult(None, None, f"{type(e).__name__}: {e}")
        if resp.status_code >= 400:
            return FetchResult(resp.status_code, None, f"HTTP {resp.status_code}",
                               blocked=resp.status_code in BLOCK_STATUSES)
        ctype = resp.headers.get("content-type", "").lower()
        try:
            is_pdf = resp.content[:5] == b"%PDF-"
            if not is_pdf and url.lower().split("?")[0].endswith(".pdf"):
                # a non-PDF body on a .pdf URL is a block page
                return FetchResult(resp.status_code, None, "expected PDF, got non-PDF body (likely CDN block)", blocked=True)
            if is_pdf:
                text = pdf_to_text(resp.content)
            else:
                body = resp.text
                if any(m in body.lower() for m in BLOCK_MARKERS) and len(body) < 5000:
                    return FetchResult(resp.status_code, None, "blocked by CDN bot challenge", blocked=True)
                text = html_to_text(body)
                if len(normalize(text)) < MIN_TEXT_LEN:
                    return FetchResult(resp.status_code, None, f"page text too short ({len(normalize(text))} chars)",
                                       blocked=True)
        except Exception as e:  # extraction failure is ours, not the network's
            return FetchResult(resp.status_code, None, f"extract failed: {type(e).__name__}: {e}", False)
        return FetchResult(resp.status_code, text, None)
    finally:
        if own:
            client.close()


class Fetcher:
    """httpx only. Honest User-Agent, robots.txt first, no retry or workaround when blocked."""

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | str] = {}

    def _robots_for(self, origin: str):
        if origin not in self._robots:
            try:
                r = self.client.get(origin + "/robots.txt")
            except httpx.HTTPError as e:
                self._robots[origin] = f"robots.txt unavailable: {type(e).__name__}"
                return self._robots[origin]
            rp = urllib.robotparser.RobotFileParser()
            if r.status_code in (401, 403) or r.status_code >= 500:
                self._robots[origin] = f"robots.txt unavailable: HTTP {r.status_code}"  # be conservative
            elif r.status_code >= 400:
                rp.parse([])  # no robots.txt: everything allowed
                self._robots[origin] = rp
            else:
                rp.parse(r.text.splitlines())
                self._robots[origin] = rp
        return self._robots[origin]

    def __call__(self, url: str) -> FetchResult:
        u = urlsplit(url)
        rp = self._robots_for(f"{u.scheme}://{u.netloc}")
        if isinstance(rp, str):
            return FetchResult(None, None, rp, blocked=True)
        if not rp.can_fetch(ROBOTS_AGENT, url):
            return FetchResult(None, None, "disallowed by robots.txt", blocked=True)
        return http_fetch(url, self.client)

    def close(self) -> None:
        self.client.close()


def manual_run(now: str, reason: str) -> dict:
    """Run record for an item whose source we must not fetch automatically (no request is made)."""
    return {"at": now, "http_status": None, "quote_found": None, "page_sha256": None,
            "error": None, "network_error": False, "blocked": False,
            "manual_check_required": True, "manual_reason": reason}


def check_item(item: dict, fetch=http_fetch, now: str = "") -> dict:
    """Run record: {at, http_status, quote_found, page_sha256, error, network_error}.

    quote_found is None when the page could not be read (not the same as False).
    """
    res = fetch(item["source_url"])
    run = {
        "at": now,
        "http_status": res.http_status,
        "quote_found": None,
        "page_sha256": None,
        "error": res.error,
        "network_error": bool(res.error) and res.network_error,
        "blocked": res.blocked,
        "manual_check_required": False,
    }
    if res.text is not None:
        norm = normalize(res.text)
        run["quote_found"] = normalize(item["quote"]) in norm
        run["page_sha256"] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return run


def next_status(item: dict, run: dict) -> str:
    """Only verified + quote definitely gone -> needs_review. Everything else keeps status."""
    if item.get("status") == "verified" and run["quote_found"] is False:
        return "needs_review"
    return item.get("status", "draft")
