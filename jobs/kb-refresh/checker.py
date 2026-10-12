"""Fetch a source page, check that the quote is still in it. Never rewrites item content."""
import hashlib
import io
import re
import sys
import unicodedata
import urllib.robotparser
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "kb"))
from content_hash import content_hash, effective_status  # noqa: E402

USER_AGENT = "home-in-brief/0.1 (+https://github.com/hypta/home-in-brief)"
ROBOTS_AGENT = "home-in-brief"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-NZ,en;q=0.9",
}
TIMEOUT = 20.0
BLOCK_STATUSES = {401, 403, 406, 429, 503}
MAX_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
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


def _get_limited(client: httpx.Client, url: str) -> tuple[httpx.Response, bytes]:
    """GET without following redirects; abort bodies larger than MAX_BYTES."""
    with client.stream("GET", url) as resp:
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError(f"response larger than {MAX_BYTES} bytes")
            chunks.append(chunk)
        return resp, b"".join(chunks)


def http_fetch(url: str, client: httpx.Client | None = None, guard=None) -> FetchResult:
    """Fetch one page. Redirects are followed by hand (max MAX_REDIRECTS): only https and the same
    host as the approved URL, and guard(url) (robots.txt) must still pass for every hop."""
    own = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=False)
    try:
        origin_host = urlsplit(url).netloc.lower()
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                resp, content = _get_limited(client, current)
            except httpx.HTTPError as e:
                return FetchResult(None, None, f"{type(e).__name__}: {e}")
            except ValueError as e:
                return FetchResult(None, None, str(e), blocked=True)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                current = urljoin(current, resp.headers["location"])
                parts = urlsplit(current)
                if parts.scheme != "https" or parts.netloc.lower() != origin_host:
                    return FetchResult(resp.status_code, None,
                                       f"redirect to {parts.scheme}://{parts.netloc} refused (https and same host only)",
                                       blocked=True)
                reason = guard(current) if guard else None
                if reason:
                    return FetchResult(resp.status_code, None, reason, blocked=True)
                continue
            break
        else:
            return FetchResult(None, None, f"more than {MAX_REDIRECTS} redirects", blocked=True)
        if resp.status_code >= 400:
            return FetchResult(resp.status_code, None, f"HTTP {resp.status_code}",
                               blocked=resp.status_code in BLOCK_STATUSES)
        try:
            is_pdf = content[:5] == b"%PDF-"
            if not is_pdf and current.lower().split("?")[0].endswith(".pdf"):
                # a non-PDF body on a .pdf URL is a block page
                return FetchResult(resp.status_code, None, "expected PDF, got non-PDF body (likely CDN block)", blocked=True)
            if is_pdf:
                text = pdf_to_text(content)
            else:
                body = content.decode(resp.encoding or "utf-8", errors="replace")
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
    """httpx only. Honest User-Agent, https only, robots.txt first, no retry or workaround when blocked."""

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=False)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | str] = {}

    def _robots_for(self, origin: str):
        """A parsed robots.txt, or a reason string meaning: do not fetch from this host."""
        if origin in self._robots:
            return self._robots[origin]
        result: urllib.robotparser.RobotFileParser | str
        try:
            r = self.client.get(origin + "/robots.txt")
        except httpx.HTTPError as e:
            result = f"robots.txt unavailable: {type(e).__name__}"
        else:
            ctype = r.headers.get("content-type", "").lower()
            if r.status_code in (404, 410):
                result = urllib.robotparser.RobotFileParser()  # no robots.txt: nothing is disallowed
                result.parse([])
            elif r.status_code != 200:
                result = f"robots.txt unavailable: HTTP {r.status_code}"  # 429, 401, 403, 5xx, ...
            elif "html" in ctype or r.text.lstrip()[:1] == "<":
                result = "robots.txt unavailable: got an HTML page (challenge or error page)"
            else:
                result = urllib.robotparser.RobotFileParser()
                result.parse(r.text.splitlines())
        self._robots[origin] = result
        return result

    def guard(self, url: str) -> str | None:
        """Reason not to fetch url (not https, or robots.txt), else None."""
        u = urlsplit(url)
        if u.scheme != "https":
            return f"only https URLs are fetched (got {u.scheme or 'none'})"
        rp = self._robots_for(f"{u.scheme}://{u.netloc}")
        if isinstance(rp, str):
            return rp
        if not rp.can_fetch(ROBOTS_AGENT, url):
            return "disallowed by robots.txt"
        return None

    def __call__(self, url: str) -> FetchResult:
        reason = self.guard(url)
        if reason:
            return FetchResult(None, None, reason, blocked=True)
        return http_fetch(url, self.client, guard=self.guard)

    def close(self) -> None:
        self.client.close()


def manual_run(now: str, reason: str, item: dict | None = None) -> dict:
    """Run record for an item whose source we must not fetch automatically (no request is made)."""
    return {"at": now, "http_status": None, "quote_found": None, "page_sha256": None,
            "error": None, "network_error": False, "blocked": False,
            "manual_check_required": True, "manual_reason": reason,
            "content_hash": content_hash(item) if item else None}


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
        "content_hash": content_hash(item),  # the content version this result is about
    }
    if res.text is not None:
        norm = normalize(res.text)
        run["quote_found"] = normalize(item["quote"]) in norm
        run["page_sha256"] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return run


def next_status(item: dict, run: dict) -> str:
    """Status to store. A verified item without sound verification (missing reviewer data or content
    changed since review) counts as needs_review; a sound verified item is demoted only when its
    quote is definitely gone. Everything else keeps its status."""
    status = effective_status(item)
    if status == "verified" and run["quote_found"] is False:
        return "needs_review"
    return status
