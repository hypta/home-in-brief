"""Sentence validator for report summaries. Pure functions, no I/O.

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
# A value code extracted from a Google Search answer is not something the user told us.
WEB_PREFIX = "Based on a web search (not checked by this service), "
PREFIX_RE = re.compile(r"^\s*based on (?:what you (?:told|gave) us|a web search(?: \(not checked by this service\))?)"
                       r"\s*,?\s*", re.I)

# Display precision per kind of field: percent 1 decimal, money and areas whole units, rates 2 decimals.
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
# Verdicts, advice, predictions and approximations. Words in QUOTABLE are allowed when
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
    precision: Optional[int] = None  # the value rounded to this many decimals also matches


@dataclass
class Evidence:
    known_ids: set[str] = field(default_factory=set)
    selected_kb: set[str] = field(default_factory=set)
    kb_claims: dict[str, str] = field(default_factory=dict)
    kb_topics: dict[str, str] = field(default_factory=dict)
    allowed: dict[str, list[Allowed]] = field(default_factory=dict)
    computed: set[Decimal] = field(default_factory=set)
    # follow-up only, never on the request's shared Evidence: the one computed value a code-written program sentence may
    # show, bound to the source id it cites (the buyer's own LVR; prompt.check_program sets it for that sentence alone).
    # Everything else in `computed` stays blocked.
    typed_computed: dict[str, list[Allowed]] = field(default_factory=dict)
    user_kb: set[str] = field(default_factory=set)
    no_loan: bool = False
    landslide_high: bool = False
    chat_year: bool = False
    unavailable_hazards: set[str] = field(default_factory=set)
    # id (source id or selected kb_ id) -> host of the page it cites. A sentence may name only these hosts.
    cited_hosts: dict[str, set[str]] = field(default_factory=dict)
    # follow-up only: English names and values a Chinese/Japanese/Korean answer may contain (followup.latin_terms)
    latin_terms: set[str] = field(default_factory=set)
    # follow-up only: source id -> English text of the facts citing it (a Chinese 可能 needs a may in here or in kb_claims)
    fact_text: dict[str, str] = field(default_factory=dict)


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
            if n != 0 and _matches(n, [a for i in ids for a in ev.typed_computed.get(i, [])]):
                continue  # the one typed value the cited source is bound to
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


def _same_sentence(a: str, b: str) -> bool:
    norm = lambda t: re.sub(r"\s+", " ", strip_prefix(t)).strip().lower()  # noqa: E731
    return norm(a) == norm(b)


def user_year_facts(facts: Any) -> list:
    """Summary facts that state the year the user typed (they have a `user_year` slot)."""
    return [f for f in facts if any(sl.name == "user_year" and sl.kind in ("year", "decade")
                                    for sl in f.slots.values())]


def with_user_year(ev: Evidence, facts: Any, text: str) -> Evidence:
    """ev with the year the user typed allowed for exactly one sentence: `text` must be the code-written draft of one of
    `facts` (the prefix and the case of the first letter aside); the allowance is the value of its `user_year` slot
    under that fact's source ids. Any other sentence, even one citing the same source, gets ev unchanged."""
    from dataclasses import replace
    allowed = None
    for f in user_year_facts(facts):
        if not _same_sentence(text, f.draft()):
            continue
        for sl in f.slots.values():
            d = to_decimal(sl.value) if sl.name == "user_year" else None
            if d is not None:
                allowed = allowed or {k: list(v) for k, v in ev.allowed.items()}
                for sid in f.source_ids:
                    allowed.setdefault(sid, []).append(Allowed(d))
    return replace(ev, allowed=allowed) if allowed is not None else ev


def with_prefix(s: Sentence, prefix: str = PREFIX) -> Sentence:
    body = strip_prefix(s.text).strip()
    if len(body) > 1 and not body[1].isupper():  # keep acronyms such as "LINZ"
        body = body[:1].lower() + body[1:]
    return Sentence(text=prefix + body, source_ids=s.source_ids)


# ---- rewrite checks (fact list + placeholders) ----------------------------------------------------
# Gemini rewrites a list of code-built facts (facts.Fact). A rewrite names the facts it uses (fact_ids) and writes
# every value as a placeholder; code derives the source_ids from the facts and fills the values in.
NO_FACT_IDS = "no_fact_ids"
UNKNOWN_FACT_ID = "unknown_fact_id"
FOREIGN_PLACEHOLDER = "placeholder_not_in_fact_ids"
MALFORMED_PLACEHOLDER = "malformed_placeholder"
BARE_NUMBER = "bare_number"
RAW_FIELD_VALUE = "raw_field_value"
SLOT_CONTEXT = "slot_context"
MISSING_QUALIFIER = "missing_qualifier"
QUALIFIER_ORDER = "qualifier_order"
NEGATION_CHANGED = "negation_changed"
MISSING_ATTRIBUTION = "missing_attribution"
MIXED_USER_FACTS = "mixed_user_facts"
ADDED_ATTRIBUTION = "added_attribution"
USER_YEAR_REWRITTEN = "user_year_rewritten"
ADDED_CAUSE = "added_cause"

# Semantic guard (simple): a phrase of this list in a fact's text must also be in the rewrite. CJK rewrites
# (follow-up answers in Chinese, Japanese or Korean) may use any of the listed equivalents instead. Negations are
# counted separately.
QUALIFIERS: dict[str, tuple[str, ...]] = {
    "may": ("可能", "或許", "或许", "也許", "也许", "可", "かもしれ", "ことがあ", "場合があ", "수 있", "있을 수"),
    "can": ("可", "能", "でき", "得る", "수 있"),
    "could not": ("無法", "无法", "未能", "不能", "できなかった", "できません", "できず", "できない", "수 없", "못했"),
    "only": ("只", "僅", "仅", "のみ", "だけ", "에만", "뿐", "만 "),
    "limited": ("限", "制限", "제한", "한도"), "limits": ("限", "制限", "제한", "한도"),
    "at least": ("至少", "少なくとも", "以上", "최소", "적어도", "이상"),
    "generally": ("一般", "通常", "일반적", "보통"),
    "exempt": ("不受", "豁免", "不適用", "不适用", "対象外", "免除", "適用されない", "면제", "적용되지 않"),
    "unknown": ("未知", "不明", "無法得知", "わから", "分から", "알 수 없", "불명"),
    "on individual properties": ("個別", "个别", "單一", "单一", "個々", "개별"),
    "differs": ("不同", "不一致", "異な", "違", "다르", "다릅", "차이"),
    "same decade": ("同一個年代", "同一年代", "相同年代", "同一个年代", "同じ年代", "같은 연대"),
    "assumes": ("假設", "假设", "視為", "视为", "仮定", "想定", "みなし", "가정"),
    "within": ("以內", "之內", "以内", "之内", "未超過", "未超过", "範囲内", "이내", "이하"),
    "above": ("超過", "超过", "高於", "高于", "超え", "上回", "초과", "넘"),
    "if": ("如果", "若", "假如", "一旦", "場合", "もし", "なら", "경우", "만약", "면"),  # a dropped condition widens the claim
}
# Terms a fact uses that a rewrite must keep as written (dropping "LVR" from "exempt from Reserve Bank LVR limits"
# widens the claim). CJK rewrites keep them in English too.
TERMS = ("LVR", "DTI", "First Home Loan", "body corporate", "leaky")
TERMS_CJK = ("LVR", "DTI", "First Home Loan")  # kept in English in Chinese answers too
# Attribution: a publisher a fact names must be named in the rewrite too (CJK equivalents allowed).
PUBLISHERS: dict[str, tuple[str, ...]] = {
    "Auckland Council": ("Auckland Council", "council", "市議會", "市议会", "奧克蘭", "奥克兰", "市議会", "オークランド",
                         "오클랜드", "시의회"),
    "Settled.govt.nz": ("Settled.govt.nz",),
    "Reserve Bank": ("Reserve Bank", "RBNZ", "儲備銀行", "储备银行", "準備銀行", "준비은행"),
    "Kāinga Ora": ("Kāinga Ora", "Kainga Ora"),
    "LINZ": ("LINZ",),
    "unittitles.govt.nz": ("unittitles.govt.nz",),
    "Natural Hazards Commission": ("Natural Hazards Commission", "NHC", "自然災害委員會", "自然灾害委员会",
                                   "自然災害委員会", "자연재해위원회"),
}
_NEG_RE = re.compile(r"\b(?:not|no|never|neither|nor|without|cannot)\b|n't\b", re.I)
# A rewrite may not add a "says" (a gap in our data is not something the council "states") or a causal link
# ("your income is within the cap, making it exempt") that its facts do not have.
_SAY_RE = re.compile(r"\b(?:says?|said|states?|stated|stating|notes?|noted|noting|explains?|explained|describes?|"
                     r"described|discusses|discussed|according to|reports?|reported|warns?|advises?)\b", re.I)
# ", as no debts were entered" is a cause too; "as you entered", "describes it as" are not
_CAUSE_RE = re.compile(r"\b(?:because|so|therefore|thus|hence|since|making|makes|means?|meaning|as a result|due to|"
                       r"leads? to|results? in|(?<=, )as(?=\s+(?:no|there|its?|the|your|they|this|these|those)\b))\b",
                       re.I)
# Negation markers: Chinese, Japanese (ない, ません, ず, なかった), Korean (않, 없, 못, 아니)
_CJK_NEG_RE = re.compile(r"[不沒没未無无非否]|ない|ません|なかった|ず[、。に]|않|없|못|아니")
# Slot context (before/after regexes are English): in a CJK rewrite the same relation may be written with one of
# these words anywhere in the sentence (Japanese and Korean put "over" after the number).
_CJK_CONTEXT = {"over": r"超過|超过|高於|高于|以上|超え|上回|초과|넘", "at least": r"至少|以上|少なくとも|최소|이상",
                "times": r"倍|배"}
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


@dataclass
class Rewrite:
    """check_rewrite result: the filled sentence (source_ids derived from the facts) when it passes."""
    sentence: Optional[Sentence]
    fact_ids: list[str]           # the named ids that exist in the fact list
    fails: list[str]
    filled: str = ""              # the text with values filled in (also for blocked sentences, for logs)


def _phrase_re(p: str) -> re.Pattern:
    # ASCII boundaries, so a phrase glued to CJK text ("依據settled.govt.nz的說明") is still found
    return re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(p)}(?![A-Za-z0-9_-])", re.I)


def qualifiers_in(text: str) -> list[str]:
    """QUALIFIERS phrases a fact's text uses (what a rewrite must keep)."""
    words = re.sub(r"\{\{[^}]*\}\}", " ", text)
    return [q for q in QUALIFIERS if _phrase_re(q).search(words)]


def terms_in(text: str) -> list[str]:
    words = re.sub(r"\{\{[^}]*\}\}", " ", text)
    return [t for t in TERMS if re.search(rf"(?<![A-Za-z]){re.escape(t)}", words, re.I)]


def publishers_in(text: str) -> list[str]:
    return [p for p, alts in PUBLISHERS.items() if _phrase_re(alts[0]).search(text)
            or (p == "Auckland Council" and re.search(r"\bcouncil\b", text, re.I))]


def _cjk_context(english: str, text: str) -> bool:
    """True when `text` has a CJK word for the relation an English before/after regex names."""
    return any(k in english and re.search(rx, text) for k, rx in _CJK_CONTEXT.items())


def negations(text: str) -> int:
    return len(_NEG_RE.findall(re.sub(r"\{\{[^}]*\}\}", " ", text)))


def check_rewrite(s: Any, facts: Any, ev: Evidence, max_words: Optional[int] = MAX_WORDS,
                  final: Optional[Any] = None) -> Rewrite:
    """Check one rewritten sentence ({text, fact_ids}) against the fact list, fill in its placeholders, and run
    the sentence rules on the result (check_sentence, or `final(sentence)` for follow-up answers).

    Blocks: unknown or missing fact ids; a placeholder of a fact the sentence does not name, or a slot that fact
    does not have; leftover braces; any digit outside a placeholder; a strict string value (a title name, the
    council's land-use words) written without its placeholder; a placeholder away from its words (slot label,
    before/after context); a fact's qualifier, term (LVR, DTI, ...), publisher or number of negations not kept; user facts mixed with
    other facts. The prefix "Based on what you told us" is removed: code adds it for user facts."""
    from .facts import PLACEHOLDER_RE, TITLE_WORDS, fill, template_words

    fmap = facts if isinstance(facts, dict) else {f.id: f for f in facts}
    text = strip_prefix((getattr(s, "text", None) if not isinstance(s, dict) else s.get("text")) or "").strip()
    text = re.sub(r"\bA (?=Auckland\b)", "An ", text)  # the model's article before the publisher it has to name
    named = list(dict.fromkeys((getattr(s, "fact_ids", None) if not isinstance(s, dict) else s.get("fact_ids")) or []))
    ids = [i for i in named if i in fmap]
    fails: list[str] = []
    if not text:
        return Rewrite(None, ids, [EMPTY_TEXT])
    if not named:
        fails.append(NO_FACT_IDS)
    if len(ids) < len(named):
        fails.append(UNKNOWN_FACT_ID)
    cited = [fmap[i] for i in ids]
    # (summary only; follow-up answers have their own final check) the sentence with the user's own year is written by code: a rewrite may only repeat it (no added wording such
    # as "council-verified", no merge with another fact)
    if final is None and user_year_facts(cited) and not (len(cited) == 1 and _same_sentence(text, cited[0].text)):
        fails.append(USER_YEAR_REWRITTEN)
    cjk = bool(_CJK_RE.search(text))

    marks = list(PLACEHOLDER_RE.finditer(text))
    for m in marks:
        if m.group(1) not in ids or m.group(2) not in fmap[m.group(1)].slots:
            fails.append(FOREIGN_PLACEHOLDER)
    rest = PLACEHOLDER_RE.sub(" ", text)
    if "{" in rest or "}" in rest:
        fails.append(MALFORMED_PLACEHOLDER)
    if any(c.isnumeric() for c in rest.replace("一", "")):  # "一" is part of ordinary words (一般, 之一)
        fails.append(BARE_NUMBER)

    # strict string values (and every title name) may appear only through a placeholder, unless a cited fact's
    # own words contain them
    own = " ".join(template_words(f.text) for f in cited)
    strict = set(TITLE_WORDS) | {str(sl.value) for f in fmap.values() for sl in f.slots.values()
                                 if sl.strict and isinstance(sl.value, str)}
    for value in strict:
        rx = _phrase_re(value)
        if rx.search(rest) and not rx.search(own):
            fails.append(RAW_FIELD_VALUE)
            break

    # each placeholder stays next to its words
    labelled = [sl for f in cited for sl in f.slots.values() if sl.label]
    for m in marks:
        f = fmap.get(m.group(1))
        sl = f.slots.get(m.group(2)) if f and m.group(1) in ids else None
        if sl is None:
            continue
        before, after = PLACEHOLDER_RE.sub("X", text[:m.start()]), text[m.end():]
        if sl.before and not re.search(rf"(?:{sl.before})\s*$", before, re.I) and not (cjk and _cjk_context(sl.before, rest)):
            fails.append(SLOT_CONTEXT)
        if sl.after and not re.match(rf"\s*(?:{sl.after})", after, re.I) and not (cjk and _cjk_context(sl.after, rest)):
            fails.append(SLOT_CONTEXT)
        if sl.label:
            hits = [(h.start(), h.end(), o.label.lower()) for o in labelled
                    for h in _phrase_re(o.label).finditer(text)]
            prior = [h for h in hits if h[1] <= m.start()]
            near = max(prior, key=lambda h: h[1]) if prior else min(
                (h for h in hits if h[0] >= m.end()), key=lambda h: h[0], default=None)
            if near is None or near[2] != sl.label.lower():
                fails.append(SLOT_CONTEXT)

    # meaning: qualifiers, attribution and negations of the facts are kept
    for f in cited:
        for q in qualifiers_in(f.text):
            if not (_phrase_re(q).search(rest) or (cjk and any(a in rest for a in QUALIFIERS[q]))):
                fails.append(MISSING_QUALIFIER)
                break
        if not cjk:
            # the fact's qualifiers and keep phrases, all present and in the fact's order ("shown because your
            # income ... exempt" must not become "exempt ... because your income")
            own_words = template_words(f.text)
            phrases = set(qualifiers_in(f.text) + list(f.keep))
            phrases = {p for p in phrases if not any(p != o and p.lower() in o.lower() for o in phrases)}
            req = sorted(phrases,
                         key=lambda p: (m.start() if (m := _phrase_re(p).search(own_words)) else -1))
            pos = 0
            for p in req:
                m = _phrase_re(p).search(rest, pos)
                if m:
                    pos = m.end()
                elif _phrase_re(p).search(rest):
                    fails.append(QUALIFIER_ORDER)
                    break
                else:
                    fails.append(MISSING_QUALIFIER)
                    break
        for t in terms_in(f.text):
            if cjk and t not in TERMS_CJK:
                continue
            if not re.search(rf"(?<![A-Za-z]){re.escape(t)}", rest, re.I):
                fails.append(MISSING_QUALIFIER)
                break
        for p in publishers_in(template_words(f.text)):
            if not any((_phrase_re(a).search(rest) if a.isascii() else a in rest) for a in PUBLISHERS[p]):
                fails.append(MISSING_ATTRIBUTION)
                break
    need = sum(negations(f.text) for f in cited)
    if cjk:
        if need and not (_CJK_NEG_RE.search(rest) or negations(rest)):
            fails.append(NEGATION_CHANGED)
    elif negations(rest) != need:
        fails.append(NEGATION_CHANGED)
    if len({f.user for f in cited}) > 1:
        fails.append(MIXED_USER_FACTS)
    # a publisher the facts do not name may not be named ("The council records ..." for a fact about a KB item)
    own_all = " ".join(template_words(f.text) for f in cited)
    for p, alts in PUBLISHERS.items():
        if (any((_phrase_re(a).search(rest) if a.isascii() else a in rest) for a in alts)
                and p not in publishers_in(own_all)):
            fails.append(ADDED_ATTRIBUTION)
            break
    if not cjk:
        if len(_SAY_RE.findall(rest)) > len(_SAY_RE.findall(own_all)):
            fails.append(ADDED_ATTRIBUTION)
        if len(_CAUSE_RE.findall(rest)) > len(_CAUSE_RE.findall(own_all)):
            fails.append(ADDED_CAUSE)

    filled = fill(text, {f.id: f for f in cited})
    sids = list(dict.fromkeys(i for f in cited for i in f.source_ids))
    sentence = Sentence(text=filled, source_ids=sids)
    fails += final(sentence) if final else check_sentence(sentence, with_user_year(ev, cited, filled), max_words)
    fails = list(dict.fromkeys(fails))
    return Rewrite(None if fails else sentence, ids, fails, filled)
