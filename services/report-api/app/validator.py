"""Sentence validator for report summaries (INTERFACE_DRAFT 3.2, 3.3). Pure functions, no I/O.

A sentence passes only when every rule holds; `check_sentence` returns the list of failed rule codes
(empty = pass). The evidence a sentence may use is built once per request by `build_evidence`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from b_common.report_schemas import ReportRequest, Sentence

MAX_WORDS = 25
NO_LOAN_WARNINGS = ("deposit_exceeds_price", "cash_purchase_no_loan", "price_missing")
LENDING_SOURCES = {"rbnz_lvr", "rbnz_dti", "kainga_ora_fhl", "kainga_ora_fhl_brochure", "bank_rates_snapshot"}
HIGH_CLASSES = {"high", "very high"}
PREFIX = "Based on what you told us, "
PREFIX_RE = re.compile(r"^\s*based on what you (?:told|gave) us\s*,?\s*", re.I)

# Display precision per kind of field (R1): percent 1 decimal, money and areas whole units, rates 2 decimals.
P_PCT, P_MONEY, P_RATE = 1, 0, 2

# Rule codes returned by check_sentence.
NO_SOURCE_IDS = "no_source_ids"
UNKNOWN_SOURCE_ID = "unknown_source_id"
KB_NOT_SELECTED = "kb_not_selected"
NUMBER_NOT_IN_CITED_SOURCES = "number_not_in_cited_sources"
COMPUTED_VALUE = "computed_value"
ABBREVIATED_NUMBER = "abbreviated_number"
NUMBER_WORD = "number_word"
BANNED_WORD = "banned_word"
TOO_LONG = "too_long"
URL_OR_HTML = "url_or_html"
INTERNAL_JARGON = "internal_jargon"
LANDSLIDE_RULE = "landslide_rule"
LENDING_WITHOUT_LOAN = "lending_without_loan"
EMPTY_TEXT = "empty_text"
UNSUPPORTED_ABSENCE = "unsupported_absence"
INVALID_NUMBER_FORMAT = "invalid_number_format"

_NUM_RE = re.compile(r"(?<![\w.,])([-−]?)(?:NZ)?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(s\b)?")
_ABBREV_RE = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?\s?(?:k|m|bn)\b", re.I)
_NUMBER_WORD_RE = re.compile(
    r"\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|million|billion|percent|per cent)\b", re.I)
_URL_RE = re.compile(r"https?://|www\.|<\s*/?\s*[a-z!]|\]\(|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S", re.I)
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)


def host_of(url: Any) -> Optional[str]:
    """Lower-case host of a URL with a leading www. removed; None if there is no host."""
    if not isinstance(url, str) or not url.strip():
        return None
    host = urlparse(url.strip() if "//" in url else "//" + url.strip()).hostname
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host
_JARGON_RE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b|\brules assessment\b|\bstandard loan scenario\b|"
                        r"\brules-api\b|\bRulesOutput\b|\bPropertyContext\b", re.I)
# Verdicts, advice, predictions and approximations (RUBRIC 7). Words in QUOTABLE are allowed when
# they appear in the claim_en of a cited KB item (quoting the source, e.g. "will not provide funding").
_BANNED = [
    r"safe", r"safer", r"unsafe", r"no risk", r"risk-free", r"low risk", r"high risk", r"no hazards?",
    r"clear of", r"free of", r"recommend\w*", r"should", r"must", r"(?:good|bad|great) (?:deal|buy|investment|value|choice)",
    r"wise to", r"we suggest", r"great opportunity", r"no (?:flood|landslide|coastal) risk",
    r"ignore (?:all )?(?:previous|prior) (?:instructions|prompts)", r"bargain", r"avoid", r"worr(?:y|ied|ies)", r"(?:don't|do not) buy", r"buy", r"likely", r"unlikely",
    r"will", r"about", r"around", r"approximately", r"approx", r"roughly", r"nearly", r"almost", r"guarantee\w*",
]
_BANNED_RE = [(w, re.compile(rf"\b{w}\b", re.I)) for w in _BANNED]
QUOTABLE = {"should", "must", "will", "likely", "unlikely", "avoid"}
_LENDING_TEXT_RE = re.compile(r"\b(?:loans?|LVR|DTI|mortgage|repayments?|lend\w*|borrow\w*|deposit|"
                              r"finance|financing|credit|debt servicing|interest payments?|loan-to-value|debt-to-income|First Home Loan)\b", re.I)


@dataclass(frozen=True)
class Allowed:
    value: Decimal
    precision: Optional[int] = None  # R1: the value rounded to this many decimals also matches


@dataclass
class Evidence:
    known_ids: set[str] = field(default_factory=set)
    selected_kb: set[str] = field(default_factory=set)
    kb_claims: dict[str, str] = field(default_factory=dict)
    kb_topics: dict[str, str] = field(default_factory=dict)
    allowed: dict[str, list[Allowed]] = field(default_factory=dict)
    computed: set[Decimal] = field(default_factory=set)
    user_kb: set[str] = field(default_factory=set)
    no_loan: bool = False
    landslide_high: bool = False
    chat_year: bool = False
    unavailable_hazards: set[str] = field(default_factory=set)
    # id (source id or selected kb_ id) -> host of the page it cites. A sentence may name only these hosts.
    cited_hosts: dict[str, set[str]] = field(default_factory=dict)


# ---- number helpers -----------------------------------------------------------------------------
def to_decimal(x: Any) -> Optional[Decimal]:
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        try:
            d = Decimal(str(x))
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def numbers_in(text: str) -> list[Decimal]:
    """Numbers in text after stripping $, NZ$, thousands separators and %; "1990s" gives 1990."""
    out = []
    for m in _NUM_RE.finditer(text):
        out.append(Decimal(("-" if m.group(1) else "") + m.group(2).replace(",", "") + (m.group(3) or "")))
    return out


def _round(d: Decimal, places: int) -> Decimal:
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _year(s: Any) -> Optional[Decimal]:
    if isinstance(s, str) and re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return Decimal(s[:4])
    return None


def _matches(n: Decimal, allowed: Iterable[Allowed]) -> bool:
    for a in allowed:
        if n == a.value or (a.precision is not None and n == _round(a.value, a.precision)):
            return True
    return False


# ---- evidence -----------------------------------------------------------------------------------
def _money_or_area(name: str) -> int:
    return P_PCT if name.endswith("_pct") else P_MONEY


def build_evidence(req: ReportRequest, kb_items: dict[str, dict], rules_sources: Iterable[str],
                   source_urls: Optional[dict[str, str]] = None) -> Evidence:
    """Everything a summary sentence may cite or quote, for one request.

    source_urls maps rules-api source ids to URLs; when omitted, a `urls` attribute on rules_sources
    (as kb.py provides) is used."""
    ev = Evidence()
    urls: dict[str, str] = dict(source_urls or getattr(rules_sources, "urls", None) or {})
    for s in req.sources():
        if (s.model_extra or {}).get("url"):
            urls[s.id] = s.model_extra["url"]
    hosts: dict[str, set[str]] = {}
    for sid, u in urls.items():
        h = host_of(u)
        if h:
            hosts.setdefault(sid, set()).add(h)
    ev.chat_year = "year_built" in req.chat_supplied()
    ev.unavailable_hazards = {h.source_id for h in req.hazards()
                              if h.source_id and h.status in ("not_found", "unavailable")}
    selected = [i for i in req.selected_kb_ids() if i in kb_items]
    ev.selected_kb = set(selected)
    ev.kb_claims = {i: kb_items[i].get("claim_en") or "" for i in selected}
    ev.kb_topics = {i: kb_items[i].get("topic") or "" for i in selected}
    ev.user_kb = set(req.kb_items_from_user_input())
    for i in selected:
        h = host_of(kb_items[i].get("source_url")) or host_of(urls.get(kb_items[i].get("source_id") or ""))
        if h:
            hosts.setdefault(i, set()).add(h)
    ev.cited_hosts = hosts
    ev.known_ids = {s.id for s in req.sources()} | set(rules_sources) | ev.selected_kb

    allowed: dict[str, list[Allowed]] = {}

    def add(sid: Optional[str], value: Any, precision: Optional[int] = None) -> None:
        d = value if isinstance(value, Decimal) else to_decimal(value)
        if sid and d is not None:
            allowed.setdefault(sid, []).append(Allowed(d, precision))

    for h in req.hazards():
        add(h.source_id, h.overlap_pct, P_PCT)
        add(h.source_id, _year(h.data_date))
    for name, f in req.property_fields().items():
        if not f.source_id:
            continue
        add(f.source_id, f.value, _money_or_area(name))
        extra = f.model_extra or {}
        for k, v in extra.items():
            if k.endswith("_date"):
                add(f.source_id, _year(v))
        if name == "year_built" and isinstance(extra.get("decade_range"), list):
            for y in extra["decade_range"]:
                add(f.source_id, y)
    ro = req.rules_output
    add("bank_rates_snapshot", _year(ro.get("rates_snapshot_date")))
    for s in req.scenarios():
        for b in s.get("per_bank") or []:
            if isinstance(b, dict):
                add("bank_rates_snapshot", b.get("rate_pct"), P_RATE)
        for caps in (s.get("income_caps") or {}).values():
            add("kainga_ora_fhl", caps, P_MONEY)
            add("kainga_ora_fhl_brochure", caps, P_MONEY)
    for cap in (ro.get("fhl_income_caps") or {}).values():
        add("kainga_ora_fhl", cap, P_MONEY)
        add("kainga_ora_fhl_brochure", cap, P_MONEY)
    for i in selected:
        for n in numbers_in(ev.kb_claims[i]):
            add(i, n)
    ev.allowed = allowed
    ev.computed = computed_values(req)

    warnings = set(req.warnings())
    loan = to_decimal(req.rules_output.get("loan_amount"))
    ev.no_loan = any(w in warnings for w in NO_LOAN_WARNINGS) or (loan is not None and loan <= 0)
    for h in req.hazards():
        if h.layer.startswith("landslide") and h.status == "intersects":
            classes = [c.lower() for c in h.classes] + ([h.susceptibility.lower()] if h.susceptibility else [])
            if HIGH_CLASSES & set(classes):
                ev.landslide_high = True
    return ev


_COMPUTED_KEYS = ("loan_amount", "lvr_pct", "dti", "high_dti_threshold", "lmi_amount", "lmi_amount_if_full_premium",
                  "loan_with_lmi", "monthly", "fortnightly", "monthly_if_full_premium")


def computed_values(req: ReportRequest) -> set[Decimal]:
    """Values rules-api calculated (and the deposit share), with their whole-number and 1-decimal roundings."""
    raw: list[Decimal] = []

    def take(x: Any) -> None:
        if isinstance(x, list):
            for v in x:
                take(v)
            return
        d = to_decimal(x)
        if d is not None:
            raw.append(d)

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k in _COMPUTED_KEYS or k.startswith(("monthly", "fortnightly", "stress_", "lmi_amount")):
                    take(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    ro = req.rules_output
    walk(ro)
    price = to_decimal((ro.get("price_used") or {}).get("value"))
    deposit = to_decimal(ro.get("deposit"))
    if price and deposit is not None and price > 0:
        raw.append(deposit / price * 100)
    out: set[Decimal] = set()
    for d in raw:
        out.update({d, _round(d, 0), _round(d, 1)})
    return out


# ---- sentence checks ----------------------------------------------------------------------------
def strip_prefix(text: str) -> str:
    return PREFIX_RE.sub("", text, count=1)


def word_count(text: str) -> int:
    return len(strip_prefix(text).split())


def check_sentence(s: Sentence, ev: Evidence, max_words: Optional[int] = MAX_WORDS) -> list[str]:
    """Failed rule codes for one summary sentence; [] means it passes."""
    text = s.text or ""
    fails: list[str] = []
    if not text.strip():
        return [EMPTY_TEXT]
    ids = list(dict.fromkeys(s.source_ids or []))
    if not ids:
        fails.append(NO_SOURCE_IDS)
    for i in ids:
        if not i.strip():
            fails.append(UNKNOWN_SOURCE_ID)
        elif i.startswith("kb_"):
            if i not in ev.selected_kb:
                fails.append(KB_NOT_SELECTED)
        elif i not in ev.known_ids:
            fails.append(UNKNOWN_SOURCE_ID)
    cited_kb = [i for i in ids if i in ev.selected_kb]
    kb_allowed = [a for i in cited_kb for a in ev.allowed.get(i, [])]
    field_allowed = [a for i in ids if not i.startswith("kb_") and i in ev.known_ids for a in ev.allowed.get(i, [])]

    if _ABBREV_RE.search(text):
        fails.append(ABBREVIATED_NUMBER)
    if _NUMBER_WORD_RE.search(text):
        fails.append(NUMBER_WORD)
    # Every numeric character must belong to a complete, canonical token. Never silently
    # skip a glued number or parse only the first part of a malformed decimal.
    spans = list(_NUM_RE.finditer(text))
    covered = {i for m in spans for i in range(m.start(), m.end())}
    if (any(c.isnumeric() and (c not in "0123456789" or i not in covered)
            for i, c in enumerate(text))
            or any(m.end() < len(text) and (text[m.end()].isalnum() or text[m.end()] == "_")
                   for m in spans)
            or re.search(r"[0-9]\s+[0-9]", text)):
        fails.append(INVALID_NUMBER_FORMAT)
    for n in numbers_in(text):
        if _matches(n, kb_allowed):
            continue  # a number quoted from a cited KB claim is valid even if it equals a computed value
        if n in ev.computed or n == 0:
            fails.append(COMPUTED_VALUE)
        elif not _matches(n, field_allowed):
            fails.append(NUMBER_NOT_IN_CITED_SOURCES)

    claims = " ".join(ev.kb_claims.get(i, "") for i in cited_kb).lower()
    for word, rx in _BANNED_RE:
        m = rx.search(text)
        if m and not (word in QUOTABLE and re.search(rf"\b{word}\b", claims)):
            fails.append(BANNED_WORD)
            break
    # Only the bare domain of a page this sentence cites may appear (a publisher name, "Settled.govt.nz says").
    cited_hosts = {h for i in ids for h in ev.cited_hosts.get(i, ())}
    if (_URL_RE.search(text) or "@" in text
            or any(m.group(0).lower() not in cited_hosts for m in _DOMAIN_RE.finditer(text))):
        fails.append(URL_OR_HTML)
    if _JARGON_RE.search(text):
        fails.append(INTERNAL_JARGON)
    if max_words is not None and word_count(text) > max_words:
        fails.append(TOO_LONG)
    if (ev.unavailable_hazards.intersection(ids)
            and re.search(r"\b(?:shows? no|does not show|do not show|no flooding|no flood hazard)\b", text, re.I)):
        fails.append(UNSUPPORTED_ABSENCE)
    landslide = "ac_landslide" in ids or "kb_landslide_map_context" in ids or re.search(r"\blandslide", text, re.I)
    if landslide and (not ev.landslide_high or "kb_landslide_map_context" not in ids):
        fails.append(LANDSLIDE_RULE)
    if ev.no_loan and (any(i in LENDING_SOURCES or ev.kb_topics.get(i) == "lending" for i in ids)
                       or _LENDING_TEXT_RE.search(text)):
        fails.append(LENDING_WITHOUT_LOAN)
    return list(dict.fromkeys(fails))


@dataclass
class Verdict:
    passed: list[Sentence]
    rejected: list[tuple[Sentence, list[str]]]

    @property
    def dropped(self) -> int:
        return len(self.rejected)


def validate(sentences: Iterable[Sentence], ev: Evidence, max_words: Optional[int] = MAX_WORDS) -> Verdict:
    v = Verdict([], [])
    for s in sentences:
        fails = check_sentence(s, ev, max_words)
        if fails:
            v.rejected.append((s, fails))
        else:
            v.passed.append(s)
    return v


_USER_FACT_RE = re.compile(r"\b(?:you (?:gave|told|said|mentioned|supplied|provided)|your (?:stated|supplied|given))\b", re.I)
_USER_TOPIC_RE = re.compile(r"\b(?:year|built|build|decade|cladding)\b", re.I)


def relies_on_user_input(s: Sentence, ev: Evidence) -> bool:
    """True when a sentence uses chat-supplied facts: it cites a kb_items_from_user_input item, or it
    refers to the year or cladding the user gave."""
    if any(i in ev.user_kb for i in s.source_ids):
        return True
    body = strip_prefix(s.text)
    # A comparison may omit the user's year entirely; retain provenance when the
    # model says it chose the council figure in a request containing a chat year.
    if (ev.chat_year and "ac_property_page" in s.source_ids
            and re.search(r"\b(?:uses? the council figure|council figure takes precedence)\b", body, re.I)):
        return True
    return bool(_USER_FACT_RE.search(body) and _USER_TOPIC_RE.search(body))


def with_prefix(s: Sentence) -> Sentence:
    body = strip_prefix(s.text).strip()
    if len(body) > 1 and not body[1].isupper():  # keep acronyms such as "LINZ"
        body = body[:1].lower() + body[1:]
    return Sentence(text=PREFIX + body, source_ids=s.source_ids)
