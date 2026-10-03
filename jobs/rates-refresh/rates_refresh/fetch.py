"""Page fetching: plain HTTP first, Playwright when blocked or configured."""
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
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
    fell_back: bool = False

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


def detect_block(status: int | None, text: str, raw: str = "") -> str | None:
    """Return a reason string if the response looks like a block / challenge page."""
    if status in BLOCK_STATUSES:
        return f"http_{status}"
    sample = raw or text
    if BLOCK_MARKERS.search(sample) and len(text) < 5000:
        return "challenge_page"
    if len(text) < MIN_TEXT_LEN:
        return f"short_content_{len(text)}"
    # JS-rendered pages return a shell with no actual rate table over plain HTTP
    plausible = [float(m) for m in RATE_RE.findall(text) if 2.0 <= float(m) <= 12.0]
    if len(plausible) < MIN_RATES_ON_PAGE:
        return f"no_rates_in_content_{len(plausible)}"
    return None


def http_fetch(url: str) -> FetchResult:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-NZ,en;q=0.9"}
    with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as c:
        r = c.get(url)
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
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3000)  # rate tables are often filled by a late XHR
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
            status = resp.status if resp else None
        finally:
            browser.close()
    text = normalize_text(text)
    reason = detect_block(status, text)
    return FetchResult(text, "browser", status, reason is not None, reason)


def fetch_bank(bank: dict, http=http_fetch, browser=browser_fetch) -> FetchResult:
    """Fetch per config; switch to the browser if HTTP is blocked or errors out."""
    url = bank["source_url"]
    if bank.get("fetch") == "browser":
        return browser(url)
    try:
        res = http(url)
    except httpx.HTTPError:
        res = None
    if res is not None and not res.blocked:
        return res
    out = browser(url)
    out.fell_back = True
    return out
