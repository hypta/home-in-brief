"""Fetch a source page, check that the quote is still in it. Never rewrites item content."""
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = 20.0
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


def fetch_page(url: str, client: httpx.Client | None = None) -> FetchResult:
    own = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    try:
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            return FetchResult(None, None, f"{type(e).__name__}: {e}")
        if resp.status_code >= 400:
            return FetchResult(resp.status_code, None, f"HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "").lower()
        try:
            if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf") or resp.content[:5] == b"%PDF-":
                text = pdf_to_text(resp.content)
            else:
                body = resp.text
                if any(m in body.lower() for m in BLOCK_MARKERS) and len(body) < 5000:
                    return FetchResult(resp.status_code, None, "blocked by CDN bot challenge")
                text = html_to_text(body)
        except Exception as e:  # extraction failure is ours, not the network's
            return FetchResult(resp.status_code, None, f"extract failed: {type(e).__name__}: {e}", False)
        return FetchResult(resp.status_code, text, None)
    finally:
        if own:
            client.close()


def check_item(item: dict, fetch=fetch_page, now: str = "") -> dict:
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
