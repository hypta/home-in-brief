"""Polite page fetching for banks whose terms allow automated access.

Identifies itself honestly, obeys robots.txt, and stops (never retries another way) when a site blocks us.
Playwright is used only when banks.yaml says `render: browser` (the page needs JavaScript to show rates).
"""
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import urllib.robotparser
from urllib.parse import urlsplit

import httpx

USER_AGENT = "home-in-brief/0.1 (+https://github.com/hypta/home-in-brief)"
ROBOTS_TOKEN = "home-in-brief"
MIN_TEXT_LEN = 1500
MIN_RATES_ON_PAGE = 8
RATE_RE = re.compile(r"\b(\d{1,2}\.\d{2})\s*%")
BLOCK_STATUSES = {401, 403, 406, 429, 503}
BLOCK_MARKERS = re.compile(
    r"incapsula|imperva|_incapsula_resource|access denied|request unsuccessful|"
    r"verify you are human|captcha|request blocked|enable javascript",
    re.I,
)


@dataclass
class FetchResult:
    text: str
    method: str  # "http" | "browser"
    status: int | None
    blocked: bool
    block_reason: str | None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def normalize_text(text: str) -> str:
    text = re.sub(r"(\d)\s*\n\s*%", r"\1%", text)  # "4.85\n%" -> "4.85%" (value and unit in separate elements)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return normalize_text(text)


def detect_guard(status: int | None, text: str, raw: str = "") -> str | None:
    """Reason if the site is refusing us (block status or challenge page); checked before anything else."""
    if status in BLOCK_STATUSES:
        return f"http_{status}"
    if BLOCK_MARKERS.search(raw or text) and len(text) < 5000:
        return "challenge_page"
    return None


def detect_block(status: int | None, text: str, raw: str = "") -> str | None:
    """Return a reason string if the response is a block / challenge page or has no usable rates."""
    reason = detect_guard(status, text, raw)
    if reason:
        return reason
    if len(text) < MIN_TEXT_LEN:
        return f"short_content_{len(text)}"
    # JS-rendered pages return a shell with no actual rate table over plain HTTP
    plausible = [float(m) for m in RATE_RE.findall(text) if 2.0 <= float(m) <= 12.0]
    if len(plausible) < MIN_RATES_ON_PAGE:
        return f"no_rates_in_content_{len(plausible)}"
    return None


class RedirectBlocked(Exception):
    """A redirect left the approved host or downgraded to http."""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def safe_get(url: str, max_hops: int = 3, client=None, allow=None) -> httpx.Response:
    """GET without auto-redirects: every hop must be https, on the approved host, and (if `allow` is given)
    pass allow(url) -> (ok, reason), e.g. a robots.txt check for the new path."""
    approved = _host(url)
    own = client is None
    client = client or httpx.Client(follow_redirects=False, timeout=20,
                                    headers={"User-Agent": USER_AGENT, "Accept-Language": "en-NZ,en;q=0.9"})
    try:
        for _ in range(max_hops + 1):
            r = client.get(url)
            if r.status_code not in (301, 302, 303, 307, 308):
                return r
            url = str(httpx.URL(url).join(r.headers.get("location", "")))
            if urlsplit(url).scheme != "https" or _host(url) != approved:
                raise RedirectBlocked(f"redirect to {_host(url) or 'unknown'} (approved host {approved})")
            if allow is not None:
                ok, why = allow(url)
                if not ok:
                    raise RedirectBlocked(f"redirect target not allowed: {why}")
        raise RedirectBlocked("too many redirects")
    finally:
        if own:
            client.close()


def http_fetch(url: str) -> FetchResult:
    try:
        r = safe_get(url, allow=robots_allows)
    except RedirectBlocked as e:
        return FetchResult("", "http", None, True, f"redirect_blocked: {e}")
    text = html_to_text(r.text)
    reason = detect_block(r.status_code, text, r.text)
    return FetchResult(text, "http", r.status_code, reason is not None, reason)


EXPAND_JS = """
() => {
  let n = 0;
  document.querySelectorAll('[aria-expanded="false"]:not(header *):not(nav *):not([role="tab"]), summary, [data-toggle="collapse"], .accordion-header, .accordion__trigger')
    .forEach(el => { try { el.click(); n++; } catch (e) {} });
  return n;
}
"""


def _expanded_text(page) -> str:
    for _ in range(2):  # a second pass catches nested accordions
        page.evaluate(EXPAND_JS)
        page.wait_for_timeout(500)
    return page.inner_text("body")


def browser_fetch(url: str) -> FetchResult:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT, locale="en-NZ", viewport={"width": 1366, "height": 900}
            )
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp else None
            if _host(page.url) != _host(url) or urlsplit(page.url).scheme != "https":
                return FetchResult("", "browser", status, True, f"redirect_blocked: landed on {_host(page.url)}")
            if page.url != url:  # followed a redirect inside the browser: robots must allow the new path too
                ok, why = robots_allows(page.url)
                if not ok:
                    return FetchResult("", "browser", status, True, f"redirect_blocked: {why}")
            # stop before any interaction if the site is refusing us
            first = page.inner_text("body")
            reason = detect_guard(status, first)
            if reason:
                return FetchResult(first, "browser", status, True, reason)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3000)  # rate tables are often filled by a late XHR
            if _host(page.url) != _host(url):  # navigation may have moved on (script redirect, challenge)
                return FetchResult("", "browser", status, True, f"redirect_blocked: landed on {_host(page.url)}")
            reason = detect_guard(status, page.inner_text("body"))
            if reason:
                return FetchResult("", "browser", status, True, reason)
            text = _expanded_text(page)
            # tabbed pages only render the active tab: click each tab, keep lines not seen yet
            seen = set(text.split("\n"))
            tabs = page.locator('[role="tab"]')
            for i in range(min(tabs.count(), 20)):
                try:
                    tabs.nth(i).evaluate("el => el.click()")
                    page.wait_for_timeout(600)
                    extra = [ln for ln in _expanded_text(page).split("\n") if ln not in seen]
                    seen.update(extra)
                    text += "\n" + "\n".join(extra)
                except Exception:
                    continue
        finally:
            browser.close()
    text = normalize_text(text)
    reason = detect_block(status, text)
    return FetchResult(text, "browser", status, reason is not None, reason)


def robots_allows(url: str, getter=None) -> tuple[bool, str]:
    """Only a readable robots.txt (200) or a plain 404/410 lets us proceed.

    Rate limits (429), auth walls (401/403), other 4xx, 5xx, redirects off-host, timeouts and HTML challenge pages
    served in place of robots.txt all mean "do not fetch".
    """
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    getter = getter or _get_robots
    try:
        status, body = getter(robots_url)
    except (httpx.HTTPError, RedirectBlocked) as e:
        return False, f"robots_unreachable: {type(e).__name__}"
    if status in (404, 410):
        return True, "no_robots_txt"
    if status != 200:
        return False, f"robots_status_{status}"
    if re.search(r"<\s*(html|!doctype|body|head)\b", body[:2000], re.I):
        return False, "robots_not_text"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.splitlines())
    if rp.can_fetch(ROBOTS_TOKEN, url):
        return True, "robots_ok"
    return False, "robots_disallow"


def _get_robots(robots_url: str) -> tuple[int, str]:
    r = safe_get(robots_url)
    return r.status_code, r.text


def fetch_bank(bank: dict, http=http_fetch, browser=browser_fetch, robots=robots_allows) -> FetchResult:
    """One attempt, honest client. A block, challenge page or robots Disallow is final: no retry another way."""
    url = bank["source_url"]
    ok, why = robots(url)
    if not ok:
        return FetchResult("", "none", None, True, why)
    try:
        return (browser if bank.get("render") == "browser" else http)(url)
    except httpx.HTTPError as e:
        return FetchResult("", "http", None, True, f"http_error: {type(e).__name__}")
    except RedirectBlocked as e:
        return FetchResult("", "http", None, True, f"redirect_blocked: {e}")
