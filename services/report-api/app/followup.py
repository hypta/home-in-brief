"""POST /v1/followup: answer one question in the asker's language, validated like the summary (INTERFACE_DRAFT 5, 6.1).

Language (2026-10-11 decision; the report itself stays English): classify detects the question's language
(BCP 47, corrected by script in normalize_language); the answer is written in it, with English terms (LVR, DTI,
First Home Loan, Cross Lease, Unit Title, Freehold, Settled.govt.nz, ...) kept. `language` in the response is the
detected language.

Flow: classify (Gemini: language, out-of-scope, stated year/cladding) -> code checks user_supplied ->
optional rules-api re-run with the merged chat_supplied -> fact list (prompt.followup_facts, rebuilt from the
re-run RulesOutput) -> answer (Gemini rewrites facts in the question's language, values as placeholders) ->
validator.check_rewrite plus the language checks (check_answer) -> blocked sentences fall back to their facts'
drafts -> English program sentences (drafts, program facts, comparison, refusal) get Chinese wording written by
code or a restricted translation (translate_parts), English when the translation fails its checks -> code-added
prefix in the language of the sentence. Out of scope: an answer from a Google Search when classify says web pages
can answer it (`web`, in the language of the question, shown whole with its sources and never run through the
validator), else a fixed refusal chosen by code.

question and history are untrusted: they never enter the evidence or the translation prompt. The one exception
(P-V3) is a validated year_built that was used for the re-run; it is allowed only in sentences citing
kb_items_from_user_input.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Literal, Optional

import requests
from pydantic import BaseModel, Field, ValidationError

from b_common import cladding as cladding_table
from b_common.report_schemas import FieldView, ReportRequest, Sentence

from . import gemini, kb, rewrite
from . import followup_prompt as fp
from . import prompt as facts_prompt
from .facts import TITLE_WORDS, RewriteSentence
from .service import ReportError, _env_float, mock_enabled
from .validator import (BANNED_WORD, MAX_WORDS, NUMBER_WORD, TOO_LONG, URL_OR_HTML, Allowed, Evidence,
                        build_evidence, check_rewrite, check_sentence, strip_prefix, to_decimal)

log = logging.getLogger("report-api.followup")
MAX_ANSWER = 3

REFUSAL_EN = ("This question is outside what this property report covers; the original sources linked below "
              "may help you look it up.")
# Chinese refusal written by code (zh-Hans via _zh); other languages get a restricted translation of REFUSAL_EN.
REFUSAL_ZH = "這個問題超出本報告涵蓋的範圍；下方的原始連結或許能幫你查詢。"
PREFIX_EN = "Based on what you told us, "
PREFIXES = {"zh-Hant": "依你提供的資訊，", "zh-Hans": "根据你提供的信息，"}
LANGUAGE_MISMATCH = "language_mismatch"  # the sentence is not in the language it is meant to be in
MIXED_LANGUAGE = "mixed_language"  # a CJK sentence with English words that are not a kept term, name or value


# ---- request / response -------------------------------------------------------------------------
class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=2000)


class FollowupRequest(ReportRequest):
    question: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=6)


class WebSource(BaseModel):
    title: str  # the site's domain, as the search returns it
    url: str    # Google's redirect to the page


class WebAnswer(BaseModel):
    """An answer grounded in Google Search. Gemini API terms: shown as returned, with its search suggestions."""
    text: str
    sources: list[WebSource]
    search_suggestions: str  # Google's HTML widget


class FollowupResponse(BaseModel):
    language: str
    answer: list[Sentence]
    out_of_scope: bool
    user_supplied: dict[str, Any]
    links: list[str]
    dropped: int
    warnings: list[str] = Field(default_factory=list)  # e.g. rules_rerun_unavailable (mock mode only)
    web: Optional[WebAnswer] = None  # set, with an empty `answer`, when an out-of-scope question was answered from a search


# ---- Gemini (own wrapper: gemini.gemini_generate is fixed to the summary schema) ---------------
class ClassifyOut(BaseModel):
    language: str = "en"
    out_of_scope: bool = False
    searchable: bool = False
    year_built: Optional[str] = None
    cladding: Optional[Literal[cladding_table.VALUES]] = None  # type: ignore[valid-type]  # the 13 shared values
    hypothetical: bool = False
    links: list[str] = Field(default_factory=list)


class AnswerOut(BaseModel):
    answer: list[RewriteSentence]
    links: list[str] = Field(default_factory=list)


class TranslateOut(BaseModel):
    texts: list[str]


# (prompt, response schema, timeout_s) -> raw JSON text. Replaced in tests.
JsonGenerator = Callable[[str, type, float], str]


def gemini_json(prompt: str, schema: type, timeout_s: float) -> str:
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise gemini.ModelUnavailable("GEMINI_API_KEY is not set")
    try:
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        resp = client.models.generate_content(
            model=gemini.model_name(), contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema, temperature=gemini.TEMPERATURE,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    except Exception as e:  # timeouts, APIError (429, 5xx, bad key), network
        msg = str(e).replace(key, "***")[:300]
        log.warning("gemini call failed: %s: %s", type(e).__name__, msg)
        raise gemini.ModelUnavailable(f"{type(e).__name__}: {msg}") from e
    if not resp.text:
        raise gemini.ModelOutputInvalid("empty response")
    return resp.text


def gemini_search(prompt: str, timeout_s: float) -> Optional[WebAnswer]:
    """One Gemini call with the Google Search tool. None when the answer names no source. Replaced in tests."""
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise gemini.ModelUnavailable("GEMINI_API_KEY is not set")
    try:
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        resp = client.models.generate_content(
            model=gemini.model_name(), contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())], temperature=gemini.TEMPERATURE,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        meta = resp.candidates[0].grounding_metadata if resp.candidates else None
        text = (resp.text or "").strip()
    except Exception as e:  # timeouts, APIError (429, 5xx, bad key), network
        msg = str(e).replace(key, "***")[:300]
        log.warning("gemini search call failed: %s: %s", type(e).__name__, msg)
        raise gemini.ModelUnavailable(f"{type(e).__name__}: {msg}") from e
    pages = {c.web.uri: c.web.title or "" for c in (meta and meta.grounding_chunks) or [] if c.web and c.web.uri}
    if not (text and pages):
        return None
    entry = meta.search_entry_point
    return WebAnswer(text=text, sources=[WebSource(title=t, url=u) for u, t in pages.items()],
                     search_suggestions=(entry and entry.rendered_content) or "")


def _parse(raw: str, schema: type):
    try:
        return schema.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as e:
        raise gemini.ModelOutputInvalid(str(e)[:300]) from e


class _Clock:
    def __init__(self):
        self.deadline = time.monotonic() + _env_float("REQUEST_DEADLINE_S", 25.0)
        self.call_limit = _env_float("GEMINI_TIMEOUT_S", 20.0)

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def budget(self, share: float = 1.0) -> float:
        left = self.left()
        if left <= 0.5:
            raise ReportError(504, "deadline_exceeded", "The request ran out of time.")
        return max(0.5, min(self.call_limit, left * share))


def _call(generate: JsonGenerator, prompt: str, schema: type, timeout: float):
    try:
        return _parse(generate(prompt, schema, timeout), schema)
    except gemini.ModelUnavailable:
        raise ReportError(503, "model_unavailable", "The language model did not answer.")
    except gemini.ModelOutputInvalid:
        raise ReportError(502, "model_output_invalid", "The language model's answer did not match the schema.")


# ---- user_supplied (5.3) ------------------------------------------------------------------------
_CONDITIONAL_RE = re.compile(r"\b(?:if|suppose|assuming|whether|not|isn't|wasn't|isnt|wasnt)\b|如果|假如|假設|若是|"
                             r"要是|萬一|不是|並非", re.I)


def extract_user_supplied(question: str, c: ClassifyOut) -> dict[str, Any]:
    """Validated year_built/cladding: in range, written in the question, not conditional or negated."""
    out: dict[str, Any] = {}
    if c.hypothetical or _CONDITIONAL_RE.search(question):
        if c.year_built or c.cladding:
            log.info("user_supplied_ignored: conditional or negated")
        return out
    y = (c.year_built or "").strip()
    if y:
        this_year = datetime.now().year
        m_int, m_dec = re.fullmatch(r"\d{4}", y), re.fullmatch(r"(\d{3}0)s", y)
        verbatim = re.search(rf"(?<![\d]){re.escape(y)}(?![\d])", question)
        if m_int and 1800 <= int(y) <= this_year and verbatim:
            out["year_built"] = int(y)
        elif m_dec and 1800 <= int(m_dec.group(1)) <= this_year and verbatim:
            out["year_built"] = y
        else:
            log.info("user_supplied_ignored: year_built not valid or not in the question")
    if c.cladding:
        # the value must have a keyword written in the question itself
        if c.cladding in cladding_table.find_all(question):
            out["cladding"] = c.cladding
        else:
            log.info("user_supplied_ignored: cladding not stated in the question")
    return out


def _year_numbers(v: Any) -> list[int]:
    if isinstance(v, int):
        return [v]
    m = re.fullmatch(r"(\d{4})s", str(v))
    return [int(m.group(1))] if m else []


# ---- rules-api re-run ---------------------------------------------------------------------------
def call_rules(property_context: dict, user_inputs: dict, timeout: float) -> dict:
    """POST {RULES_API_URL}/v1/rules (ID token for https, as kb.py and web do). Replaced in tests."""
    base = os.environ.get("RULES_API_URL", "http://localhost:8080").rstrip("/")
    try:
        r = requests.post(base + "/v1/rules", json={"property_context": property_context, "user_inputs": user_inputs},
                          headers=kb._auth(base, timeout), timeout=timeout)
        if r.status_code in (401, 403):
            kb._tokens.pop(base, None)
        r.raise_for_status()
        out = r.json()
    except Exception as e:  # network, HTTP or JSON errors
        log.warning("rules re-run failed: %s", type(e).__name__)
        raise ReportError(503, "rules_unavailable", "The rules re-run failed.") from e
    if not isinstance(out, dict):
        raise ReportError(503, "rules_unavailable", "The rules re-run returned an unexpected body.")
    return out


# ---- language -----------------------------------------------------------------------------------
_HAN_RE = re.compile(r"[\u3400-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef]")
_BCP47_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
# Characters written differently in simplified Chinese (a few common ones), to tell zh-Hans from zh-Hant when the
# model returns a bare "zh".
_HANS_ONLY = set("这么们个来时说为会对发经过还进门问题间盖议资记录买卖贷页区县环险产权楼层东")
_HANT_ONLY = set("這麼們個來時說為會對發經過還進門問題間蓋議資記錄買賣貸頁區縣環險產權樓層東")


def normalize_language(claimed: str, question: str) -> str:
    """Gemini's code, corrected by script: Hangul -> ko, kana -> ja, Han -> zh-Hant/zh-Hans (or ja); a question
    without CJK script is never zh/ja/ko. Chinese variants become zh-Hant or zh-Hans."""
    lang = (claimed or "").strip().replace("_", "-")
    if not _BCP47_RE.fullmatch(lang):
        lang = "en"
    low = lang.lower()
    if _HANGUL_RE.search(question):
        return "ko"
    if _KANA_RE.search(question):
        return "ja"
    if _HAN_RE.search(question):
        if low.startswith("ja"):
            return "ja"
        if "hans" in low or low.endswith(("-cn", "-sg")):
            return "zh-Hans"
        if "hant" in low or low.endswith(("-tw", "-hk", "-mo")):
            return "zh-Hant"
        hans = sum(ch in _HANS_ONLY for ch in question)
        hant = sum(ch in _HANT_ONLY for ch in question)
        return "zh-Hans" if hans > hant else "zh-Hant"
    if low.startswith(("zh", "ja", "ko")):
        return "en"  # no CJK script in the question
    return "en" if low.startswith("en") else lang


def is_english(language: str) -> bool:
    return language.lower().startswith("en")


def is_cjk(language: str) -> bool:
    return language.lower().startswith(("zh", "ja", "ko"))


def is_zh(language: str) -> bool:
    return language.lower().startswith("zh")


_TO_HANS = str.maketrans("議會資記錄說為內準與牆這個問題蓋範圍連結許幫詢調錯與據時間準鍵戶稱開關東區層單樣嗎實際處筆價貸額屬權產圖齡庫訊標準舊歲牠",
                         "议会资记录说为内准与墙这个问题盖范围连结许帮询调错与据时间准键户称开关东区层单样吗实际处笔价贷额属权产图龄库讯标准旧岁它")


def _zh(text: str, language: str) -> str:
    """Code-written Chinese is zh-Hant; zh-Hans maps the few characters it uses."""
    return text.translate(_TO_HANS) if language == "zh-Hans" else text


# ---- validation (validator.validate plus language-dependent rules) ------------------------------
LATIN_MAX_WORDS = 35  # other languages written in Latin script: a little more room than English
_CJK_NUM_SPACE = [(re.compile(r"(\d)(?=[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af])"), r"\1 "),
                  (re.compile(r"(?<=[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af])(?=[$\d])"), " ")]
_ASCII_CJK_GAP = re.compile(r"(?<=[A-Za-z0-9])(?=[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef])|"
                            r"(?<=[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef])(?=[A-Za-z0-9])")
# Verdict, advice and approximation words (6.1). Chinese "may" is 或許, never 可能 (also "possible").
_BANNED_BY_LANG = {
    "zh": re.compile(r"安全|危險|危险|無風險|无风险|沒有風險|没有风险|低風險|低风险|高風險|高风险|風險低|风险低|風險高|风险高|"
                     r"建議|建议|推薦|推荐|應該|应该|應當|应当|必須|必须|務必|务必|一定要|將會|将会|一定會|一定会|可能|大約|"
                     r"大约|大概|左右|將近|将近|幾乎|几乎|保證|保证|划算|值得|便宜|別買|别买|不要買|不要买|該買|该买|可以買|"
                     r"可以买|擔心|担心|放心|沒有災害|没有灾害|沒有淹水|没有淹水|不會淹水|不会淹水"),
    "ja": re.compile(r"安全|危険|推奨|お勧め|おすすめ|オススメ|すべき|べきです|べきだ|必ず|保証|およそ|大体|だいたい|多分|"
                     r"たぶん|恐らく|おそらく|心配|安心|割安|お得|リスクが低|リスクが高|リスクはありません"),
    "ko": re.compile(r"안전|추천|권장|해야 합니다|해야 한다|해야 해|반드시|보장|아마|대략|걱정|안심|위험이 낮|위험이 높"),
}
_ZH_NUMBER = re.compile(r"[兩两零]|百分之|ゼロ")
# English verdict words glued to CJK text, where \b in the validator does not fire ("很safe").
_EN_GLUED = re.compile(r"(?<![A-Za-z])(?:safe|unsafe|risk-free|no risk|recommend\w*|should|must|guarantee\w*|"
                       r"will|likely|unlikely)(?![A-Za-z])", re.I)
_CJK_DOMAIN_RE = re.compile(r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9-])")

# (sentence, max_words) -> validator fails; check_answer wraps it with the language rules.
BaseCheck = Callable[[Sentence, Optional[int]], list[str]]


_DECADE_WORD = {"zh": "年代", "ja": "年代", "ko": "년대"}


def tidy(text: str, language: str = "") -> str:
    """Space between digits and CJK characters, so "1997年" reads as the number 1997 (no change in meaning). In a
    Chinese, Japanese or Korean sentence a filled-in decade "1990s" becomes "1990 年代" / "1990 년대"."""
    word = _DECADE_WORD.get(language.lower()[:2])
    if word:
        text = re.sub(rf"(?<!\d)(\d{{3}}0)s(?:\s*{word})?", rf"\1{word}", text)
    for rx, rep in _CJK_NUM_SPACE:
        text = rx.sub(rep, text)
    return text.strip()


# English kept in any answer (6.6): terms, publishers, title names, layer names, the cladding table, plus (per
# request) the values and labels code fills in and the property's own text values (latin_terms).
_LATIN_TERMS = {*fp.KEEP_ENGLISH, "Auckland Council", "Auckland", "Reserve Bank", "RBNZ", "Kainga Ora",
                "Natural Hazards Commission", "EQC", "CV", "body corporate", "high-LVR", "high-DTI", "loan-to-value",
                "debt-to-income", *facts_prompt.LAYER_NAMES.values(), *facts_prompt.SHORT_NAMES.values(),
                *TITLE_WORDS, *(c.label for c in cladding_table.CLADDINGS), *cladding_table.VALUES}
_LATIN_WORD = re.compile(r"[A-Za-zĀ-ſ][A-Za-zĀ-ſ'’-]+")


def latin_terms(req: ReportRequest, fs: list) -> set[str]:
    """Per-request English a CJK answer may contain: the facts' filled-in values and labels, the property's text
    values and the hazard layer names."""
    out: set[str] = set()
    for f in fs:
        for sl in f.slots.values():
            out.add(sl.render())
            if sl.label:
                out.add(sl.label)
    for v in req.property_fields().values():
        if isinstance(v.value, str):
            out.add(v.value)
    out.update(h.layer.replace("_", " ") for h in req.hazards())
    return {t for t in out if t and _LATIN_WORD.search(t)}


def mixed_latin(text: str, terms: set[str]) -> bool:
    """True when a CJK sentence has an English word outside the kept terms, names and values (and domains)."""
    rest = re.sub(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}", " ", text)
    for t in sorted(_LATIN_TERMS | terms, key=len, reverse=True):
        rest = re.sub(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", " ", rest, flags=re.I)
    return bool(_LATIN_WORD.search(rest))


def cjk_units(text: str) -> int:
    return len(_HAN_RE.findall(text)) + len(_KANA_RE.findall(text)) + len(_HANGUL_RE.findall(text)) + \
        len(re.findall(r"[A-Za-z0-9$][A-Za-z0-9$.,%'’-]*", text))


def script_matches(text: str, language: str) -> bool:
    """The sentence is written in the script of `language` (zh: Han and no kana/Hangul; ja: kana; ko: Hangul;
    any other language: no CJK script). A Latin-script language other than English is not told apart from English."""
    low = language.lower()
    if low.startswith("zh"):
        return bool(_HAN_RE.search(text)) and not (_KANA_RE.search(text) or _HANGUL_RE.search(text))
    if low.startswith("ja"):
        return bool(_KANA_RE.search(text))
    if low.startswith("ko"):
        return bool(_HANGUL_RE.search(text))
    return not _CJK_RE.search(text)


def check_answer(s: Sentence, ev: Evidence, language: str = "en", base: Optional[BaseCheck] = None) -> list[str]:
    """validator.check_sentence rules (or `base`), plus the rules of the sentence's language:

    - the sentence must be written in `language` (LANGUAGE_MISMATCH; English sentences with any CJK script fail);
    - English: the 25-word limit; other Latin-script languages: LATIN_MAX_WORDS;
    - Chinese, Japanese, Korean: fp.CJK_MAX_UNITS_BY_LANG (zh 60, ja 90, ko 80) instead of the word limit, the language's verdict and
      approximation words and English verdict words glued to CJK text (BANNED_WORD), 兩/百分之 (NUMBER_WORD), and
      domains glued to CJK characters (URL_OR_HTML), and English words other than kept terms, names and values
      (MIXED_LANGUAGE). "一" is masked before the validator's numeral check (it is
      part of ordinary words: 一般, 之一), and ASCII next to CJK gets a space so \b-based checks see whole words.
    """
    base = base or (lambda p, mw: check_sentence(p, ev, mw))
    fails: list[str] = []
    if not script_matches(s.text, language):
        fails.append(LANGUAGE_MISMATCH)
    if not is_cjk(language):
        fails += base(s, MAX_WORDS if is_english(language) else LATIN_MAX_WORDS)
        return list(dict.fromkeys(fails))
    probe = Sentence(text=_ASCII_CJK_GAP.sub(" ", s.text.replace("一", "某")), source_ids=s.source_ids)
    fails += base(probe, None)
    if cjk_units(s.text) > fp.CJK_MAX_UNITS_BY_LANG[language.lower()[:2]]:
        fails.append(TOO_LONG)
    if _BANNED_BY_LANG[language.lower()[:2]].search(s.text) or _EN_GLUED.search(s.text):
        fails.append(BANNED_WORD)
    if _ZH_NUMBER.search(s.text):
        fails.append(NUMBER_WORD)
    if mixed_latin(s.text, ev.latin_terms):
        fails.append(MIXED_LANGUAGE)
    cited_hosts = {h for i in s.source_ids for h in ev.cited_hosts.get(i, ())}
    if URL_OR_HTML not in fails and (re.search(r"https?|www\.", s.text, re.I) or any(
            m.group(0).lower() not in cited_hosts for m in _CJK_DOMAIN_RE.finditer(s.text))):
        fails.append(URL_OR_HTML)
    return list(dict.fromkeys(fails))


_PROPER_START = re.compile(r"^(?:\S*\.\S|[A-Z]\S*[A-Z]|Auckland\b|Kāinga\b|Kainga\b|Settled\b)")
_ANY_PREFIX = re.compile(r"^\s*(?:依你提供的資訊|依你提供的资讯|根据你提供的信息|根據你提供的資訊)\s*[，,、]?\s*")


def add_prefix(s: Sentence, prefix: str = PREFIX_EN) -> Sentence:
    """Code-added prefix. English: lower-case the first letter unless it starts a name ("Settled.govt.nz",
    "LINZ"). A prefix the model wrote itself is removed first."""
    body = _ANY_PREFIX.sub("", strip_prefix(s.text)).strip()
    if prefix == PREFIX_EN and body and not _PROPER_START.match(body):
        body = body[:1].lower() + body[1:]
    return Sentence(text=prefix + body, source_ids=s.source_ids)


# ---- user value vs. property value (5.3; user decision 10/10: show both) -------------------------
def comparison_sentence(key: str, value: Any, f: FieldView, language: str = "en") -> Optional[Sentence]:
    """A code-written sentence comparing the user's value with the property field (status found).

    It names both values and cites the field's source_id. It is a program sentence: the user's value is
    allowed in it (and only in it); the validator still checks it (see check_comparison). Chinese wording is
    written here (zh-Hant, zh-Hans by _zh); for other languages the English sentence is translated later
    (translate_parts) or kept.
    """
    zh = is_zh(language)
    council = (f.source_id or "").startswith("ac_")
    src_zh, src_en = ("市議會資料", "the council") if council else ("公開紀錄", "public records")
    if key == "year_built":
        dr = (f.model_extra or {}).get("decade_range")
        decade = dr[0] if (isinstance(dr, list) and len(dr) == 2 and all(isinstance(x, int) for x in dr)
                           and dr[0] % 10 == 0 and dr[1] == dr[0] + 9) else None
        u_dec = re.fullmatch(r"(\d{3}0)s", str(value))
        u_int = value if isinstance(value, int) else None
        if decade is not None:
            same = (u_int is not None and decade <= u_int <= decade + 9) or (u_dec and int(u_dec.group(1)) == decade)
            c_zh, c_en = f"{decade} 年代（{decade}–{decade + 9}）", f"the {decade}s ({decade}–{decade + 9})"
        else:
            cv = to_decimal(f.value)
            if cv is None:
                return None
            same = u_int is not None and Decimal(u_int) == cv
            c_zh, c_en = f"{_n(cv)} 年", f"{_n(cv)}"
        u_zh = f"{u_dec.group(1)} 年代" if u_dec else f"{u_int} 年"
        u_en = f"the {u_dec.group(1)}s" if u_dec else f"{u_int}"
        if same:
            zh_t = (f"你提供的 {u_zh}落在{src_zh}記錄的 {c_zh}內。" if decade is not None and u_int is not None
                    else f"你提供的 {u_zh}和{src_zh}記錄的 {c_zh}一致。")
            en_t = (f"The year you gave, {u_en}, falls within {src_en}'s recorded decade, {c_en}."
                    if decade is not None and u_int is not None
                    else f"The year you gave, {u_en}, matches {src_en}'s record, {c_en}.")
        else:
            zh_t = f"你提供的建造年份是 {u_zh}；{src_zh}記錄為 {c_zh}。以下說明以{src_zh}為準。"
            en_t = f"You gave the year built as {u_en}; {src_en} records {c_en}, and this answer uses {src_en}'s record."
    else:
        cv = str(f.value)
        shown = cladding_table.label(str(value))
        same = cv.strip().lower() == shown.lower() or cladding_table.normalize(cv) == value
        if same:
            zh_t = f"你提供的外牆 {shown} 和{src_zh}記錄的 {cv} 一致。"
            en_t = f"The cladding you gave, {shown}, matches {src_en}'s record, {cv}."
        else:
            zh_t = f"你提供的外牆是 {shown}；{src_zh}記錄為 {cv}。以下說明以{src_zh}為準。"
            en_t = f"You gave the cladding as {shown}; {src_en} records {cv}, and this answer uses {src_en}'s record."
    text = _zh(zh_t, language) if zh else en_t
    return Sentence(text=text.replace("  ", " "), source_ids=[f.source_id])


def _with_years(ev: Evidence, s: Sentence, years: list) -> Evidence:
    allowed = {k: list(v) for k, v in ev.allowed.items()}
    for sid in s.source_ids:
        allowed.setdefault(sid, []).extend(Allowed(Decimal(y)) for y in years if y is not None)
    return replace(ev, allowed=allowed)


def comparison_check(value: Any, ev: Evidence) -> BaseCheck:
    """Program sentence rule: the validator as usual, with the user's year allowed under the cited field only."""
    years = _year_numbers(value)
    return lambda p, mw: check_sentence(p, _with_years(ev, p, years), mw)


def check_comparison(s: Sentence, value: Any, ev: Evidence, language: str = "en") -> list[str]:
    return check_answer(s, ev, language, comparison_check(value, ev))


def _n(d: Decimal) -> str:
    return format(d.normalize(), "f")


# ---- links fallback for the refusal ---------------------------------------------------------------
_VALUE_Q = re.compile(r"valuation|value|worth|price|sold|sale|sell|market|\bCV\b|估價|估值|價值|價格|房價|售價|成交|行情|賣", re.I)
_FLOOD_Q = re.compile(r"flood|stormwater|overland|淹水|水災|洪水|積水|排水", re.I)
_HAZARD_Q = re.compile(r"hazard|landslide|tsunami|coast|erosion|liquefaction|earthquake|災害|地震|土石|山崩|海嘯|液化|侵蝕|海岸",
                       re.I)


def fallback_links(question: str, link_ids: list[str]) -> list[str]:
    """At most 2 links chosen by question type, from the ids PropertyContext actually has."""
    if _VALUE_Q.search(question):
        want = ["homes", "oneroof", "council_valuation"]
    elif _FLOOD_Q.search(question):
        want = ["council_flood_viewer", "council_geomaps"]
    elif _HAZARD_Q.search(question):
        want = ["council_geomaps", "council_flood_viewer"]
    else:
        want = ["council_geomaps"]
    got = [i for i in want if i in link_ids][:2]
    return got or link_ids[:1]


# ---- REPORT_MOCK=true: no Gemini call ---------------------------------------------------------------
_MOCK_OOS = re.compile(r"school|\bzon(?:e|es|ing)\b|commute|commuting|crime|neighbou?r|traffic|investment return|"
                       r"學區|學校|校區|通勤|治安|犯罪|鄰居|交通|学区|学校|通勤|治安", re.I)
MOCK_ANSWER = "This is a mock answer; a real answer cites the public sources listed in this report."
MOCK_ANSWER_ZH = "這是模擬回答；正式回答會引用本報告列出的公開來源。"


def mock_classify(question: str) -> ClassifyOut:
    """The classify step by keywords. Values still go through extract_user_supplied."""
    year = None
    m = re.search(r"(?<!\d)(\d{3}0s)(?![\w])", question) or re.search(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", question)
    if m:
        year = m.group(1)
    cladding = cladding_table.normalize(question)
    oos = bool(_MOCK_OOS.search(question)) and not (_FLOOD_Q.search(question) or _HAZARD_Q.search(question))
    return ClassifyOut(language=normalize_language("", question), out_of_scope=oos, year_built=year,
                       cladding=cladding)


def mock_answer(ev: Evidence, kb_items: dict[str, dict], used: dict, language: str) -> tuple[list[Sentence], int]:
    """claim_en of the items the user's value selected, verbatim (English: mock mode does not translate); else one
    fixed mock sentence (Chinese for a Chinese question)."""
    passed, dropped = [], 0
    if used:
        for i in sorted(ev.user_kb):
            claim = (kb_items.get(i) or {}).get("claim_en")
            if not claim:
                continue
            s = Sentence(text=claim, source_ids=[i])
            if check_answer(s, ev):
                dropped += 1
            else:
                passed.append(s)
    if not passed:
        sid = next((i for i in sorted(ev.known_ids) if not i.startswith("kb_")), None)
        zh = is_zh(language)
        s = Sentence(text=_zh(MOCK_ANSWER_ZH, language) if zh else MOCK_ANSWER, source_ids=[sid] if sid else [])
        if not check_answer(s, ev, language if zh else "en"):
            passed.append(s)
    return passed, dropped


# ---- program facts (prompt.program_facts): shown as their drafts, never rewritten ------------------
# Questions about a field, for the safety net when the model names no program fact for it. Any language is
# understood by the model; these cover the common English, Chinese and Japanese wordings of the demo questions.
_FIELD_Q = {
    "cladding": re.compile(r"cladding|exterior|outside walls?|\bwalls?\b|weatherboard|stucco|plaster|monolithic|"
                           r"roughcast|brick|leaky|外牆|外墙|牆|墙|漏水|外壁|雨漏り", re.I),
    "year_built": re.compile(r"\bbuilt\b|\bbuild\b|year built|how old|\bage\b|decade|建造|屋齡|屋龄|哪一年蓋|哪一年盖|"
                             r"幾年蓋|几年盖|築年|建築年|建てられ", re.I),
    "last_sale": re.compile(r"last sale|last sold|sale price|sold for|成交|売却|販売価格", re.I),
}


@dataclass
class Part:
    """One answer sentence before the language step. `native`: already in the answer language (a checked
    rewrite, or Chinese written by code); otherwise English, to be translated. `base`: the validator rules for
    this sentence with its own allowances (a program fact's or comparison's user year)."""
    sentence: Sentence
    native: bool
    user: bool
    base: Optional[BaseCheck] = None
    keep: tuple[str, ...] = ()  # values a translation must copy as written (a title, the cladding, land-use words)


def program_sentences(fs: list, named: list[str], question: str, ev: Evidence) -> list[Part]:
    """The drafts of the program facts the model named, plus (safety net) the program fact of a field the
    question asks about; each checked by prompt.check_program. English: translated later for other languages;
    prefixed later when it states what the user entered."""
    by_id = {f.id: f for f in fs if f.program}
    ids = [i for i in named if i in by_id]
    for f in by_id.values():
        field = f.topic.removeprefix("program:")
        rx = _FIELD_Q.get(field)
        if rx and rx.search(question) and f.id not in ids:
            ids.append(f.id)
    out = []
    for i in dict.fromkeys(ids):
        f = by_id[i]
        s = Sentence(text=f.draft(), source_ids=list(f.source_ids))
        base = (lambda slots: lambda p, mw: facts_prompt.check_program(p, ev, slots, mw))(f.slots)
        fails = check_answer(s, ev, "en", base)
        if fails:
            log.warning("program sentence %s dropped: %s", i, ",".join(fails))
            continue
        out.append(Part(s, native=False, user=f.user, base=base, keep=text_values([f])))
    return out


# ---- restricted translation of English program sentences (6.1) ------------------------------------
_TR_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_TR_DOMAIN = re.compile(r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9-])")
TRANSLATION_CHANGED = "translation_changed"  # numbers, terms, negation or length differ from the English


def text_values(facts: list) -> tuple[str, ...]:
    """The text values a fact shows (title name, cladding, land-use words): a translation keeps them as written."""
    return tuple(sl.render() for f in facts for sl in f.slots.values() if sl.kind == "text" and str(sl.value).strip())


def check_translation(orig: Sentence, text: str, language: str, ev: Evidence,
                      base: Optional[BaseCheck] = None, keep: tuple[str, ...] = ()) -> list[str]:
    """A translation is used only when: its numbers are exactly the English sentence's numbers; it has no URL and
    no domain the English does not have; it is at most 1.5 times as long (nothing added); it keeps the English
    terms (fp.KEEP_ENGLISH) the English has; for Chinese/Japanese/Korean it keeps a negation when the English has
    one; and it passes check_answer in the target language (with the sentence's own validator allowances)."""
    from collections import Counter

    from .validator import _CJK_NEG_RE, negations

    t = tidy(text or "", language)
    if not t:
        return [EMPTY_TRANSLATION]
    fails = []
    if Counter(_TR_NUM.findall(t)) != Counter(_TR_NUM.findall(orig.text)):
        fails.append(TRANSLATION_CHANGED)
    if re.search(r"https?:|www\.|<|@", t, re.I) or not {d.lower() for d in _TR_DOMAIN.findall(t)} <= {
            d.lower() for d in _TR_DOMAIN.findall(orig.text)}:
        fails.append(URL_OR_HTML)
    if len(t) > max(40, int(len(orig.text) * 1.5)):
        fails.append(TRANSLATION_CHANGED)
    if any(term in orig.text and term not in t for term in (*fp.KEEP_ENGLISH, *keep)):
        fails.append(TRANSLATION_CHANGED)
    if is_cjk(language) and negations(orig.text) and not _CJK_NEG_RE.search(t):
        fails.append(TRANSLATION_CHANGED)
    fails += check_answer(Sentence(text=t, source_ids=orig.source_ids), ev, language, base)
    return list(dict.fromkeys(fails))


EMPTY_TRANSLATION = "empty_translation"


def check_prefix(text: str, language: str) -> bool:
    t = (text or "").strip().rstrip(" ,，、")
    return bool(t) and len(t) <= 40 and not re.search(r"\d|https?:|www\.|<|@|\.[a-z]{2,}", t, re.I) \
        and script_matches(t, language)


def finish(parts: list[Part], language: str, ev: Evidence,
           translate: Callable[..., Optional[list[str]]], debug: dict) -> list[Sentence]:
    """The language step: English parts are translated (one restricted call for all of them, plus the prefix
    for languages other than Chinese); sentences whose translation fails check_translation are sent once more
    with the words to copy; a second failure leaves the whole sentence in English. The prefix is added in the
    language the sentence ends up in."""
    if is_english(language):
        return [add_prefix(p.sentence) if p.user else p.sentence for p in parts]
    english = [p for p in parts if not p.native]
    want_prefix = any(p.user for p in parts) and not is_zh(language)
    texts = [strip_prefix(p.sentence.text) for p in english] + ([PREFIX_EN.strip().rstrip(",")] if want_prefix else [])
    got = translate(texts) if texts else None
    if got is not None and len(got) != len(texts):
        log.info("translation returned %d texts for %d; using English", len(got), len(texts))
        got = None
    debug["translations"] = []
    done: dict[int, Sentence] = {}
    retry: list[Part] = []
    for n, p in enumerate(english):
        t = got[n] if got else None
        fails = (check_translation(p.sentence, t, language, ev, p.base, p.keep) if t is not None
                 else ["not_translated"])
        debug["translations"].append({"en": p.sentence.text, "text": t, "fails": fails})
        if fails:
            log.info("translation not used: %s", ",".join(fails))
            if t is not None:
                retry.append(p)
        else:
            done[id(p)] = Sentence(text=tidy(t, language), source_ids=p.sentence.source_ids)
    if retry:
        copy = sorted({w for p in retry for w in (*_TR_NUM.findall(p.sentence.text), *p.keep,
                                                  *(k for k in fp.KEEP_ENGLISH if k in p.sentence.text))})
        again = translate([strip_prefix(p.sentence.text) for p in retry], copy)
        for n, p in enumerate(retry if again and len(again) == len(retry) else []):
            fails = check_translation(p.sentence, again[n], language, ev, p.base, p.keep)
            debug["translations"].append({"en": p.sentence.text, "text": again[n], "fails": fails, "retry": True})
            if not fails:
                done[id(p)] = Sentence(text=tidy(again[n], language), source_ids=p.sentence.source_ids)
    if is_zh(language):
        prefix = PREFIXES.get(language, PREFIXES["zh-Hant"])
    else:
        t = got[-1] if (got and want_prefix) else None
        joiner = "、" if language.lower().startswith("ja") else ", "
        prefix = (t.strip().rstrip(" ,，、") + joiner) if t and check_prefix(t, language) else None
        debug["prefix"] = prefix
    out = []
    for p in parts:
        s = done.get(id(p))
        in_language = p.native or s is not None
        s = s or p.sentence
        if p.native:
            s = Sentence(text=tidy(s.text, language), source_ids=s.source_ids)
        if p.user:
            s = add_prefix(s, prefix) if (in_language and prefix) else add_prefix(s)
        out.append(s)
    return out


# ---- main flow ----------------------------------------------------------------------------------
def answer_followup(req: FollowupRequest, refusal_mode: str = "exempt",
                    generate: Optional[JsonGenerator] = None, debug: Optional[dict] = None,
                    web_search: bool = False) -> FollowupResponse:
    """`debug`, when given, receives the fact list, the checked attempts and the composed answer (for eval).
    `web_search` lets an out-of-scope question be answered from a Google Search. The route turns it on; eval runs
    leave it off, because the Gemini API terms do not allow storing such answers."""
    generate = generate or gemini_json
    debug = debug if debug is not None else {}
    mock = mock_enabled()
    clock = _Clock()
    history = [t.model_dump() for t in req.history]
    link_ids = [link.id for link in req.links()]
    warnings: list[str] = []

    def translate(texts: list[str], copy: Optional[list[str]] = None) -> Optional[list[str]]:
        """Sees only the fixed sentences, the language code and (on the retry) the words to copy, which come from
        those sentences (fp.translate_prompt)."""
        if mock or clock.left() < 1.5:
            return None
        try:
            return _call(generate, fp.translate_prompt(texts, language, copy), TranslateOut,
                         clock.budget(0.5)).texts
        except ReportError as e:
            log.info("translation failed (%s); using English", e.code)
            return None

    # 1. classify
    if mock:
        c = mock_classify(req.question)
    else:
        c = _call(generate, fp.classify_prompt(req.question, history, [{"id": link.id, "label": link.label}
                                                                        for link in req.links()]),
                  ClassifyOut, clock.budget(0.4))
    language = normalize_language(c.language, req.question)
    debug["language"] = {"claimed": c.language, "used": language}
    supplied = extract_user_supplied(req.question, c)
    links = [i for i in dict.fromkeys(c.links) if i in link_ids][:3]

    # 2. out of scope: an answer from a Google Search when web pages can answer it, else the fixed refusal
    if c.out_of_scope:
        address = str((req.property_context.get("address") or {}).get("matched") or "")
        if c.searchable and web_search and refusal_mode == "exempt" and address:
            prompt = fp.web_prompt(address, req.question, history)
            # The model does not always search: asked once more, time allowing, when the answer names no source.
            # Neither call gets all the time that is left: web, the caller, has its own limit for the whole request.
            try:
                web = gemini_search(prompt, clock.budget(0.6))
                if not web and clock.left() >= 6.0:
                    web = gemini_search(prompt, clock.budget(0.6))
            except (gemini.ModelUnavailable, ReportError) as e:
                log.warning("web answer unavailable: %s", type(e).__name__)
                web = None
            if web:
                return FollowupResponse(language=language, answer=[], out_of_scope=True, user_supplied=supplied,
                                        links=links, dropped=0, warnings=warnings, web=web)
        refusal, refusal_lang = Sentence(text=REFUSAL_EN, source_ids=[]), "en"
        if is_zh(language):
            refusal, refusal_lang = Sentence(text=_zh(REFUSAL_ZH, language), source_ids=[]), language
        elif not is_english(language):
            got = translate([REFUSAL_EN])
            no_ids = lambda p, mw: [f for f in check_sentence(p, Evidence(), mw) if f != "no_source_ids"]  # noqa: E731
            if got and len(got) == 1 and not check_translation(refusal, got[0], language, Evidence(), no_ids):
                refusal, refusal_lang = Sentence(text=tidy(got[0], language), source_ids=[]), language
            else:
                log.info("refusal translation not used; English")
        links = links or fallback_links(req.question, link_ids)
        if refusal_mode == "strict":
            data = _kb(req, clock)
            ev = build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
            if check_answer(refusal, ev, refusal_lang):
                raise ReportError(502, "validation_failed", "No answer sentence passed validation.")
        return FollowupResponse(language=language, answer=[refusal], out_of_scope=True, user_supplied=supplied,
                                links=links, dropped=0, warnings=warnings)

    # 3. merge user_supplied; property values with status "found" win; re-run rules when something new is used
    pf = req.property_fields()
    chat = dict(req.chat_supplied())
    notes: list[str] = []
    used: dict[str, Any] = {}
    compared: list[tuple[str, Any, FieldView]] = []
    for key, value in supplied.items():
        f = pf.get(key)
        if f is not None and f.status == "found" and f.value is not None:
            compared.append((key, value, f))
            notes.append(f"The program already states how the user's {key.replace('_', ' ')} compares with the "
                         f"record; the record is used. Do not repeat that comparison and do not write the user's "
                         f"value.")
        else:
            used[key] = value
    user_inputs = dict(req.user_inputs)
    rules_output = req.rules_output
    if used and any(chat.get(k) != v for k, v in used.items()):
        chat.update(used)
        user_inputs["chat_supplied"] = chat
        try:
            rules_output = call_rules(req.property_context, user_inputs, max(0.5, min(5.0, clock.left() * 0.3)))
        except ReportError:
            if not mock:
                raise
            warnings.append("rules_rerun_unavailable")  # mock: answer from the input RulesOutput
    elif used:
        user_inputs["chat_supplied"] = chat
    eff = ReportRequest(property_context=req.property_context, user_inputs=user_inputs, rules_output=rules_output)

    data = _kb(eff, clock)
    ev = build_evidence(eff, data.items, data.sources.keys(), source_urls=data.sources.urls)
    titles = {s.id: s.title or "" for s in eff.sources()} | data.sources
    ev.latin_terms = latin_terms(eff, [])
    if used:
        allow_user_year(ev, used)
        what = ", ".join(k.replace("_", " ") for k in used)
        notes.append(f"The user just told us the {what}. This is unverified and is used for this answer: the facts "
                     "with \"user\": true depend on it" + ("." if ev.user_kb else
                                                         "; it selected no extra fact, so do not write the value."))
    elif ev.user_kb:
        notes.append("Facts with \"user\": true depend on what the user told us earlier (unverified).")

    # Program sentences comparing the user's value with the property value (both values, cited).
    # Chinese wording is written by code; other non-English languages translate the English sentence later.
    fixed: list[Part] = []
    for key, value, f in compared:
        s = comparison_sentence(key, value, f, language)
        native = is_english(language) or is_zh(language)
        fails = check_comparison(s, value, ev, language if native else "en") if s else ["no_council_value"]
        if fails:
            log.warning("comparison sentence dropped: %s", ",".join(fails))
        else:
            fixed.append(Part(s, native=native, user=False, base=comparison_check(value, ev),
                              keep=() if key == "year_built" else (cladding_table.label(str(value)), str(f.value))))

    # 4. answer from the fact list; check each rewrite; one retry when none passes and time allows; blocked
    #    sentences fall back to their facts' drafts
    passed: list[Part] = []
    dropped, out_links = 0, []
    last: Optional[ReportError] = None
    limit = max(0, MAX_ANSWER - len(fixed))
    if mock:
        sentences, dropped = mock_answer(ev, data.items, used, language)
        if fixed and not used:
            sentences = []  # the comparison is the answer; no filler sentence
        passed = [Part(s, native=is_english(language) or script_matches(s.text, language),
                       user=bool(set(s.source_ids) & ev.user_kb)) for s in sentences[:limit]]
    else:
        fs = facts_prompt.followup_facts(eff, ev, data.items, used)
        ev.latin_terms |= latin_terms(eff, fs)
        debug["facts"] = [{"id": f.id, "text": f.text, "draft": f.draft(), "user": f.user,
                           "source_ids": f.source_ids} for f in fs]
        debug["attempts"] = []

        def final(s: Sentence) -> list[str]:
            return check_answer(Sentence(text=tidy(s.text, language), source_ids=s.source_ids), ev, language)

        prog_ids = {f.id for f in fs if f.program}
        base = fp.answer_prompt(eff, ev, data.items, titles, req.question, history, notes, fs, language)
        prompt = base
        best: Optional[list] = None
        named: list[str] = []
        for attempt in range(2):
            if attempt and clock.left() < 6.0:
                break
            try:
                out = _call(generate, prompt, AnswerOut, clock.budget())
            except ReportError as e:
                if e.status == 504 or (e.code == "model_unavailable" and clock.left() < 6.0):
                    raise
                last = e
                prompt = base
                continue
            # a sentence naming a program fact is replaced by that fact's draft (program_sentences)
            prog_named = [i for s in out.answer for i in s.fact_ids if i in prog_ids]
            rewrites = [s for s in out.answer if not set(s.fact_ids) & prog_ids]
            checked = [check_rewrite(s, fs, ev, final=final) for s in rewrites]
            debug["attempts"].append([{"text": s.text, "fact_ids": s.fact_ids, "filled": r.filled, "fails": r.fails}
                                      for s, r in zip(rewrites, checked)]
                                     + [{"program": i} for i in prog_named])
            for r in checked:
                if r.fails:
                    log.info("dropped answer sentence: %s", ",".join(r.fails))
            out_links = out.links
            score = sum(r.sentence is not None for r in checked) + len(prog_named)
            if best is None or score > sum(r.sentence is not None for r in best) + len(named):
                best, named = checked, prog_named
            if score:
                break
            last = ReportError(502, "validation_failed", "No answer sentence passed validation.")
            prompt = base + ("\nYour previous answer could not be used (" +
                             ", ".join(sorted({f for r in checked for f in r.fails} or {"no sentences"})) +
                             "). Follow every rule exactly.\n")
        prog = program_sentences(fs, named, req.question, ev)[:limit]
        debug["program"] = [p.sentence.text for p in prog]
        passed.extend(prog)
        limit -= len(prog)
        items = rewrite.compose(best or [], fs, limit, add_musts=False, sort=False,
                                check_draft=lambda s: check_answer(s, ev, "en")) if limit else []
        dropped = sum(r.sentence is None for r in best or [])
        debug["composed"] = [{"text": it.sentence.text, "fact_ids": it.fact_ids, "kind": it.kind} for it in items]
        debug["rewrite"] = rewrite.stats(items, best or [])
        fmap = {f.id: f for f in fs}
        for it in items:  # a rewrite is in the answer language; a draft is English
            passed.append(Part(it.sentence, native=it.kind == rewrite.REWRITE,
                               user=bool(it.user or set(it.sentence.source_ids) & ev.user_kb),
                               keep=text_values([fmap[i] for i in it.fact_ids if i in fmap])))
    if not passed and not fixed:
        raise last or ReportError(502, "validation_failed", "No answer sentence passed validation.")
    answer = finish(fixed + passed, language, ev, translate, debug)

    links = [i for i in dict.fromkeys(out_links + links) if i in link_ids][:3]
    return FollowupResponse(language=language, answer=answer, out_of_scope=False, user_supplied=supplied,
                            links=links, dropped=dropped, warnings=warnings)




def allow_user_year(ev: Evidence, used: dict[str, Any]) -> None:
    """P-V3 exception: the validated, used year is allowed only with the items it selected."""
    years = [Decimal(y) for y in _year_numbers(used.get("year_built"))]
    for i in ev.user_kb:
        ev.allowed.setdefault(i, []).extend(Allowed(y) for y in years)


def _kb(req: ReportRequest, clock: _Clock):
    try:
        return kb.load(req.selected_kb_ids(), timeout=max(0.5, min(5.0, clock.left())))
    except kb.KbUnavailable:
        raise ReportError(503, "kb_unavailable", "Knowledge-base items or sources could not be read.")
