"""POST /v1/followup: answer one question in the asker's language, validated like the summary.

Language (the report itself stays English): classify detects the question's language
(BCP 47, corrected by script in normalize_language); the answer is written in it, with English terms (LVR, DTI,
First Home Loan, Cross Lease, Unit Title, Freehold, Settled.govt.nz, ...) kept. `language` in the response is the
detected language, except when no model call could be made for the translation (allowance refused, request stopped,
no time): Chinese then gets fixed Chinese sentences written by code, any other language the English
sentences with `language` "en" (fixed_answer).

Flow: classify (Gemini: language, out-of-scope, stated year/cladding) -> code checks user_supplied ->
optional rules-api re-run with the merged chat_supplied -> fact list (prompt.followup_facts, rebuilt from the
re-run RulesOutput) -> answer (Gemini rewrites facts in the question's language, values as placeholders) ->
validator.check_rewrite plus the language checks (check_answer) -> blocked sentences fall back to their facts'
drafts -> English program sentences (drafts, program facts, comparison, refusal) get Chinese wording written by
code or a restricted translation (translate_parts), English when the translation fails its checks -> code-added
prefix in the language of the sentence. Out of scope: an answer from a Google Search when classify says web pages
can answer it (`web`, in the language of the question, shown whole with its sources and never run through the
validator), else a fixed refusal chosen by code.

Property fields the records do not have: year_built, cladding and last_sale
may be looked up with a Google Search (`field_search`): when the question asks about one whose status is
not_found/unavailable, or when web's "Search the web" button sends `search_field`. The search answer is
shown as returned (`web`); code extracts the year built and cladding from it (web_fields) into user_supplied, marked
"web_search" in user_supplied_source, and re-runs the rules with them. Sentences that depend on such a value get
the prefix "Based on a web search (not checked by this service), " instead of "Based on what you told us, ".

question and history are untrusted: they never enter the evidence or the translation prompt. The one exception
is a validated year_built that was used for the re-run; it is allowed only in sentences citing
kb_items_from_user_input.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Literal, Optional

import requests
from pydantic import BaseModel, Field, ValidationError, model_validator

from b_common import cladding as cladding_table
from b_common.report_schemas import FieldView, ReportRequest, Sentence

from . import gemini, kb, obs, protection, rewrite, web_fields
from . import followup_prompt as fp
from . import prompt as facts_prompt
from .facts import TITLE_WORDS, RewriteSentence
from .service import ReportError, mock_enabled
from .validator import (BANNED_WORD, MAX_WORDS, NUMBER_WORD, TOO_LONG, URL_OR_HTML, WEB_PREFIX, Allowed, Evidence,
                        build_evidence, check_rewrite, check_sentence, strip_prefix, to_decimal)

log = logging.getLogger("report-api.followup")
MAX_ANSWER = 3

REFUSAL_EN = ("This question is outside what this tool covers; the original sources linked below "
              "may help you look it up.")
# Chinese refusal written by code (zh-Hans via _zh); other languages get a restricted translation of REFUSAL_EN.
REFUSAL_ZH = "這個問題超出本工具涵蓋的範圍；下方的原始連結可供你查詢。"
PREFIX_EN = "Based on what you told us, "
PREFIXES = {"zh-Hant": "依你提供的資訊，", "zh-Hans": "根据你提供的信息，"}
# A value code extracted from a Google Search answer; other languages translate it.
WEB_PREFIX_EN = WEB_PREFIX
WEB_PREFIXES = {"zh-Hant": "依網路搜尋結果（未經本系統核對），", "zh-Hans": "依网络搜索结果（未经本系统核对），"}
USER, WEB_SEARCH = facts_prompt.USER, facts_prompt.WEB_SEARCH
LANGUAGE_MISMATCH = "language_mismatch"  # the sentence is not in the language it is meant to be in
MIXED_LANGUAGE = "mixed_language"  # a CJK sentence with English words that are not a kept term, name or value


# ---- request / response -------------------------------------------------------------------------
class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=2000)


class FollowupRequest(ReportRequest):
    question: str = Field(default="", max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=6)
    # web's "Search the web" button: search for this one field, no classify step (question optional).
    search_field: Optional[Literal["year_built", "cladding", "last_sale"]] = None
    # The button with no question: the page's language (BCP 47, e.g. "zh-Hant"), for the search answer and the
    # sentences derived from it. Ignored when a question is sent (its language is detected as usual). Default en.
    language: Optional[str] = Field(default=None, max_length=35, pattern=r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")

    @model_validator(mode="after")
    def _question_or_field(self):
        if not self.question.strip() and not self.search_field:
            raise ValueError("question is required unless search_field is set")
        return self


class WebSource(BaseModel):
    title: str  # the site's domain, as the search returns it
    url: str    # Google's redirect to the page


class WebAnswer(BaseModel):
    """An answer grounded in Google Search. Gemini API terms: shown as returned, with its search suggestions."""
    text: str
    sources: list[WebSource]
    search_suggestions: str  # Google's HTML widget
    # Field searches only: the fields searched for, and the values code extracted
    # from `text` ({"year_built": 1987, "cladding": "wood", "last_sale": {"date": "March 2019", "price": "$850,000"}}).
    # `text` itself is never changed.
    fields: list[str] = Field(default_factory=list)
    extracted: dict[str, Any] = Field(default_factory=dict)
    # Only when a value in `extracted` (year_built, cladding) was NOT put in user_supplied because the property has
    # several owners ("multi_unit_title": Unit Title, Cross Lease, or several title types at the address). Absent
    # otherwise (the route leaves out null keys).
    autofill_skipped: Optional[Literal["multi_unit_title"]] = None


class FollowupResponse(BaseModel):
    language: str
    answer: list[Sentence]
    out_of_scope: bool
    user_supplied: dict[str, Any]
    # Only when a user_supplied value came from a web search: {field: "user" | "web_search"} for each user_supplied
    # key ("web_search": code extracted it from the search answer in `web`). Absent: every value is the user's.
    # web sends it back as user_inputs.chat_supplied_source for the values still in its form (see README).
    user_supplied_source: Optional[dict[str, str]] = None
    links: list[str]
    dropped: int
    # rules_rerun_unavailable (mock mode only), generation_degraded, search_unavailable (a Google Search was called
    # for and gave no answer: refused, failed, out of time or no source)
    warnings: list[str] = Field(default_factory=list)
    web: Optional[WebAnswer] = None  # a Google Search answer: out of scope (with an empty `answer`) or a field search


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
    # False: no fact in the list says anything about what was asked (a definition, a comparison of two terms, a
    # topic the facts do not hold). The reply is then a Google Search answer, or the fixed "cannot answer" sentence.
    answered: bool = True
    answer: list[RewriteSentence] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class TranslateOut(BaseModel):
    texts: list[str]


# (prompt, response schema, timeout_s) -> raw JSON text. Replaced in tests.
JsonGenerator = Callable[[str, type, float], str]


def gemini_json(prompt: str, schema: type, timeout_s: float) -> str:
    from google import genai
    from google.genai import types

    gemini.check_stopped()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise gemini.no_key()
    timeout_s = gemini.admit(prompt, timeout_s)
    obs.note_call()
    try:
        client = genai.Client(api_key=key, http_options=gemini.http_options(timeout_s))
        resp = client.models.generate_content(
            model=gemini.model_name(), contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema, temperature=gemini.TEMPERATURE,
                max_output_tokens=protection.max_output_tokens(),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
    except Exception as e:  # timeouts, APIError (429, 5xx, bad key), network
        raise gemini.failed(log, "gemini call", e) from e
    if not resp.text:
        obs.note_error(obs.SCHEMA)
        raise gemini.ModelOutputInvalid("empty response")
    return resp.text


def gemini_search(prompt: str, timeout_s: float) -> Optional[WebAnswer]:
    """One Gemini call with the Google Search tool. None when the answer names no source. Replaced in tests."""
    from google import genai
    from google.genai import types

    gemini.check_stopped()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise gemini.no_key()
    timeout_s = gemini.admit(prompt, timeout_s, search=True)
    obs.note_call(search=True)
    try:
        client = genai.Client(api_key=key, http_options=gemini.http_options(timeout_s))
        resp = client.models.generate_content(
            model=gemini.model_name(), contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())], temperature=gemini.TEMPERATURE,
                max_output_tokens=protection.max_output_tokens(),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        meta = resp.candidates[0].grounding_metadata if resp.candidates else None
        text = resp.text or ""  # shown as returned: strip() only tests for an empty answer
    except Exception as e:  # timeouts, APIError (429, 5xx, bad key), network
        raise gemini.failed(log, "gemini search call", e) from e
    pages = {c.web.uri: c.web.title or "" for c in (meta and meta.grounding_chunks) or [] if c.web and c.web.uri}
    if not (text.strip() and pages):
        return None
    entry = meta.search_entry_point
    return WebAnswer(text=text, sources=[WebSource(title=t, url=u) for u, t in pages.items()],
                     search_suggestions=(entry and entry.rendered_content) or "")


def _parse(raw: str, schema: type):
    try:
        return schema.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as e:
        obs.note_error(obs.SCHEMA)
        raise gemini.ModelOutputInvalid(type(e).__name__) from e  # no model text in the message


# One deadline for the request (gemini.Budget): every Gemini call, search, translation and retry is made only with
# gemini.ADMIT_S (11 s) left and is sent with at least gemini.MIN_CALL_S (10 s, the API minimum); at most
# gemini.MAX_SEARCHES Google Search calls per request. Without time for a call: program drafts or fixed sentences.
_Clock = gemini.Budget
MIN_CALL_S = gemini.MIN_CALL_S


def _search(prompt: str, clock: _Clock, share: float) -> Optional[WebAnswer]:
    """One Google Search call when the request's search limit and time allow it; None otherwise."""
    if not clock.can_search() or protection.stopped():
        return None
    clock.searches += 1
    return gemini_search(prompt, clock.timeout(share))


def _call(generate: JsonGenerator, prompt: str, schema: type, timeout: float):
    try:
        return _parse(generate(prompt, schema, timeout), schema)
    except gemini.ModelUnavailable as e:
        raise ReportError(503, "model_unavailable", "The language model did not answer.", e.category)
    except gemini.ModelOutputInvalid:
        raise ReportError(502, "model_output_invalid", "The language model's answer did not match the schema.")


# ---- user_supplied ------------------------------------------------------------------------------
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


_CLAUSE_SPLIT = re.compile(r"[^,，、.?!。？！;；\n]+[,，、.?!。？！;；]?")
_ASKS_START = re.compile(r"^\s*(?:and\s+|or\s+|so\s+)?(?:when|what|which|how|is|was|were|are|does|did|do|has|have|had|could|"
                         r"can|would|will|any|tell)\b", re.I)
_ASKS_ZH = re.compile(r"嗎|吗|是否|是不是|有沒有|有没有|呢|か[。？?]?$|ですか")


def affirmed(question: str, key: str, value: Any) -> bool:
    """The question states the value rather than asks about it: a clause that names it is not itself a question.
    "It was built in 1997, what does that mean?" states 1997; "Was it built in 1997?" and "這房子是 1997 年蓋的嗎？"
    ask about it."""
    if key == "year_built":
        m = re.fullmatch(r"(\d{3}0)s", str(value))
        rx = re.compile(rf"(?<!\d){m.group(1) if m else re.escape(str(value))}(?!\d)")
    else:
        rx = dict(cladding_table._COMPILED).get(str(value))
        if rx is None:
            return False
    for clause in _CLAUSE_SPLIT.findall(question):
        if not rx.search(clause):
            continue
        asking = clause.rstrip().endswith(("?", "？")) or _ASKS_START.search(clause) or _ASKS_ZH.search(clause)
        if not asking:
            return True
    return False


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


def page_language(code: Optional[str]) -> str:
    """The language web sends for a button request without a question (the page's language), as a code the answer
    steps know: zh variants -> zh-Hant / zh-Hans, ja, ko; anything unusable -> en."""
    lang = (code or "en").strip().replace("_", "-")
    if not _BCP47_RE.fullmatch(lang):
        return "en"
    low = lang.lower()
    if low.startswith("zh"):
        return "zh-Hans" if "hans" in low or low.endswith(("-cn", "-sg")) else "zh-Hant"
    if low.startswith(("ja", "ko")):
        return low[:2]
    return "en" if low.startswith("en") else lang


def is_english(language: str) -> bool:
    return language.lower().startswith("en")


def is_cjk(language: str) -> bool:
    return language.lower().startswith(("zh", "ja", "ko"))


def is_zh(language: str) -> bool:
    return language.lower().startswith("zh")


_TO_HANS = str.maketrans("議會資記錄說為內準與牆這個問題蓋範圍連結許幫詢調錯與據時間準鍵戶稱開關東區層單樣嗎實際處筆價貸額屬權產圖齡庫訊標準舊歲牠網尋賃樓塊類輸質",
                         "议会资记录说为内准与墙这个问题盖范围连结许帮询调错与据时间准键户称开关东区层单样吗实际处笔价贷额属权产图龄库讯标准旧岁它网寻赁楼块类输质")


def _zh(text: str, language: str) -> str:
    """Code-written Chinese is zh-Hant; zh-Hans maps the few characters it uses."""
    return text.translate(_TO_HANS) if language == "zh-Hans" else text


# ---- validation (validator.validate plus language-dependent rules) ------------------------------
LATIN_MAX_WORDS = 35  # other languages written in Latin script: a little more room than English
_CJK_NUM_SPACE = [(re.compile(r"(\d)(?=[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af])"), r"\1 "),
                  (re.compile(r"(?<=[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af])(?=[$\d])"), " ")]
_ASCII_CJK_GAP = re.compile(r"(?<=[A-Za-z0-9])(?=[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef])|"
                            r"(?<=[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef])(?=[A-Za-z0-9])")
# Verdict, advice and approximation words. The Chinese words for "may" are allowed only when a cited
# source's English has may/might/can/could (hedged_source); never as the model's own guess.
_BANNED_BY_LANG = {
    "zh": re.compile(r"安全|危險|危险|無風險|无风险|沒有風險|没有风险|低風險|低风险|高風險|高风险|風險低|风险低|風險高|风险高|"
                     r"建議|建议|推薦|推荐|應該|应该|應當|应当|必須|必须|務必|务必|一定要|將會|将会|一定會|一定会|大約|"
                     r"大约|大概|左右|將近|将近|幾乎|几乎|保證|保证|划算|值得|便宜|別買|别买|不要買|不要买|該買|该买|可以買|"
                     r"可以买|擔心|担心|放心|沒有災害|没有灾害|沒有淹水|没有淹水|不會淹水|不会淹水"),
    "ja": re.compile(r"安全|危険|推奨|お勧め|おすすめ|オススメ|すべき|べきです|べきだ|必ず|保証|およそ|大体|だいたい|多分|"
                     r"たぶん|恐らく|おそらく|心配|安心|割安|お得|リスクが低|リスクが高|リスクはありません"),
    "ko": re.compile(r"안전|추천|권장|해야 합니다|해야 한다|해야 해|반드시|보장|아마|대략|걱정|안심|위험이 낮|위험이 높"),
}
_ZH_MAY = re.compile(r"可能|或許|或许|也許|也许")  # banned unless the cited English has a may-type word
_MAY_EN = re.compile(r"\b(?:may|might|can|could)\b", re.I)
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


# English kept in any answer: terms, publishers, title names, layer names, the cladding table, plus (per
# request) the values and labels code fills in and the property's own text values (latin_terms).
_LATIN_TERMS = {*fp.KEEP_ENGLISH, "Auckland Council", "Auckland", "Reserve Bank", "RBNZ", "Kainga Ora",
                "Natural Hazards Commission", "EQC", "CV", "body corporate", "high-LVR", "high-DTI", "loan-to-value",
                "debt-to-income", "LIM reports", "LIM report", "Land Information Memorandum",
                *facts_prompt.LAYER_NAMES.values(), *facts_prompt.SHORT_NAMES.values(),
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


def hedged_source(s: Sentence, ev: Evidence, extra_en: str = "") -> bool:
    """A source the sentence cites (kb claim_en, a fact's English text, or `extra_en` = the English being
    translated) says may/might/can/could, so a Chinese 可能/或許 is a retelling, not a guess."""
    texts = [extra_en, *(ev.kb_claims.get(i, "") for i in s.source_ids), *(ev.fact_text.get(i, "") for i in s.source_ids)]
    return any(_MAY_EN.search(t) for t in texts if t)


def check_answer(s: Sentence, ev: Evidence, language: str = "en", base: Optional[BaseCheck] = None,
                 source_en: str = "") -> list[str]:
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
    banned = _BANNED_BY_LANG[language.lower()[:2]].search(s.text) or _EN_GLUED.search(s.text)
    if is_zh(language) and not banned and _ZH_MAY.search(s.text) and not hedged_source(s, ev, source_en):
        banned = True
    if banned:
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
_ANY_PREFIX = re.compile(r"^\s*(?:依你提供的資訊|依你提供的资讯|根据你提供的信息|根據你提供的資訊|"
                         r"(?:依|根據|根据)網路搜尋結果(?:（未經本系統核對）)?|(?:依|根據|根据)网络搜索结果(?:（未经本系统核对）)?)"
                         r"\s*[，,、]?\s*")


def add_prefix(s: Sentence, prefix: str = PREFIX_EN) -> Sentence:
    """Code-added prefix. English: lower-case the first letter unless it starts a name ("Settled.govt.nz",
    "LINZ"). A prefix the model wrote itself is removed first."""
    body = _ANY_PREFIX.sub("", strip_prefix(s.text)).strip()
    if prefix in (PREFIX_EN, WEB_PREFIX_EN) and body and not _PROPER_START.match(body):
        body = body[:1].lower() + body[1:]
    return Sentence(text=prefix + body, source_ids=s.source_ids)


# ---- user value vs. property value: both are shown ------------------------------------------------
def comparison_sentence(key: str, value: Any, f: FieldView, language: str = "en",
                        web: bool = False) -> Optional[Sentence]:
    """A code-written sentence comparing the user's value with the property field (status found).

    It names both values and cites the field's source_id. It is a program sentence: the user's value is
    allowed in it (and only in it); the validator still checks it (see check_comparison). Chinese wording is
    written here (zh-Hant, zh-Hans by _zh); for other languages the English sentence is translated later
    (translate_parts) or kept. `web`: the value came from a web search, not from the user.
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
            if web:
                zh_t = zh_t.replace("你提供的", "網頁上的")
                en_t = en_t.replace("The year you gave", "the year found online")
        elif web:  # the web-search prefix is added in front (finish)
            zh_t = f"網頁上的建造年份是 {u_zh}；{src_zh}記錄為 {c_zh}。以下說明以{src_zh}為準。"
            en_t = (f"web pages give the year built as {u_en}; {src_en} records {c_en}, and this answer uses "
                    f"{src_en}'s record.")
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
            if web:
                zh_t = zh_t.replace("你提供的", "網頁上的")
                en_t = en_t.replace("The cladding you gave", "the cladding found online")
        elif web:  # the web-search prefix is added in front (finish)
            zh_t = f"網頁上的外牆是 {shown}；{src_zh}記錄為 {cv}。以下說明以{src_zh}為準。"
            en_t = f"web pages give the cladding as {shown}; {src_en} records {cv}, and this answer uses {src_en}'s record."
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
MOCK_ANSWER = "This is a mock answer; a real answer cites the public sources listed on this page."
MOCK_ANSWER_ZH = "這是模擬回答；正式回答會引用本工具列出的公開來源。"


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


# ---- program facts (prompt._program_topics): shown as their drafts, never rewritten ----------------
# Questions about a field, for the safety net when the model names no program fact for it. Any language is
# understood by the model; these cover the common English, Chinese and Japanese wordings of the demo questions.
_FIELD_Q = {
    "cladding": re.compile(r"cladding|exterior|outside walls?|\bwalls?\b|weatherboard|stucco|plaster|monolithic|"
                           r"roughcast|brick|leaky|外牆|外墙|牆|墙|漏水|外壁|雨漏り", re.I),
    "year_built": re.compile(r"\bbuilt\b|\bbuild\b|\bconstruct(?:ed|ion)\b|\berected\b|year built|how old|\bage\b|decade|"
                             r"when was (?:it|this|the) (?:home|house|place|property|building)? ?(?:made|put up|finished)|建造|屋齡|屋龄|哪一年蓋|哪一年盖|"
                             r"幾年蓋|几年盖|哪年|蓋的|盖的|蓋好|盖好|建成|落成|築年|建築年|建てられ|建於|建于|興建|兴建|幾年建|几年建", re.I),
    "last_sale": re.compile(r"last sale|last sold|\bsold\b|sale price|sale history|purchase price|bought for|成交|"
                            r"賣出|卖出|售出|上次.{0,4}[賣卖售]|買賣紀錄|买卖记录|売却|販売価格", re.I),
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
    fields: frozenset = frozenset()  # the chat-supplied fields its facts depend on (prompt.from_web)
    web: Optional[bool] = None  # set when code knows the sentence uses a value found online (a comparison)


def program_sentences(fs: list, named: list[str], question: str, ev: Evidence,
                      skip: frozenset = frozenset()) -> list[Part]:
    """The drafts of the program facts the model named, plus (safety net) the program fact of a field the
    question asks about; each checked by prompt.check_program. English: translated later for other languages;
    prefixed later when it states what the user entered. `skip`: fields a web search answered (its answer is shown
    instead of the fixed sentence)."""
    by_id = {f.id: f for f in fs if f.program and f.topic.removeprefix("program:") not in skip}
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
        out.append(Part(s, native=False, user=f.user, base=base, keep=text_values([f]), fields=frozenset(f.fields)))
    return out


# ---- restricted translation of English program sentences ------------------------------------------
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
    fails += check_answer(Sentence(text=t, source_ids=orig.source_ids), ev, language, base, orig.text)
    return list(dict.fromkeys(fails))


EMPTY_TRANSLATION = "empty_translation"


# ---- fixed sentences when no model call can be made -----------------------------------------------------------
# Allowance refused, the request stopped (quota, config) or no time left: no translation call. A language with
# sentences written by code (zh-Hant / zh-Hans) gets them; any other language gets the English sentences, and the
# response's `language` is then "en" (it always names the language of the text delivered).
FALLBACK_LEAD = {"zh-Hant": "目前無法產生完整回答，以下是紀錄中的事實。",
                 "zh-Hans": "目前无法产生完整回答，以下是记录中的事实。",
                 "en": "A full answer could not be produced right now; these are the facts on record."}
FALLBACK_NONE = {"zh-Hant": "目前無法產生完整回答，請查看下方的原始資料連結。",
                 "zh-Hans": "目前无法产生完整回答，请查看下方的原始资料链接。",
                 "en": "A full answer could not be produced right now; the original sources linked below may help."}
_ZH_FIELD = {"year built": ("建造年份", "建造年份"), "cladding": ("外牆材質", "外墙材质"),
             "last sale": ("上次成交紀錄", "上次成交记录"), "council valuation": ("市議會估價", "市议会估价"),
             "title type": ("產權類型", "产权类型"), "annual rates": ("年度地方稅", "年度地方税"),
             "land value": ("土地價值", "土地价值"), "floor area": ("樓地板面積", "楼地板面积"),
             "land area": ("土地面積", "土地面积"), "local board": ("地方委員會", "地方委员会"),
             "building site coverage": ("建築覆蓋面積", "建筑覆盖面积")}
_ZH_EXACT = {
    facts_prompt.NO_SOURCE_TEXT["cladding"]: ("沒有免費的公開來源記錄外牆材質，因此無法確認；如果你知道，可以在表單中填寫。",
                                              "没有免费的公开来源记录外墙材质，因此无法确认；如果你知道，可以在表单中填写。"),
    facts_prompt.NO_SOURCE_TEXT["last_sale"]: ("我們沒有成交價格的授權資料來源，因此無法查核上次成交紀錄。",
                                               "我们没有成交价格的授权资料来源，因此无法查核上次成交记录。"),
}
_ZH_PATTERNS = [
    (re.compile(r"The (.+) could not be checked from public records\."), ("公開紀錄無法查核{f}。", "公开纪录无法查核{f}。")),
    (re.compile(r"Council property records could not be retrieved, so the (.+) is unknown\."),
     ("無法取得市議會的物業紀錄，因此{f}不明。", "无法取得市议会的物业纪录，因此{f}不明。")),
    (re.compile(r"Council records could not be read from our server, so the (.+) is unknown\."),
     ("本系統無法讀取市議會紀錄，因此{f}不明。", "本系统无法读取市议会纪录，因此{f}不明。")),
]


def _variant(language: str) -> int:
    return 1 if language == "zh-Hans" else 0


def zh_fixed(text: str, language: str) -> Optional[str]:
    """Code-written Chinese for an English program sentence (a field the records do not have), or None."""
    text = text.strip()
    if text in _ZH_EXACT:
        return _ZH_EXACT[text][_variant(language)]
    for rx, pair in _ZH_PATTERNS:
        m = rx.fullmatch(text)
        if m and m.group(1) in _ZH_FIELD:
            return pair[_variant(language)].format(f=_ZH_FIELD[m.group(1)][_variant(language)])
    return None


def fallback_language(language: str) -> str:
    """The language of the fixed sentences: the asker's when code has sentences in it, else English."""
    if is_zh(language):
        return "zh-Hans" if language == "zh-Hans" else "zh-Hant"
    return "en"


def fixed_answer(parts: list[Part], language: str, ev: Evidence, debug: dict,
                 web_part: Callable[[Part], bool]) -> list[Sentence]:
    """The answer when no translation call may be made. Chinese: sentences already in Chinese (checked
    rewrites, code-written comparisons) and code-written Chinese for program sentences, each checked; an English
    sentence with no Chinese version is left out. Other languages: the English sentences, no sentence in the asker's
    language (they would not match `language: "en"`). A lead sentence says the answer is limited."""
    out_lang = fallback_language(language)
    out: list[Sentence] = []
    if out_lang != "en":
        prefix = {False: PREFIXES[out_lang], True: WEB_PREFIXES[out_lang]}
        for p in parts:
            if p.native:
                s = Sentence(text=tidy(p.sentence.text, out_lang), source_ids=p.sentence.source_ids)
            else:
                zh = zh_fixed(strip_prefix(p.sentence.text), out_lang)
                s = Sentence(text=zh or "", source_ids=p.sentence.source_ids)
                if not zh or check_answer(s, ev, out_lang, p.base):
                    continue
            out.append(add_prefix(s, prefix[web_part(p)]) if p.user else s)
    else:
        english_prefix = {False: PREFIX_EN, True: WEB_PREFIX_EN}
        out = [add_prefix(p.sentence, english_prefix[web_part(p)]) if p.user else p.sentence
               for p in parts if not p.native]
    lead = (FALLBACK_LEAD if out else FALLBACK_NONE)[out_lang]
    debug["fallback"] = {"language": out_lang, "left_out": len(parts) - len(out)}
    return [Sentence(text=lead, source_ids=[]), *out]


def check_prefix(text: str, language: str, max_len: int = 40) -> bool:
    t = (text or "").strip().rstrip(" ,，、")
    return bool(t) and len(t) <= max_len and not re.search(r"\d|https?:|www\.|<|@|\.[a-z]{2,}", t, re.I) \
        and script_matches(t, language)


def finish(parts: list[Part], language: str, ev: Evidence,
           translate: Callable[..., Optional[list[str]]], debug: dict,
           web_part: Callable[[Part], bool] = lambda p: False,
           unavailable: Callable[[], bool] = lambda: False) -> list[Sentence]:
    """The language step: English parts are translated (one restricted call for all of them, plus the prefix
    for languages other than Chinese); sentences whose translation fails check_translation are sent once more
    with the words to copy; a second failure leaves the whole sentence in English. The prefix is added in the
    language the sentence ends up in. `web_part`: the user part depends on a value from a web search (web prefix).
    `unavailable()`: no model call may be made (allowance refused, request stopped, no time): fixed_answer, whose
    language is in debug["fallback"]."""
    english_prefix = {False: PREFIX_EN, True: WEB_PREFIX_EN}
    if is_english(language):
        return [add_prefix(p.sentence, english_prefix[web_part(p)]) if p.user else p.sentence for p in parts]
    english = [p for p in parts if not p.native]
    kinds = [] if is_zh(language) else sorted({web_part(p) for p in parts if p.user})
    texts = [strip_prefix(p.sentence.text) for p in english] + [english_prefix[k].strip().rstrip(",") for k in kinds]
    got = translate(texts) if texts else None
    if got is None and texts and unavailable():
        return fixed_answer(parts, language, ev, debug, web_part)
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
    if english and not done and not getattr(translate, "mock", False):
        # no usable model translation at all (call failed or timed out, checks failed, retry refused or failed): the
        # same fixed-language decision as when the first call is refused, so `language` matches the content
        return fixed_answer(parts, language, ev, debug, web_part)
    if is_zh(language):
        prefix = {False: PREFIXES.get(language, PREFIXES["zh-Hant"]),
                  True: WEB_PREFIXES.get(language, WEB_PREFIXES["zh-Hant"])}
    else:
        prefix = {}
        joiner = "、" if language.lower().startswith("ja") else ", "
        for j, k in enumerate(kinds):
            t = got[len(english) + j] if got else None
            prefix[k] = (t.strip().rstrip(" ,，、") + joiner) if t and check_prefix(t, language, 70 if k else 40) else None
        debug["prefix"] = prefix.get(False) if list(prefix) == [False] else prefix
    out = []
    for p in parts:
        s = done.get(id(p))
        in_language = p.native or s is not None
        s = s or p.sentence
        if p.native:
            s = Sentence(text=tidy(s.text, language), source_ids=s.source_ids)
        if p.user:
            k = web_part(p)
            s = add_prefix(s, prefix[k]) if (in_language and prefix.get(k)) else add_prefix(s, english_prefix[k])
        out.append(s)
    return out


# ---- web search for property fields the records do not have --------------------------------------
# year_built, cladding and last_sale, which the first-hand sources often lack, may be looked up with Google
# Search. The search answer (`web`) is shown as returned, never edited or
# mixed with other text; values code extracts from it go in user_supplied / web.extracted. Sources are not filtered
# by domain. At most 2 search calls per request, within the request deadline.
BUTTON_QUESTIONS = {"year_built": "When was this home built?",
                    "cladding": "What is this home's exterior cladding?",
                    "last_sale": "When was this home last sold, and for how much?"}
_ASKING = re.compile(r"[?？]|\b(?:when|what|which|how|is|was|were|does|did|do|has|any|tell)\b|哪|幾|几|什麼|什么|多少|多久|"
                     r"嗎|吗|是否|呢|何|いつ|ですか", re.I)
MOCK_WEB = WebAnswer(
    text=("MOCK web search answer for testing the page; no search was run. Listing pages describe this home as built "
          "in 1985 with weatherboard cladding. It last sold in March 2019 for $850,000."),
    sources=[WebSource(title="example.com", url="https://example.com/mock-web-search")], search_suggestions="")


def missing_field(req: ReportRequest, name: str) -> bool:
    """The property field is not_found/unavailable (or PropertyContext lacks it): only then is it searched for, and
    only then may a value found online fill it (council data wins over everything)."""
    f = req.property_fields().get(name)
    return f is None or f.status in facts_prompt.MISSING


def fields_to_search(question: str, req: ReportRequest, supplied: dict[str, Any]) -> list[str]:
    """Fields the question asks about (_FIELD_Q) whose status is not_found/unavailable, and that the user has not
    given: stated in this question (`supplied` holds only values the question states, see affirmed; "Was it built in
    1997?" asks) or earlier (chat_supplied). Other years in the question ("It sold in 2019; when was it built?") do
    not stop a search."""
    if not _ASKING.search(question):
        return []
    chat = req.chat_supplied()
    return [name for name in web_fields.FIELDS
            if missing_field(req, name) and name not in supplied and name not in chat and _FIELD_Q[name].search(question)]


def field_search(req: ReportRequest, fields: list[str], question: str, history: list[dict],
                 generate: JsonGenerator, clock: _Clock, mock: bool, debug: dict,
                 language: str = "") -> Optional[WebAnswer]:
    """One Google Search for the fields (a second only when the first names no source, within the request's search
    limit and time), then the values code extracts from its text (web_fields.extract; a field code found nothing
    for may be asked once of Gemini, from the text only, and is kept only when its quote passes the same checks).
    `language`: the button's page language when there is no question. Mock mode: MOCK_WEB, no call."""
    info: dict[str, Any] = {"fields": list(fields), "searches": 0}
    debug["field_search"] = info
    address = str((req.property_context.get("address") or {}).get("matched") or "")
    web: Optional[WebAnswer] = None
    if mock:
        web = MOCK_WEB.model_copy(deep=True)
    elif not address:
        return None
    else:
        prompt = fp.field_search_prompt(address, fields, req.question.strip(), history, language)
        try:
            web = _search(prompt, clock, 0.5)
            if not web:
                web = _search(prompt, clock, 0.5)
        except gemini.ModelUnavailable as e:
            log.warning("field search unavailable: %s", type(e).__name__)
            web = None
        info["searches"] = clock.searches
    info["web"] = bool(web)
    if not web:
        return None
    found, ambiguous, missing = web_fields.extract(web.text, list(fields), address)
    info.update(by_code=sorted(found), ambiguous=sorted(ambiguous))
    if missing and not mock and clock.can_call() and not protection.stopped():
        try:
            out = _call(generate, fp.extract_prompt(web.text, sorted(missing)), web_fields.ExtractOut,
                        clock.timeout(0.25))
            got = web_fields.check_model_values(out, web.text, missing, address)
            info["by_model"] = sorted(got)
            found.update(got)
        except ReportError as e:
            log.info("extraction call failed (%s)", e.code)
    info["extracted"] = found
    return web.model_copy(update={"fields": list(fields), "extracted": found})


def _sources_out(supplied: dict[str, Any], sources: dict[str, str]) -> Optional[dict[str, str]]:
    """user_supplied_source: only when a value came from a web search."""
    out = {k: sources.get(k, USER) for k in supplied}
    return out if WEB_SEARCH in out.values() else None


# ---- multi-owner titles: a search value is shown, never filled in ---------------------------------
MULTI_UNIT = "multi_unit_title"
SEARCH_UNAVAILABLE = "search_unavailable"


def multi_unit_kind(req: ReportRequest) -> Optional[str]:
    """"unit" (Unit Title), "cross" (Cross Lease) or "multi" (reason multiple_title_types: several title types at the
    address, no value) when the property has several owners, else None. Values compare like rules-api's title_type
    (whitespace-normalised, case-folded)."""
    f = req.property_fields().get("title_type")
    if f is None:
        return None
    if f.value is not None:
        return {"unit title": "unit", "cross lease": "cross"}.get(" ".join(str(f.value).split()).casefold())
    return "multi" if f.reason == "multiple_title_types" else None


_SKIP_EN = {"unit": ("This is a unit title property, so the result may describe another unit or the whole building.",
                     "your unit"),
            "cross": ("This is a cross lease property, so the result may describe another home on the same site.",
                      "your home"),
            "multi": ("This address has more than one title type, so the result may describe another title or the "
                      "whole building.", "your property")}
_SKIP_ZH = {"unit": ("這是分戶產權（Unit Title）物業，搜尋結果可能描述其他單位或整棟大樓。", "你的單位"),
            "cross": ("這是交叉租賃（Cross Lease）物業，搜尋結果可能描述同一地塊上的其他住宅。", "你的住宅"),
            "multi": ("這個地址有多種產權類型，搜尋結果可能描述其他產權或整棟大樓。", "你的物業")}


def skipped_answer(kind: str, found: dict[str, Any], language: str,
                   translate: Callable[..., Optional[list[str]]], debug: dict,
                   unavailable: Callable[[], bool] = lambda: False) -> list[Sentence]:
    """Code-written sentences for a search value that was not filled in: what was found, why it was not added, and
    that the user may enter it. With no value held back (a last-sale search: nothing is ever filled in) only the
    middle one, that the result may be about another unit. Chinese is written by code; other languages are translated
    under the usual checks (English when a check fails). Each sentence passes the validator against the found values
    only."""
    year, cladding = found.get("year_built"), found.get("cladding")
    en_bits, zh_bits, nums, terms = [], [], [], set()
    if year is not None:
        decade = bool(re.fullmatch(r"\d{4}s", str(year)))
        en_bits.append(f"the year built found online is {'the ' if decade else ''}{year}")
        zh_bits.append(f"建造年份是 {str(year)[:4]} 年代" if decade else f"建造年份是 {year} 年")
        nums += _year_numbers(year)
    if cladding is not None:
        label = cladding_table.label(str(cladding))
        en_bits.append(f"the cladding found online is {label}")
        zh_bits.append(f"外牆材質是 {label}")
        terms.add(label)
    ev = Evidence(known_ids={"web_search"}, allowed={"web_search": [Allowed(Decimal(n)) for n in nums]},
                  latin_terms={*terms, "Unit Title", "Cross Lease"})
    base: BaseCheck = lambda s, mw: check_sentence(Sentence(text=s.text, source_ids=["web_search"]), ev, mw)  # noqa: E731
    reason_en, mine = _SKIP_EN[kind]
    en = [(" and ".join(en_bits)).capitalize() + ".", reason_en,
          f"The result has not been added to the form; you can enter it yourself if it matches {mine}."]
    reason_zh, mine_zh = _SKIP_ZH[kind]
    zh = ["網路搜尋找到的" + "，".join(zh_bits) + "。", reason_zh, f"搜尋結果尚未填入表單；若與{mine_zh}相符，可自行輸入。"]
    parts = []
    for n, text in enumerate(en):
        if not found and n != 1:
            continue
        s = Sentence(text=text, source_ids=[])
        if check_answer(s, ev, "en", base):
            continue  # not expected; a sentence that fails its own check is left out
        native = None
        if is_zh(language):
            z = Sentence(text=tidy(_zh(zh[n], language), language), source_ids=[])
            if not check_answer(z, ev, language, base, text):
                native = z
        parts.append(Part(native or s, native=native is not None, user=n == 0, base=base,
                          keep=tuple(terms), web=True if n == 0 else None))
    return finish(parts, language, ev, translate, debug, lambda p: True, unavailable)


# ---- a loan question the report has no loan facts for ------------------------------------------------------
# The model is told to say what the facts do show when none answers; with no loan, deposit or income entered the facts
# are about hazards and the title, so a First Home Loan question got an unrelated sentence. Code decides instead: a
# question about the loan or the First Home Loan, no fact about it in the list, and no other topic in the question
# -> a fixed answer that says so and names what is missing. No model call.
_FHL_Q = re.compile(r"first[- ]home[- ](?:loan|buyer|scheme)|\bFHL\b|k[aā]inga ora|首購貸款|首购贷款|首購房貸|首次購屋貸款|首次置業貸款|"
                    r"ファーストホームローン|첫 ?주택 ?대출", re.I)
_LOAN_Q = re.compile(r"\bloans?\b|mortgage|borrow|repayments?|\bLVR\b|\bDTI\b|deposit|interest rate|lender|lending|"
                     r"貸款|贷款|房貸|房贷|還款|还款|月供|頭期款|头期款|首付|利率|借多少|ローン|返済|頭金|대출|상환", re.I)
_LOAN_TOPICS = frozenset({"lending", "lending_debts", "first_home_loan", "user_input", "kb:lending"})
INSUFFICIENT_EN = "This page does not have enough data to answer this question."
INSUFFICIENT_ZH = ("這裡沒有足夠的資料可以回答這個問題。", "这里没有足够的资料可以回答这个问题。")
_NEED = {"price": ("the price", "价格", "價格"), "deposit": ("the deposit", "头期款", "頭期款"),
         "income": ("the household income", "家庭收入", "家庭收入")}


def loan_topic(question: str) -> Optional[str]:
    """"fhl" or "loan" when the question is about the First Home Loan or the loan and about nothing else the report
    has facts for (hazards, property fields); else None."""
    if _FLOOD_Q.search(question) or _HAZARD_Q.search(question) or any(rx.search(question) for rx in _FIELD_Q.values()):
        return None
    if _FHL_Q.search(question):
        return "fhl"
    return "loan" if _LOAN_Q.search(question) else None


def loan_facts(fs: list, kind: str) -> list:
    """The facts that can answer a question of this kind: any fact about the loan, or for "fhl" one about the
    First Home Loan (its own program fact, or one that cites a First Home Loan source)."""
    fhl = [f for f in fs if f.topic == "first_home_loan" or any("fhl" in i or "kainga" in i for i in f.source_ids)]
    return fhl if kind == "fhl" else [f for f in fs if f.topic in _LOAN_TOPICS] + fhl


def missing_loan_inputs(req: ReportRequest) -> list[str]:
    """Which of price, deposit and household income the user has not given (keys of _NEED), in that order."""
    u = req.user_inputs
    return [k for k, gone in (("price", "price_missing" in req.warnings()), ("deposit", u.get("deposit") is None),
                              ("income", u.get("household_income") is None)) if gone]


def insufficient_texts(req: ReportRequest, kind: Optional[str]) -> tuple[list[str], list[str], list[str]]:
    """The fixed answer in English, Traditional and Simplified Chinese: that the report cannot answer, then what
    is missing (the loan details not entered, a deposit that covers the price, or no result for these inputs).
    `kind` None (the question is not about the loan: no fact answers it): the first sentence alone."""
    if kind is None:
        return [INSUFFICIENT_EN], [INSUFFICIENT_ZH[0]], [INSUFFICIENT_ZH[1]]
    gone = missing_loan_inputs(req)
    cash = any(w in req.warnings() for w in ("cash_purchase_no_loan", "deposit_exceeds_price"))
    what_en, what_t, what_s = ("First Home Loan", "First Home Loan", "First Home Loan") if kind == "fhl" else \
        ("the loan", "貸款", "贷款")
    if cash:
        en = f"It cannot assess {what_en} because the deposit covers the whole price, so there is no loan."
        zh = (f"無法評估{what_t}，因為頭期款已涵蓋全部價格，沒有貸款。", f"无法评估{what_s}，因为头期款已涵盖全部价格，没有贷款。")
    elif gone:
        names = [_NEED[k] for k in gone]
        joined = _join_words([n[0] for n in names])
        en = f"It cannot assess {what_en} because {joined} {'was' if len(names) == 1 else 'were'} not entered in the form."
        zh = (f"無法評估{what_t}，因為表單中沒有填寫{'、'.join(n[2] for n in names)}。",
              f"无法评估{what_s}，因为表单中没有填写{'、'.join(n[1] for n in names)}。")
    else:
        en = f"There is no {what_en} result for the details entered."
        zh = (f"目前填寫的資料沒有得出{what_t}結果。", f"目前填写的资料没有得出{what_s}结果。")
    return [INSUFFICIENT_EN, en], [INSUFFICIENT_ZH[0], zh[0]], [INSUFFICIENT_ZH[1], zh[1]]


def _join_words(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# ---- main flow ----------------------------------------------------------------------------------
def answer_followup(req: FollowupRequest, refusal_mode: str = "exempt",
                    generate: Optional[JsonGenerator] = None, debug: Optional[dict] = None,
                    web_search: bool = False) -> FollowupResponse:
    """The follow-up answer (_answer_followup), with the warning generation_degraded when protection refused a
    model call (allowance used up, REPORT_DEGRADED, prompt too large): the answer then comes from the program's
    facts or fixed sentences, without search, extracted values or translation. A refused Search alone (daily Search
    allowance) adds no warning: the rest of the answer is made as usual."""
    with protection.request_scope() as scope:
        resp = _answer_followup(req, refusal_mode, generate, debug, web_search)
    if scope.degraded and protection.GENERATION_DEGRADED not in resp.warnings:
        resp = resp.model_copy(update={"warnings": [*resp.warnings, protection.GENERATION_DEGRADED]})
    return resp


def _answer_followup(req: FollowupRequest, refusal_mode: str = "exempt",
                     generate: Optional[JsonGenerator] = None, debug: Optional[dict] = None,
                     web_search: bool = False) -> FollowupResponse:
    """`debug`, when given, receives the fact list, the checked attempts and the composed answer (for eval).
    `web_search` lets an out-of-scope question, or a property field the records do not have (`search_field`, or a
    question about it), be answered from a Google Search. The route turns it on; eval runs leave it off, because the
    Gemini API terms do not allow storing such answers."""
    generate = generate or gemini_json
    debug = debug if debug is not None else {}
    mock = mock_enabled()
    clock = _Clock()
    history = [t.model_dump() for t in req.history]
    link_ids = [link.id for link in req.links()]
    warnings: list[str] = []
    classify_failed = stopped = False  # stopped: quota or no key, no further Gemini call (gemini.NO_RETRY)
    no_call = {"translate": False}  # a translation was needed but no model call could be made

    def halted() -> bool:
        return stopped or protection.stopped()

    def translate(texts: list[str], copy: Optional[list[str]] = None) -> Optional[list[str]]:
        """Sees only the fixed sentences, the language code and (on the retry) the words to copy, which come from
        those sentences (fp.translate_prompt)."""
        if mock:
            return None  # mock mode does not translate; finish keeps the English parts (translate.mock)
        if halted() or not clock.can_call():
            if not halted():
                obs.note_degraded(obs.DEADLINE)
            no_call["translate"] = True
            return None
        try:
            return _call(generate, fp.translate_prompt(texts, language, copy), TranslateOut,
                         clock.timeout(0.5)).texts
        except ReportError as e:
            log.info("translation failed (%s); using English", e.code)
            if e.category in gemini.NO_RETRY:
                no_call["translate"] = True
            return None

    def unavailable() -> bool:
        return no_call["translate"]

    translate.mock = mock  # type: ignore[attr-defined]

    # 1. classify (web's search button skips it: the question is optional and only gives the language)
    button = req.search_field
    question = req.question.strip() or BUTTON_QUESTIONS.get(button or "", "")
    if button:
        # a question sent with the button gives the language as before; without one, the page's language
        lang = normalize_language("en", question) if req.question.strip() else page_language(req.language)
        c = ClassifyOut(language=lang)
    elif mock:
        c = mock_classify(question)
    elif clock.can_call():
        try:
            c = _call(generate, fp.classify_prompt(question, history, [{"id": link.id, "label": link.label}
                                                                        for link in req.links()]),
                      ClassifyOut, clock.timeout(0.4))
        except ReportError as e:  # answered from the program's facts; no extracted values, no search
            log.warning("classify failed (%s); answer from the facts, no values, no search", e.code)
            c, classify_failed = ClassifyOut(language=normalize_language("en", question)), True
            stopped = e.category in gemini.NO_RETRY
    else:  # no time for classify: the language by script, no values, no search; program drafts answer
        obs.note_degraded(obs.DEADLINE)
        c = ClassifyOut(language=normalize_language("en", question))
    language = c.language if button else normalize_language(c.language, question)
    debug["language"] = {"claimed": c.language, "used": language}
    supplied = {} if button else extract_user_supplied(question, c)
    for k in [k for k, v in supplied.items() if not affirmed(question, k, v)]:
        log.info("user_supplied_ignored: %s is asked about, not stated", k)
        supplied.pop(k)
    sources = {k: USER for k in supplied}
    links = [i for i in dict.fromkeys(c.links) if i in link_ids][:3]

    def no_search() -> None:
        """A search was called for and gave no answer (refused, failed, quota 429, no key or other setting error, out
        of time, no source): the page can say so. Not when the request is degraded by the allowance:
        generation_degraded says that."""
        if not protection.degraded() and SEARCH_UNAVAILABLE not in warnings:
            warnings.append(SEARCH_UNAVAILABLE)

    def searched(out_of_scope: bool) -> Optional[FollowupResponse]:
        """The whole reply from a Google Search (fp.web_prompt): for a question the report does not cover, or one no
        fact answers. None when search is off (eval, strict or mock mode) or gave no answer; the latter is named in
        warnings.
        The model does not always search: asked once more, time allowing, when the answer names no source. Neither
        call gets all the time that is left: web, the caller, has its own limit for the whole request. The search limit
        is shared with the field search: at most gemini.MAX_SEARCHES calls per request."""
        address = str((req.property_context.get("address") or {}).get("matched") or "")
        if mock or not (web_search and refusal_mode == "exempt" and address):
            return None
        prompt = fp.web_prompt(address, question, history)
        try:
            web = _search(prompt, clock, 0.6) or _search(prompt, clock, 0.6)
        except gemini.ModelUnavailable as e:
            log.warning("web answer unavailable: %s", type(e).__name__)
            web = None
        if not web:
            no_search()
            return None
        return FollowupResponse(language=language, answer=[], out_of_scope=out_of_scope, user_supplied=supplied,
                                links=links, dropped=0, warnings=warnings, web=web)

    def insufficient_answer(eff: ReportRequest, kind: Optional[str]) -> Optional[tuple[list[Sentence], str]]:
        """The fixed "not enough data" answer in the asker's language (English or code-written Chinese; another
        language by restricted translation, else English with language "en"), or None if a sentence fails its check."""
        en, zh_t, zh_s = insufficient_texts(eff, kind)
        probe = Evidence()
        no_ids = lambda p, mw: [f for f in check_sentence(p, probe, mw) if f != "no_source_ids"]  # noqa: E731
        sents = lambda texts, lang: [Sentence(text=tidy(t, lang), source_ids=[]) for t in texts]  # noqa: E731
        if is_zh(language):
            out, lang = sents(zh_s if language == "zh-Hans" else zh_t, language), language
        else:
            out, lang = sents(en, "en"), "en"
            if not is_english(language):
                got = translate(en)
                if got and len(got) == len(en) and not any(
                        check_translation(o, t, language, probe, no_ids) for o, t in zip(out, got)):
                    out, lang = sents(got, language), language
                elif mock:
                    lang = language  # mock mode keeps the English parts (translate.mock)
        if any(check_answer(o, probe, lang, no_ids) for o in out):
            log.warning("insufficient-data answer failed its own check")
            return None
        return out, lang

    def in_scope(question: str, supplied: dict[str, Any], sources: dict[str, str],
                 skip: frozenset = frozenset(), after_search: bool = False) -> FollowupResponse:
        """Steps 3-4 for an in-scope question. `sources`: "web_search" for values extracted from a search answer;
        `skip`: the fields a web search answered (their fixed sentences are left out). When the model finds no fact
        that answers the question, the reply is a Google Search answer, else the fixed "cannot answer" sentence;
        `after_search` (a field search answer is already there): ReportError instead, and that answer stands alone."""
        nonlocal stopped
        # 3. merge user_supplied; property values with status "found" win; re-run rules when something new is used
        pf = req.property_fields()
        chat = dict(req.chat_supplied())
        notes: list[str] = []
        used: dict[str, Any] = {}
        compared: list[tuple[str, Any, FieldView]] = []
        for key, value in supplied.items():
            if key not in web_fields.FILLABLE:
                continue
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
        src = {**{k: USER for k in chat}, **facts_prompt.chat_sources(req), **{k: sources.get(k, USER) for k in used}}
        if WEB_SEARCH in src.values():  # rules-api ignores this key (UserInputs: extra="ignore")
            user_inputs["chat_supplied_source"] = {k: src[k] for k in {**chat, **used}}
        if used and any(chat.get(k) != v for k, v in used.items()):
            chat.update(used)
            user_inputs["chat_supplied"] = chat
            try:
                rules_output = call_rules(req.property_context, user_inputs, min(5.0, max(0.1, clock.left() * 0.3)))
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
        # A value found online is stated like a value entered earlier: by its program fact ("The year built found
        # online is 1987; ..."), not by the user_said fact of a value stated in this very question.
        said = {k: v for k, v in used.items() if sources.get(k) != WEB_SEARCH}
        if used and not said:
            allow_user_year(ev, used)
            notes.append(f"A web search just found the {', '.join(k.replace('_', ' ') for k in used)} (unverified). "
                         "The facts with \"user\": true depend on it.")
        elif used:
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
            web = sources.get(key) == WEB_SEARCH
            s = comparison_sentence(key, value, f, language, web=web)
            native = is_english(language) or is_zh(language)
            fails = check_comparison(s, value, ev, language if native else "en") if s else ["no_council_value"]
            if fails:
                log.warning("comparison sentence dropped: %s", ",".join(fails))
            else:
                # a comparison with a value found online names the web search in its prefix too
                fixed.append(Part(s, native=native, user=web, base=comparison_check(value, ev),
                                  keep=() if key == "year_built" else (cladding_table.label(str(value)), str(f.value)),
                                  fields=frozenset({key}), web=web))

        # 4. answer from the fact list; check each rewrite; one retry when none passes and time allows; blocked
        #    sentences fall back to their facts' drafts
        passed: list[Part] = []
        dropped, out_links = 0, []
        last: Optional[ReportError] = None
        limit = max(0, MAX_ANSWER - len(fixed))
        all_facts = facts_prompt.followup_facts(eff, ev, data.items, said)
        kind = loan_topic(question)
        if kind and not fixed and not used and not loan_facts(all_facts, kind):
            # a search first (what a First Home Loan is, how a loan works); the fixed sentences when it
            # gives nothing
            online = None if after_search else searched(False)
            if online:
                return online
            got = insufficient_answer(eff, kind)
            if got:
                debug["insufficient"] = kind
                return FollowupResponse(language=got[1], answer=got[0], out_of_scope=False, user_supplied=supplied,
                                        user_supplied_source=_sources_out(supplied, sources), links=links,
                                        dropped=0, warnings=warnings)
        if mock:
            sentences, dropped = mock_answer(ev, data.items, used, language)
            if fixed and not used:
                sentences = []  # the comparison is the answer; no filler sentence
            passed = [Part(s, native=is_english(language) or script_matches(s.text, language),
                           user=bool(set(s.source_ids) & ev.user_kb)) for s in sentences[:limit]]
        else:
            # a field a search answered: neither its program sentence nor "could not be checked" is said beside it
            fs = [f for f in all_facts
                  if not ((f.program or f.topic.startswith("missing:")) and f.topic.split(":", 1)[-1] in skip)]
            ev.latin_terms |= latin_terms(eff, fs)
            for f in fs:
                for sid in f.source_ids:
                    ev.fact_text[sid] = f"{ev.fact_text.get(sid, '')} {f.text}"
            debug["facts"] = [{"id": f.id, "text": f.text, "draft": f.draft(), "user": f.user,
                               "source_ids": f.source_ids} for f in fs]
            debug["attempts"] = []

            def final(s: Sentence) -> list[str]:
                return check_answer(Sentence(text=tidy(s.text, language), source_ids=s.source_ids), ev, language)

            prog_ids = {f.id for f in fs if f.program}
            base = fp.answer_prompt(eff, ev, data.items, titles, question, history, notes, fs, language)
            prompt = base
            best: Optional[list] = None
            named: list[str] = []
            out_of_time = False  # no time for a (further) call: the program's drafts answer
            for attempt in range(2):
                if halted():
                    break
                if not clock.can_call():
                    out_of_time = True
                    obs.note_degraded(obs.DEADLINE)
                    break
                try:
                    out = _call(generate, prompt, AnswerOut, clock.timeout())
                except ReportError as e:
                    last = e
                    prompt = base
                    if e.category in gemini.NO_RETRY:
                        stopped = True
                        break
                    continue
                if not out.answered and not fixed and not used:
                    # no fact answers it (a definition, a topic the facts do not hold): never facts about something else
                    if after_search:
                        raise ReportError(502, "validation_failed", "No fact answers the question.")
                    got = searched(False)
                    if got:
                        return got
                    sure = insufficient_answer(eff, None)
                    if sure:
                        debug["insufficient"] = "unanswered"
                        return FollowupResponse(language=sure[1], answer=sure[0], out_of_scope=False,
                                                user_supplied=supplied, links=links or fallback_links(question, link_ids),
                                                dropped=0, warnings=warnings)
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
            if best is None and last is not None and not fixed and not protection.degraded():
                # every answer call failed (5xx, timeout, quota, no key, unusable output): the must-mention drafts
                # answer a different question. Say that no answer could be made. A call the allowance refused keeps
                # the program's facts as the answer (generation_degraded is added by answer_followup).
                sure = insufficient_answer(eff, None)
                if sure:
                    debug["insufficient"] = "model_failed"
                    if protection.GENERATION_DEGRADED not in warnings:
                        warnings.append(protection.GENERATION_DEGRADED)
                    return FollowupResponse(language=sure[1], answer=sure[0], out_of_scope=False,
                                            user_supplied=supplied, user_supplied_source=_sources_out(supplied, sources),
                                            links=links or fallback_links(question, link_ids), dropped=0,
                                            warnings=warnings)
            prog = program_sentences(fs, named, question, ev, skip)[:limit]
            debug["program"] = [p.sentence.text for p in prog]
            passed.extend(prog)
            limit -= len(prog)
            # no model answer (out of time, or every call failed) and no fixed sentence: the must-mention facts'
            # drafts
            musts = best is None and not prog and not fixed
            # A draft is the program's own sentence or a verified claim as written: the sentence limit, which keeps the
            # model's rewrites short, does not hold it back (six claims are longer than MAX_WORDS, and a question about
            # one of them had no answer when its rewrite was blocked).
            items = rewrite.compose(best or [], fs, limit, add_musts=musts, sort=False,
                                    check_draft=lambda s: [f for f in check_answer(s, ev, "en") if f != TOO_LONG]
                                    ) if limit else []
            dropped = sum(r.sentence is None for r in best or [])
            debug["composed"] = [{"text": it.sentence.text, "fact_ids": it.fact_ids, "kind": it.kind} for it in items]
            debug["rewrite"] = rewrite.stats(items, best or [])
            fmap = {f.id: f for f in fs}
            for it in items:  # a rewrite is in the answer language; a draft is English
                passed.append(Part(it.sentence, native=it.kind == rewrite.REWRITE,
                                   user=bool(it.user or set(it.sentence.source_ids) & ev.user_kb),
                                   keep=text_values([fmap[i] for i in it.fact_ids if i in fmap]), fields=it.fields))
        def online() -> Optional[FollowupResponse]:
            """The facts gave no sentence that can be shown (every rewrite and draft blocked, or none that could be put
            into the asker's language): a search answer, when one may be made, before an error or an empty reply."""
            return None if after_search or used or fixed else searched(False)

        if not passed and not fixed:
            got = online()
            if got:
                return got
            if last is None and not clock.can_call():
                raise ReportError(504, "deadline_exceeded", "The request ran out of time.")
            raise last or ReportError(502, "validation_failed", "No answer sentence passed validation.")
        answer = finish(fixed + passed, language, ev, translate, debug,
                        lambda p: p.web if p.web is not None else
                        facts_prompt.from_web(p.sentence.source_ids, eff, ev, data.items, p.fields), unavailable)
        out_links = [i for i in dict.fromkeys(out_links + links) if i in link_ids][:3]
        fallback = debug.get("fallback")
        if fallback and len(answer) == 1:  # the lead sentence alone: nothing could be said in the asker's language
            got = online()
            if got:
                return got
        if fallback and not out_links:
            out_links = fallback_links(question, link_ids)  # the fixed lead sentence points to the links
        return FollowupResponse(language=fallback["language"] if fallback else language, answer=answer,
                                out_of_scope=False, user_supplied=supplied,
                                user_supplied_source=_sources_out(supplied, sources),
                                links=out_links, dropped=dropped, warnings=warnings)

    # 1b. property fields the records do not have: a Google Search answer shown as
    #     returned; the year built and cladding code extracts from it fill user_supplied ("web_search") and re-run
    #     the rules; the answer's other sentences come from the usual steps. No search, or no source: the usual
    #     steps (fixed program sentences and links).
    # The button searches only a field the records do not have; a found field is answered from the records.
    if button:
        fields = [button] if missing_field(req, button) else []
    else:
        fields = fields_to_search(question, req, supplied)
    if fields and web_search and refusal_mode == "exempt" and not classify_failed:
        web = field_search(req, fields, question, history, generate, clock, mock, debug,
                           language if button and not req.question.strip() else "")
        if not web:
            no_search()
        if web:
            chat = req.chat_supplied()  # values only add: a value the user already gave is never replaced
            kind = multi_unit_kind(req)
            held: dict[str, Any] = {}
            for k in web_fields.FILLABLE:  # council data > the user's values > search values
                if k in web.extracted and k not in supplied and k not in chat and missing_field(req, k):
                    if kind:  # several owners: the value may be another unit's or the whole building's
                        held[k] = web.extracted[k]
                    else:
                        supplied[k], sources[k] = web.extracted[k], WEB_SEARCH
            # shown, not filled in: no user_supplied value, no rules-api call. A sale found online is never filled in,
            # but it too may be another unit's: the button's reply says so (a typed question is answered as usual).
            if held or (kind and button == "last_sale"):
                if held:
                    web = web.model_copy(update={"autofill_skipped": MULTI_UNIT})
                answer = skipped_answer(kind, held, language, translate, debug, unavailable)
                fallback = debug.get("fallback")
                return FollowupResponse(language=fallback["language"] if fallback else language, answer=answer,
                                        out_of_scope=False, user_supplied=supplied,
                                        user_supplied_source=_sources_out(supplied, sources),
                                        links=links or fallback_links(question, link_ids), dropped=0,
                                        warnings=warnings, web=web)
            only_web = FollowupResponse(language=language, answer=[], out_of_scope=False, user_supplied=supplied,
                                        user_supplied_source=_sources_out(supplied, sources),
                                        links=links or fallback_links(question, link_ids), dropped=0,
                                        warnings=warnings, web=web)
            # the button with nothing to fill in: the search answer is the whole reply
            if (button and not set(supplied) & set(web_fields.FILLABLE)) or not clock.can_call():
                return only_web
            try:
                # the fixed sentence of a searched field is left out unless a value found online fills it
                out = in_scope(question, supplied, sources,
                               frozenset(f for f in fields if sources.get(f) != WEB_SEARCH), after_search=True)
            except ReportError as e:  # the search answer still stands
                log.info("answer after the field search failed (%s); search answer only", e.code)
                return only_web
            return out.model_copy(update={"web": web})

    # 2. out of scope: an answer from a Google Search when web pages can answer it, else the fixed refusal
    if c.out_of_scope:
        got = searched(True) if c.searchable else None
        if got:
            return got
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
                if unavailable():
                    language = "en"  # no model call could be made: the English refusal, said to be English
        links = links or fallback_links(question, link_ids)
        if refusal_mode == "strict":
            data = _kb(req, clock)
            ev = build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
            if check_answer(refusal, ev, refusal_lang):
                raise ReportError(502, "validation_failed", "No answer sentence passed validation.")
        return FollowupResponse(language=language, answer=[refusal], out_of_scope=True, user_supplied=supplied,
                                links=links, dropped=0, warnings=warnings)

    return in_scope(question, supplied, sources)




def allow_user_year(ev: Evidence, used: dict[str, Any]) -> None:
    """The one exception for a user's value: the validated, used year is allowed only with the items it selected."""
    years = [Decimal(y) for y in _year_numbers(used.get("year_built"))]
    for i in ev.user_kb:
        ev.allowed.setdefault(i, []).extend(Allowed(y) for y in years)


def _kb(req: ReportRequest, clock: _Clock):
    try:
        return kb.load(req.selected_kb_ids(), timeout=min(5.0, max(0.1, clock.left())))
    except kb.KbUnavailable:
        raise ReportError(503, "kb_unavailable", "Knowledge-base items or sources could not be read.")
