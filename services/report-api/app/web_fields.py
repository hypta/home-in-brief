"""Values extracted from a Google Search answer about a property field the records do not have.

Team decision 10/11 (Ting; plan C): for year_built, cladding and last_sale, when PropertyContext has no value, the
follow-up may run a Google Search (followup.gemini_search) and show the answer as returned (WebAnswer: text,
sources, search suggestions). Code then *extracts* the year built and the cladding from that text, and the follow-up
returns them in user_supplied (marked "web_search" in user_supplied_source) so the form fills them in and the rules
re-run. The search answer itself is never changed or mixed with anything: the extracted values go in other fields.
last_sale is never a user_supplied value (rules-api has no such input); it is only shown (WebAnswer.extracted).

Extraction is by code first (extract): a year 1800..this year or a decade ("1990s", "1990 年代") whose nearest
keyword is a build word, and the shared cladding keywords in a sentence about the walls. A sentence with a negation
or an uncertainty word is skipped. Several different values for one field: nothing is filled (ambiguous). A field
with no value found by code may be asked once more of Gemini (structured extraction of the text only,
check_model_values); a model value is kept only when it can be found word for word in the text.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel

from b_common import cladding as cladding_table

FIELDS = ("year_built", "cladding", "last_sale")
FILLABLE = ("year_built", "cladding")  # fields rules-api reads from chat_supplied

_SENTENCE_END = re.compile(r"(?<=[.!?。！？；;])\s*|\n+")
_NEGATION = re.compile(r"(?<![A-Za-z])(?:not|no|never|none|unable|cannot|can't|couldn't|unknown|unclear|unconfirmed|"
                       r"uncertain|may|might|possibly|perhaps|estimated|either)(?![A-Za-z])|"
                       r"未能|無法|无法|沒有|没有|找不到|查不到|不詳|不详|未知|不確定|不确定|可能|或許|或许|也許|也许", re.I)
# Build words vs. other dated events. One alternation, other events first so 改建/重建/rebuilt win over 建/built.
_EN = r"(?<![A-Za-z]){}(?![A-Za-z])"
_OTHER = (_EN.format(r"(?:rebuilt|renovat\w*|refurbish\w*|extended|extension|remodel\w*|alter\w*|sold|sale|sell\w*|"
                     r"purchased|bought|listed|listing|valued|valuation|consent\w*)") +
          r"|改建|重建|翻修|翻新|裝修|装修|擴建|扩建|增建|建議|建议|建商|售出|出售|賣出|卖出|成交|買入|买入|購入|购入|售|賣|卖|估價|估价")
_BUILD = (_EN.format(r"(?:built|constructed|erected|construction|build|dates? from|dating from)") +
          r"|建於|建于|建成|建造|興建|兴建|落成|蓋|盖|築|建|築年")
_KEYWORD = re.compile(rf"(?P<o>{_OTHER})|(?P<b>{_BUILD})", re.I)
_YEAR = re.compile(r"(?<![\d$,.])(1[89]\d\d|20\d\d)(?:\s*(年代|s\b|'s\b|’s\b))?(?!\d|,\d)")
_BEFORE, _AFTER = 60, 15
_CJK_AFTER = re.compile(r"[\s年]*[\u3040-\u30ff\u3400-\u9fff]+")  # characters searched for the nearest keyword around a year

_WALL_CONTEXT = re.compile(r"clad|exterior|façade|facade|walls?\b|weatherboard|外牆|外墙|外觀|外观|外壁|牆|墙", re.I)
_NOT_WALL = re.compile(r"roof|floor|屋頂|屋顶|地板|foundation|fenc|deck|joinery|window|kitchen|bench|tiled? roof", re.I)

_SALE_CONTEXT = re.compile(_EN.format(r"(?:sold|sale|purchased|bought)") + r"|售|成交|賣|卖|購入|购入|買入|买入", re.I)
_PRICE = re.compile(r"(?:NZ)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|m|k)\b)?", re.I)
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|" \
          r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE = re.compile(rf"(?:\d{{1,2}}\s+)?{_MONTHS}\.?\s+(?:\d{{1,2}},?\s+)?\d{{4}}|\d{{4}}\s*年\s*\d{{1,2}}\s*月(?:\s*\d{{1,2}}\s*日)?",
                   re.I)


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text or "") if s and s.strip()]


def unsure(s: str) -> bool:
    """A negation or an uncertainty word ("may" as a month name, "May 2019", does not count)."""
    return bool(_NEGATION.search(_DATE.sub(" ", s)))


def _year_value(m: re.Match) -> Optional[Any]:
    y, suffix = int(m.group(1)), m.group(2)
    if not 1800 <= y <= datetime.now().year:
        return None
    if suffix:
        return f"{y}s" if y % 10 == 0 else None
    return y


def year_in_text(value: Any, text: str) -> bool:
    """The year (1997) or decade ("1990s", also written "1990 年代") appears word for word in the text."""
    m = re.fullmatch(r"(\d{3}0)s", str(value))
    if m:
        return bool(re.search(rf"(?<!\d){m.group(1)}(?:s\b|\s*年代)", text))
    return bool(re.fullmatch(r"\d{4}", str(value)) and re.search(rf"(?<![\d$,.]){value}(?!\d|,\d)", text))


def _years(text: str) -> list[Any]:
    """Years and decades whose nearest keyword (60 characters before, 15 after) is a build word, in sentences
    without a negation or an uncertainty word."""
    out = []
    for s in sentences(text):
        if unsure(s):
            continue
        # a keyword after the year counts only in Chinese/Japanese word order ("1998 年建成"), with no punctuation
        # between; in English the keyword comes first ("built in 1965, renovated in 2004")
        keys = [(k.start(), k.end(), bool(k.group("b"))) for k in _KEYWORD.finditer(s)]
        for m in _YEAR.finditer(s):
            v = _year_value(m)
            if v is None:
                continue
            near = [(m.start() - e if e <= m.start() else s_ - m.end(), build) for s_, e, build in keys
                    if (e <= m.start() and m.start() - e <= _BEFORE) or (s_ >= m.end() and s_ - m.end() <= _AFTER and _CJK_AFTER.fullmatch(s[m.end():e]))]
            if near and min(near)[1]:
                out.append(v)
    return out


def _claddings(text: str) -> list[str]:
    """Cladding values named in sentences about the walls (not a roof, floor or fence), without a negation."""
    out = []
    for s in sentences(text):
        if unsure(s) or not _WALL_CONTEXT.search(s):
            continue
        for value, rx in cladding_table._COMPILED:
            for m in rx.finditer(s):
                if not _NOT_WALL.search(s[m.end():m.end() + 15]):
                    out.append(value)
                    break
    return out


def _last_sale(text: str) -> dict[str, str]:
    """The date and price of a sale, as written, when exactly one of each is given in sale sentences."""
    prices, dates = [], []
    for s in sentences(text):
        if unsure(s) or not _SALE_CONTEXT.search(s):
            continue
        prices += [m.group(0).strip() for m in _PRICE.finditer(s)]
        dates += [m.group(0).strip() for m in _DATE.finditer(s)]
    out = {}
    if len(set(prices)) == 1:
        out["price"] = prices[0]
    if len(set(dates)) == 1:
        out["date"] = dates[0]
    return out


def extract(text: str, fields: list[str]) -> tuple[dict[str, Any], set[str], set[str]]:
    """(values, ambiguous fields, fields code found nothing for) for the asked fields. Only year_built and cladding
    can be ambiguous or asked again of the model."""
    found: dict[str, Any] = {}
    ambiguous, missing = set(), set()
    for field, cands in (("year_built", _years), ("cladding", _claddings)):
        if field not in fields:
            continue
        got = list(dict.fromkeys(cands(text)))
        if len(got) == 1:
            found[field] = got[0]
        elif got:
            ambiguous.add(field)
        else:
            missing.add(field)
    if "last_sale" in fields:
        sale = _last_sale(text)
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


def _quote_ok(quote: Optional[str], text: str) -> bool:
    q = (quote or "").strip()
    return bool(q) and q in text and not unsure(q)


def check_model_values(out: ExtractOut, text: str, fields: set[str]) -> dict[str, Any]:
    """A model value is kept only when it is in range and found word for word in the text, with a quote from the
    text that contains it (and has no negation). Nothing is kept when the model says the text is ambiguous."""
    if out.ambiguous:
        return {}
    got: dict[str, Any] = {}
    y = (out.year_built or "").strip()
    if "year_built" in fields and y and _quote_ok(out.year_built_quote, text):
        m = _YEAR.fullmatch(y)
        v = _year_value(m) if m else None
        if v is not None and year_in_text(v, text) and year_in_text(v, out.year_built_quote or ""):
            got["year_built"] = v
    if "cladding" in fields and out.cladding and _quote_ok(out.cladding_quote, text) \
            and out.cladding in cladding_table.find_all(out.cladding_quote or ""):
        got["cladding"] = out.cladding
    return got
