"""Fetch a source page, check that the quote is still in it. Never rewrites item content."""
import hashlib
import io
import re
import time
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
BLOCK_STATUSES = {401, 403, 406, 429, 503}
MIN_TEXT_LEN = 500  # shorter HTML text is a JS shell or challenge, not the real page
BROWSER_TIMEOUT_MS = 60000
PDF_RETRY_DELAYS = (3.0, 8.0)  # PDF hosts block intermittently and the browser is not an option for PDFs
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
    blocked: bool = False  # looks like a CDN block / challenge / empty JS shell: worth retrying in a browser
    method: str = "http"  # "http" | "browser"


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
                # PDFs never go to the browser; a non-PDF body here is a block page
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


def browser_fetch(url: str) -> FetchResult:
    """Render the page in headless chromium. Gives up (no CAPTCHA solving) if still blocked."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-NZ",
                                          viewport={"width": 1366, "height": 900})
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                html = page.content()  # full DOM, includes collapsed accordion text
                status = resp.status if resp else None
            finally:
                browser.close()
    except Exception as e:
        return FetchResult(None, None, f"browser failed: {type(e).__name__}: {e}", method="browser")
    if status is not None and status >= 400:
        return FetchResult(status, None, f"HTTP {status} (browser)", method="browser")
    text = html_to_text(html)
    if len(normalize(text)) < MIN_TEXT_LEN or (
            any(m in html.lower() for m in BLOCK_MARKERS) and len(html) < 5000):
        return FetchResult(status, None, "still blocked in browser", method="browser")
    return FetchResult(status, text, None, method="browser")


def fetch_page(url: str, client: httpx.Client | None = None, browser=browser_fetch) -> FetchResult:
    """httpx first; blocked/challenge/too-short HTML (never PDF) is retried once in a browser."""
    res = http_fetch(url, client)
    if url.lower().split("?")[0].endswith(".pdf"):
        for delay in PDF_RETRY_DELAYS:
            if res.text is not None or not res.blocked:
                break
            time.sleep(delay)
            res = http_fetch(url, client)
        return res
    if res.text is not None or not res.blocked:
        return res
    out = browser(url)
    if out.text is None:
        out.error = f"{res.error}; browser: {out.error}"
    return out


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
        "fetch_method": res.method,
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
