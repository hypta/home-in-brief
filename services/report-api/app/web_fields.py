"""Values extracted from a Google Search answer about a property field the records do not have.

Team decision 10/11 (Ting; plan C): for year_built, cladding and last_sale, when PropertyContext has no value, the
follow-up may run a Google Search (followup.gemini_search) and show the answer as returned (WebAnswer: text,
sources, search suggestions). Code then *extracts* the year built and the cladding from that text, and the follow-up
returns them in user_supplied (marked "web_search" in user_supplied_source) so the form fills them in and the rules
re-run. The search answer itself is never changed or mixed with anything: the extracted values go in other fields.
last_sale is never a user_supplied value (rules-api has no such input); it is only shown (WebAnswer.extracted).

Extraction is by code first (extract), clause by clause: a year 1800..this year or a decade ("1990s", "1990 年代"),
as a whole token, whose nearest keyword in its clause is a build word; a cladding material bound to the walls (not
the roof, floor or fence). Skipped: a sentence about another property (a neighbour, the street, another address), a
clause with a negation or an uncertainty word, and for the year one with an approximation (probably, circa, about
1987, 大約). Several different values for one field, or another plausible year anywhere in the text: nothing is
filled (ambiguous). A field with no value found by code may be asked once more of Gemini (structured extraction of
the text only, check_model_values); a model value is kept only when its quote is in the text word for word and the
quote's clause passes the same checks.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel

from b_common import cladding as cladding_table

FIELDS = ("year_built", "cladding", "last_sale")
FILLABLE = ("year_built", "cladding")  # fields rules-api reads from chat_supplied

# A full stop ends a sentence only before a space: "$1.2 million" and "homes.co.nz" stay whole ("c. 1987" too).
_SENTENCE_END = re.compile(r"(?<=[!?。！？；;])\s*|(?<=\.)(?<!\bc\.)(?<!\bca\.)(?<!\bSt\.)(?<!\bNo\.)(?<!\bMt\.)\s+|\n+")
# A clause: the part of a sentence a negation or a qualifier belongs to ("built in 1987, but not renovated").
_CLAUSE_END = re.compile(r",\s+|，|、|;|；|:\s|\s+(?:but|whereas|although|though|while|however|yet)\s+|但是?|不過|不过|然而|而且",
                         re.I)
_NEGATION = re.compile(r"(?<![A-Za-z])(?:not|no|never|none|unable|cannot|can't|couldn't|isn't|wasn't|unknown|unclear|"
                       r"unconfirmed|uncertain|may|might|possibly|perhaps|estimated|either)(?![A-Za-z])|"
                       r"未能|無法|无法|沒有|没有|找不到|查不到|不詳|不详|未知|不確定|不确定|可能|或許|或许|也許|也许|並非|并非|不是",
                       re.I)
# Hedged or approximate figures: never filled in. about/around count only before a figure ("about 1987").
_APPROX = re.compile(r"(?<![A-Za-z])(?:probably|likely|circa|c\.|ca\.|approx\w*|roughly|believed|thought to|reportedly|"
                     r"supposedly|apparently|seems?|appears? to|estimate\w*|guess\w*|presumably)(?![A-Za-z])|"
                     r"(?<![A-Za-z])(?:about|around|some|nearly|almost|over|under|before|after|pre|post|early|mid|late)"
                     r"[\s-]+(?:the\s+)?(?:year\s+)?(?:1[89]\d\d|20\d\d|\d)|"
                     r"大[約约]|[約约](?=\s*(?:在|於|于)?\s*\d)|左右|估計|估计|推測|推测|大概|似乎|據說|据说|前後|前后|之前|以前|之後|以后|以後|"
                     r"初期|中期|末期|早期|晚期", re.I)
# The sentence is about another property (a neighbour, the street, the suburb): nothing in it is filled in.
_OTHER_PROPERTY = re.compile(r"(?<![A-Za-z])(?:neighbou?r\w*|next[- ]door|nearby|adjacent|adjoining|surrounding|"
                             r"other (?:houses?|homes?|propert\w+|dwellings?|units?|buildings?|addresses?)|"
                             r"another (?:house|home|property|address|dwelling|unit|building)|"
                             r"(?:homes|houses|properties|dwellings) (?:in|on|nearby|around)|the street|this street|"
                             r"the suburb|the area|in the area|neighbourhood|neighborhood)(?![A-Za-z])|"
                             r"隔壁|鄰居|邻居|鄰近|邻近|附近|周邊|周边|其他房|其他物業|其他物业|另一[棟栋間间戶户處处個个]|同一條街|同一条街|"
                             r"這條街|这条街|該區|该区|社區|社区", re.I)
_ADDRESS = re.compile(r"(?<![\w/])(\d+[A-Za-z]?(?:/\d+[A-Za-z]?)?)\s+([A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,3})\s+"
                      r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Place|Pl|Lane|Ln|Crescent|Cres|Terrace|Tce|Way|Close|"
                      r"Court|Ct|Grove|Parade|Pde|Highway|Hwy|Boulevard|Blvd|Rise|View|Square|Sq)\b")
# Build words vs. other dated events. One alternation, other events first so 改建/重建/rebuilt win over 建/built.
_EN = r"(?<![A-Za-z_]){}(?![A-Za-z_])"  # "year_built" in a page is no build word
_OTHER = (_EN.format(r"(?:rebuilt|renovat\w*|refurbish\w*|extended|extension|remodel\w*|alter\w*|sold|sale|sell\w*|"
                     r"purchased|bought|listed|listing|valued|valuation|consent\w*)") +
          r"|改建|重建|翻修|翻新|裝修|装修|擴建|扩建|增建|建議|建议|建商|售出|出售|賣出|卖出|成交|買入|买入|購入|购入|售|賣|卖|估價|估价")
_BUILD = (_EN.format(r"(?:built|constructed|erected|construction|build|dates? from|dating from)") +
          r"|建於|建于|建成|建造|興建|兴建|落成|蓋|盖|築|建|築年")
_KEYWORD = re.compile(rf"(?P<o>{_OTHER})|(?P<b>{_BUILD})", re.I)
_YEAR = re.compile(r"(?<![\d$,.])(1[89]\d\d|20\d\d)(?:\s*(年代|s\b|'s\b|’s\b))?(?!\d|,\d)")
_BEFORE, _AFTER = 60, 15
_CJK_AFTER = re.compile(r"[\s年]*[\u3040-\u30ff\u3400-\u9fff]+")  # characters searched for the nearest keyword around a year

# Parts of a building a material can belong to. A material is bound to the part named right after it ("brick
# exterior", "iron roof") or else to the nearest part named before it in its clause ("the fence is wood").
_WALL_PART = r"cladding|clad|exterior(?: walls?)?|façade|facade|siding|outside walls?|walls?|外牆|外墙|外觀|外观|外壁|牆|墙"
_OTHER_PART = (r"roof\w*|fenc\w*|floor\w*|deck\w*|foundations?|joinery|windows?|kitchens?|bench\w*|driveways?|paths?|"
               r"garage doors?|retaining walls?|interior walls?|inside walls?|屋頂|屋顶|地板|圍籬|围篱|圍欄|围栏|籬笆|篱笆|窗|廚房|厨房|地基")
_PART = re.compile(rf"(?<![A-Za-z])(?:(?P<x>{_OTHER_PART})|(?P<w>{_WALL_PART}))(?![A-Za-z])", re.I)
_WALL_CONTEXT = re.compile(r"clad|exterior|façade|facade|walls?\b|weatherboard|clapboard|外牆|外墙|外觀|外观|外壁|牆|墙", re.I)
_PART_AFTER = 20  # characters after a material searched for the part it names ("brick veneer walls")

_SALE_CONTEXT = re.compile(_EN.format(r"(?:sold|sale|purchased|bought)") + r"|售|成交|賣|卖|購入|购入|買入|买入", re.I)
_LATEST = re.compile(_EN.format(r"(?:last|latest|most recent(?:ly)?|recently)") + r"|最近|上次|最後|最后|上一次", re.I)
_PRICE = re.compile(r"(?:NZ)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|m|k)\b)?", re.I)
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|" \
          r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE = re.compile(rf"(?:\d{{1,2}}\s+)?{_MONTHS}\.?\s+(?:\d{{1,2}},?\s+)?\d{{4}}|\d{{4}}\s*年\s*\d{{1,2}}\s*月(?:\s*\d{{1,2}}\s*日)?",
                   re.I)


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text or "") if s and s.strip()]


def clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE_END.split(sentence) if c and c.strip()]


def unsure(s: str) -> bool:
    """A negation or an uncertainty word ("may" as a month name, "May 2019", does not count)."""
    return bool(_NEGATION.search(_DATE.sub(" ", s)))


def approximate(s: str) -> bool:
    """A hedged or approximate figure: probably, circa, about 1987, 大約, 約 1987 年, 1980 年代初期."""
    return bool(_APPROX.search(_DATE.sub(" ", s)))


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9/]+", " ", t.lower()).strip()


def other_property(sentence: str, target: str = "") -> bool:
    """The sentence speaks of another property: a neighbour, the street or suburb, or a street address that is not
    the target's."""
    if _OTHER_PROPERTY.search(sentence):
        return True
    tgt = f" {_norm(target)} "
    for m in _ADDRESS.finditer(sentence):
        if not target or f" {_norm(m.group(1) + ' ' + m.group(2))} " not in tgt:
            return True
    return False


def _year_value(m: re.Match) -> Optional[Any]:
    y, suffix = int(m.group(1)), m.group(2)
    if not 1800 <= y <= datetime.now().year:
        return None
    if suffix:
        return f"{y}s" if y % 10 == 0 else None
    return y


def year_in_text(value: Any, text: str) -> bool:
    """The year (1997) or decade ("1990s", also written "1990 年代") appears word for word in the text, as a whole
    token: 1990 is not in "1990s"."""
    m = re.fullmatch(r"(\d{3}0)s", str(value))
    if m:
        return bool(re.search(rf"(?<![\d$,.]){m.group(1)}(?:s\b|['’]s\b|\s*年代)", text))
    return bool(re.fullmatch(r"\d{4}", str(value)) and
                re.search(rf"(?<![\d$,.]){value}(?!\d|,\d|s\b|['’]s\b|\s*年代)", text))


def _nearest(clause: str, m: re.Match) -> Optional[bool]:
    """The keyword nearest the year in its clause: True a build word, False another dated event, None no keyword.
    A keyword after the year counts only in Chinese/Japanese word order ("1998 年建成"), with no punctuation
    between; in English the keyword comes first ("built in 1965")."""
    near = []
    for k in _KEYWORD.finditer(clause):
        s_, e = k.start(), k.end()
        if e <= m.start() and m.start() - e <= _BEFORE:
            near.append((m.start() - e, bool(k.group("b"))))
        elif s_ >= m.end() and s_ - m.end() <= _AFTER and _CJK_AFTER.fullmatch(clause[m.end():e]):
            near.append((s_ - m.end(), bool(k.group("b"))))
    return min(near)[1] if near else None


def _year_clauses(text: str, target: str):
    """(clause, year match, value, nearest keyword) for each year or decade in range, in clauses that are
    affirmative, not approximate, and in sentences about this property only."""
    for s in sentences(text):
        if other_property(s, target):
            continue
        for c in clauses(s):
            if unsure(c) or approximate(c):
                continue
            for m in _YEAR.finditer(c):
                v = _year_value(m)
                if v is not None:
                    yield c, m, v, _nearest(c, m)


def _years(text: str, target: str = "") -> list[Any]:
    """Years and decades whose nearest keyword (60 characters before, 15 after, in the same clause) is a build word,
    in affirmative clauses about this property without a negation, an uncertainty word or an approximation."""
    return [v for _, _, v, build in _year_clauses(text, target) if build]


def plausible_years(text: str, target: str = "") -> set[Any]:
    """Every year or decade that could be the year built: build-word years and years with no dated-event keyword
    ("1965 or 1972"). More than one: the text is ambiguous."""
    return {v for _, _, v, build in _year_clauses(text, target) if build is not False}


_GAP_STOP = {"and", "or", "the", "a", "an", "is", "are", "was", "were", "with", "of", "on", "under", "has", "have", "its",
             "while", "but", "to", "for", "in", "as"}


def _adjacent(gap: str) -> bool:
    """A part named right after a material: nothing between, or one modifier ("brick veneer walls",
    "corrugated iron roof", "鐵皮屋頂"); not "brick and the fence"."""
    if re.search(r"[,.;:，。；、]", gap):
        return False
    words = re.findall(r"[A-Za-z]+", gap)
    return len(words) <= 1 and not any(w.lower() in _GAP_STOP for w in words) and len(gap.strip()) <= 12


def _clause_claddings(clause: str) -> list[str]:
    """Cladding values in one clause whose material is bound to the walls: the part named right after the
    material, else the nearest part named before it in the clause, else the clause's wall wording."""
    parts = [(p.start(), p.end(), bool(p.group("w"))) for p in _PART.finditer(clause)]
    out = []
    for value, rx in cladding_table._COMPILED:
        for m in rx.finditer(clause):
            after = [w for s_, _, w in parts if m.end() <= s_ <= m.end() + _PART_AFTER and _adjacent(clause[m.end():s_])]
            before = [w for _, e, w in parts if e <= m.start()]
            if after:
                wall = after[0]
            elif before:
                wall = before[-1]
            else:
                wall = bool(_WALL_CONTEXT.search(clause))
            if wall:
                out.append(value)
                break
    return out


def _claddings(text: str, target: str = "") -> list[str]:
    """Cladding values bound to the walls (not a roof, floor or fence), in affirmative clauses about this
    property."""
    out = []
    for s in sentences(text):
        if other_property(s, target):
            continue
        for c in clauses(s):
            if not unsure(c):
                out += _clause_claddings(c)
    return out


def _last_sale(text: str, target: str = "") -> dict[str, str]:
    """The date and price of one sale event, as written. A sale event is one sentence about this property with a
    sale word; its date and price are kept only when that sentence gives one of each kind (a price is never cut at
    its decimal point). Several events: the one marked last/latest, when exactly one is; else nothing."""
    events = []
    for s in sentences(text):
        if unsure(s) or other_property(s, target) or not _SALE_CONTEXT.search(s):
            continue
        prices = list(dict.fromkeys(m.group(0).strip() for m in _PRICE.finditer(s)))
        dates = list(dict.fromkeys(m.group(0).strip() for m in _DATE.finditer(s)))
        if prices or dates:
            events.append((s, prices, dates))
    if len(events) > 1:
        events = [e for e in events if _LATEST.search(e[0])]
    if len(events) != 1:
        return {}
    _, prices, dates = events[0]
    out = {}
    if len(prices) == 1:
        out["price"] = prices[0]
    if len(dates) == 1:
        out["date"] = dates[0]
    return out


def extract(text: str, fields: list[str], target: str = "") -> tuple[dict[str, Any], set[str], set[str]]:
    """(values, ambiguous fields, fields code found nothing for) for the asked fields. `target`: the property's
    address (a sentence naming another address is skipped). Only year_built and cladding can be ambiguous or asked
    again of the model. A year is ambiguous also when the text gives another plausible year ("1965 or 1972")."""
    found: dict[str, Any] = {}
    ambiguous, missing = set(), set()
    for field, cands in (("year_built", _years), ("cladding", _claddings)):
        if field not in fields:
            continue
        got = list(dict.fromkeys(cands(text, target)))
        if field == "year_built" and len(got) == 1 and plausible_years(text, target) - set(got):
            got = got + sorted(plausible_years(text, target) - set(got), key=str)
        if len(got) == 1:
            found[field] = got[0]
        elif got:
            ambiguous.add(field)
        else:
            missing.add(field)
    if "last_sale" in fields:
        sale = _last_sale(text, target)
        if sale:
            found["last_sale"] = sale
    return found, ambiguous, missing


# ---- model fallback: structured extraction of the search answer's text --------------------------
class ExtractOut(BaseModel):
    year_built: Optional[str] = None  # "1997" or "1990s", copied from the text
    year_built_quote: Optional[str] = None  # the words of the text that give it
    cladding: Optional[Literal[cladding_table.VALUES]] = None  # type: ignore[valid-type]
    cladding_quote: Optional[str] = None
    ambiguous: bool = False


def _quote_clauses(quote: Optional[str], text: str, target: str) -> list[str]:
    """The clauses of the text that contain the quote, in sentences about this property. The quote must be
    written in the text word for word; a quote across clauses gives nothing."""
    q = (quote or "").strip()
    if not q or q not in text:
        return []
    out = []
    for s in sentences(text):
        if q in s and not other_property(s, target):
            out += [c for c in clauses(s) if q in c]
    return out


def _year_evidence_ok(value: Any, clause: str) -> bool:
    """The model's year passes the code's clause checks: affirmative, not approximate, a build word in the clause and
    no other dated event nearest the year."""
    if unsure(clause) or approximate(clause) or not re.search(_BUILD, clause, re.I):
        return False
    hits = [m for m in _YEAR.finditer(clause) if _year_value(m) == value]
    return bool(hits) and all(_nearest(clause, m) is not False for m in hits)


def check_model_values(out: ExtractOut, text: str, fields: set[str], target: str = "") -> dict[str, Any]:
    """A model value is kept only when its quote is written in the text and the quote's clause passes the same checks
    as code extraction: affirmative (no negation, uncertainty or approximation), about this property (no
    neighbour or other address), a construction event for a year (a build word, not a renovation or sale year) or the
    walls for a cladding (not the roof or fence). The year must be a whole token (1990 is not "1990s"), and the
    whole text must give no other plausible year or wall material. The model's own ambiguity flag also drops all."""
    if out.ambiguous:
        return {}
    got: dict[str, Any] = {}
    y = (out.year_built or "").strip()
    m = _YEAR.fullmatch(y) if y else None
    v = _year_value(m) if m else None
    if "year_built" in fields and v is not None and year_in_text(v, text) \
            and year_in_text(v, out.year_built_quote or "") \
            and any(_year_evidence_ok(v, c) for c in _quote_clauses(out.year_built_quote, text, target)) \
            and plausible_years(text, target) <= {v}:
        got["year_built"] = v
    if "cladding" in fields and out.cladding:
        cl = _quote_clauses(out.cladding_quote, text, target)
        if any(not unsure(c) and out.cladding in _clause_claddings(c) for c in cl) \
                and set(_claddings(text, target)) <= {out.cladding}:
            got["cladding"] = out.cladding
    return got
