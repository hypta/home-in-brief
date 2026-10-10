"""POST /v1/followup: answer one question in the asker's language, validated like the summary (INTERFACE_DRAFT 5, 6.1).

Flow: classify (Gemini: language, out-of-scope, stated year/cladding) -> code checks user_supplied ->
optional rules-api re-run with the merged chat_supplied -> answer (Gemini) -> validator + language checks ->
code-added prefix. Out of scope: a fixed refusal chosen by code, translated by Gemini.

question and history are untrusted: they never enter the evidence. The one exception (P-V3) is a validated
year_built that was used for the re-run; it is allowed only in sentences citing kb_items_from_user_input.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Literal, Optional

import requests
from pydantic import BaseModel, Field, ValidationError

from b_common.report_schemas import ReportRequest, Sentence

from . import gemini, kb
from . import followup_prompt as fp
from .service import ReportError, _env_float
from .validator import (MAX_WORDS, NUMBER_WORD, TOO_LONG, BANNED_WORD, URL_OR_HTML, Allowed, Evidence,
                        build_evidence, strip_prefix, validate)

log = logging.getLogger("report-api.followup")
MAX_ANSWER = 3

REFUSAL_EN = ("This question is outside what this property report covers; the original sources linked below "
              "may help you look it up.")
PREFIXES = {"zh-hant": "依你提供的資訊，", "zh-tw": "依你提供的資訊，", "zh-hk": "依你提供的資訊，", "zh": "依你提供的資訊，",
            "zh-hans": "根据你提供的信息，", "zh-cn": "根据你提供的信息，", "en": "Based on what you told us, "}
PREFIX_EN = PREFIXES["en"]


# ---- request / response -------------------------------------------------------------------------
class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=2000)


class FollowupRequest(ReportRequest):
    question: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=6)


class FollowupResponse(BaseModel):
    language: str
    answer: list[Sentence]
    out_of_scope: bool
    user_supplied: dict[str, Any]
    links: list[str]
    dropped: int


# ---- Gemini (own wrapper: gemini.gemini_generate is fixed to the summary schema) ---------------
class ClassifyOut(BaseModel):
    language: str = "en"
    out_of_scope: bool = False
    year_built: Optional[str] = None
    cladding: Optional[Literal["monolithic", "weatherboard", "brick", "concrete", "other"]] = None
    hypothetical: bool = False
    links: list[str] = Field(default_factory=list)


class AnswerOut(BaseModel):
    answer: list[Sentence]
    links: list[str] = Field(default_factory=list)


class TranslateOut(BaseModel):
    text: str


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


# ---- language -----------------------------------------------------------------------------------
_HAN_RE = re.compile(r"[㐀-鿿]")
_KANA_RE = re.compile(r"[぀-ヿ]")
_HANGUL_RE = re.compile(r"[가-힯]")


def normalize_language(claimed: str, question: str) -> str:
    """Gemini's code, corrected by script: a question with Han characters is never "en"."""
    lang = (claimed or "").strip() or "en"
    if _HANGUL_RE.search(question):
        return lang if lang.lower().startswith("ko") else "ko"
    if _KANA_RE.search(question):
        return lang if lang.lower().startswith("ja") else "ja"
    if _HAN_RE.search(question):
        return lang if lang.lower().startswith(("zh", "ja")) else "zh-Hant"
    if lang.lower().startswith(("zh", "ja", "ko")):
        return "en"  # no CJK script in the question
    return lang


def is_cjk(language: str) -> bool:
    return language.lower().startswith(("zh", "ja", "ko"))


# ---- user_supplied (5.3) ------------------------------------------------------------------------
_CONDITIONAL_RE = re.compile(r"\b(?:if|suppose|assuming|whether|not|isn't|wasn't|isnt|wasnt)\b|如果|假如|假設|若是|"
                             r"要是|萬一|不是|並非", re.I)
_CLADDING_WORDS = {
    "monolithic": r"monolithic|plaster|stucco|eifs|texture[- ]coated|灰泥|整體式|單體|monolith",
    "weatherboard": r"weatherboard|clapboard|木板|護牆板|木外牆",
    "brick": r"brick|磚",
    "concrete": r"concrete|混凝土|水泥",
    "other": r"cladding|外牆|外墙",
}


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
        words = _CLADDING_WORDS[c.cladding]
        if re.search(words, question, re.I) or (c.cladding == "other" and re.search(_CLADDING_WORDS["other"], question)):
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


# ---- validation (validator.validate plus language-dependent rules) ------------------------------
_CJK_NUM_SPACE = [(re.compile(r"(\d)(?=[　-〿぀-ヿ㐀-鿿가-힯])"), r"\1 "),
                  (re.compile(r"(?<=[぀-ヿ㐀-鿿가-힯])(?=[$\d])"), " ")]
_ZH_BANNED = re.compile(r"安全|危險|無風險|沒有風險|没有风险|低風險|高風險|风险低|风险高|風險低|風險高|建議|建议|推薦|推荐|應該|应该|應當|"
                        r"必須|必须|務必|务必|一定要|將會|将会|一定會|可能|大約|大约|大概|左右|將近|将近|幾乎|几乎|保證|保证|划算|"
                        r"值得|便宜|別買|不要買|該買|可以買|擔心|担心|放心|沒有災害|沒有淹水|不會淹水")
_ZH_NUMBER = re.compile(r"[兩两]|百分之")
# English verdict words glued to CJK text, where \b in the validator does not fire ("很safe").
_EN_GLUED = re.compile(r"(?<![A-Za-z])(?:safe|unsafe|risk-free|no risk|recommend\w*|should|must|guarantee\w*|"
                       r"will|likely|unlikely)(?![A-Za-z])", re.I)


def tidy(text: str) -> str:
    """Space between digits and CJK characters, so "1997年" reads as the number 1997 (no change in meaning)."""
    for rx, rep in _CJK_NUM_SPACE:
        text = rx.sub(rep, text)
    return text.strip()


def cjk_units(text: str) -> int:
    return len(_HAN_RE.findall(text)) + len(_KANA_RE.findall(text)) + len(_HANGUL_RE.findall(text)) + \
        len(re.findall(r"[A-Za-z0-9$][A-Za-z0-9$.,%'’-]*", text))


def check_answer(s: Sentence, ev: Evidence, cjk: bool) -> list[str]:
    """validator.check_sentence rules, plus: CJK length in characters, Chinese verdict and number words.
    The CJK rules apply when the language is CJK or the sentence contains CJK script.

    For CJK text the validator's word limit (whitespace split) is replaced by CJK_MAX_UNITS, and "一" is
    masked before the validator's numeral check, because it is part of ordinary words (一些, 之一, 一樣).
    In every language the publisher names Settled.govt.nz and unittitles.govt.nz are masked before the
    validator's URL check, which accepts them only before a few English verbs ("Settled.govt.nz says"), so
    "Settled.govt.nz 指出" or "unittitles.govt.nz states" are not dropped. Other domains still fail.
    """
    cjk = cjk or bool(_HAN_RE.search(s.text) or _KANA_RE.search(s.text) or _HANGUL_RE.search(s.text))
    if not cjk:
        v = validate([Sentence(text=_PUBLISHER_RE.sub("Settled", s.text), source_ids=s.source_ids)], ev, MAX_WORDS)
        return v.rejected[0][1] if v.rejected else []
    probe_text = _PUBLISHER_RE.sub("Settled", s.text.replace("一", "某"))
    probe = Sentence(text=probe_text, source_ids=s.source_ids)
    v = validate([probe], ev, max_words=None)
    fails = list(v.rejected[0][1]) if v.rejected else []
    if cjk_units(s.text) > fp.CJK_MAX_UNITS:
        fails.append(TOO_LONG)
    if _ZH_BANNED.search(s.text) or _EN_GLUED.search(s.text):
        fails.append(BANNED_WORD)
    if _ZH_NUMBER.search(s.text):
        fails.append(NUMBER_WORD)
    if re.search(r"https?|www|\.(?:com|nz|org|net)\b", s.text, re.I) and URL_OR_HTML not in fails \
            and not _PUBLISHER_RE.search(s.text):
        fails.append(URL_OR_HTML)
    return list(dict.fromkeys(fails))


def prefix_for(language: str, translate: Callable[[str], Optional[str]]) -> str:
    lang = language.lower()
    if lang in PREFIXES:
        return PREFIXES[lang]
    if lang.startswith("zh"):
        return PREFIXES["zh-hans"] if ("hans" in lang or lang.endswith(("cn", "sg"))) else PREFIXES["zh-hant"]
    if lang.startswith("en"):
        return PREFIX_EN
    t = translate(PREFIX_EN.strip().rstrip(","))
    return (t.rstrip(" ,，、") + ", ") if t else PREFIX_EN


_PROPER_START = re.compile(r"^(?:\S*\.\S|[A-Z]\S*[A-Z]|Auckland\b|Kāinga\b|Kainga\b|Settled\b)")


def add_prefix(s: Sentence, prefix: str) -> Sentence:
    """Code-added prefix. English: lower-case the first letter unless it starts a name ("Settled.govt.nz", "LINZ")."""
    body = strip_prefix(s.text).strip()
    if prefix == PREFIX_EN:
        if body and not _PROPER_START.match(body):
            body = body[:1].lower() + body[1:]
        return Sentence(text=PREFIX_EN + body, source_ids=s.source_ids)
    return Sentence(text=prefix + body, source_ids=s.source_ids)


CONFLICT_TEXT = {
    "year_built": {"en": "The council records give a different year built from the one you gave, and this answer uses "
                         "the council figure.",
                   "zh-hant": "市議會的資料和你提供的建造年份不同，這裡採用市議會的資料。",
                   "zh-hans": "市议会的资料和你提供的建造年份不同，这里采用市议会的资料。"},
    "cladding": {"en": "The council records give a different cladding from the one you gave, and this answer uses the "
                       "council record.",
                 "zh-hant": "市議會的資料和你提供的外牆不同，這裡採用市議會的資料。",
                 "zh-hans": "市议会的资料和你提供的外墙不同，这里采用市议会的资料。"},
}


def conflict_sentence(key: str, source_id: str, language: str, translate: Callable[[str], Optional[str]]) -> Sentence:
    table = CONFLICT_TEXT[key]
    lang = language.lower()
    if lang.startswith("en"):
        text = table["en"]
    elif lang.startswith("zh"):
        text = table["zh-hans"] if ("hans" in lang or lang.endswith(("cn", "sg"))) else table["zh-hant"]
    else:
        text = translate(table["en"]) or table["en"]
    return Sentence(text=text, source_ids=[source_id])


_PUBLISHER_RE = re.compile(r"\b(?:settled|unittitles)\.govt\.nz\b", re.I)
_PLAIN_RE = re.compile(r"\d|https?://|www\.|<|\n")


# ---- main flow ----------------------------------------------------------------------------------
def answer_followup(req: FollowupRequest, refusal_mode: str = "exempt",
                    generate: Optional[JsonGenerator] = None) -> FollowupResponse:
    generate = generate or gemini_json
    clock = _Clock()
    history = [t.model_dump() for t in req.history]
    link_ids = [link.id for link in req.links()]

    def translate(sentence: str) -> Optional[str]:
        try:
            t = _call(generate, fp.translate_prompt(sentence, language), TranslateOut,
                      clock.budget(0.5)).text.strip()
        except ReportError as e:
            log.info("translation failed (%s); using English", e.code)
            return None
        return t if t and not _PLAIN_RE.search(t) and len(t) <= 400 else None

    # 1. classify
    c = _call(generate, fp.classify_prompt(req.question, history, [{"id": link.id, "label": link.label}
                                                                    for link in req.links()]),
              ClassifyOut, clock.budget(0.4))
    language = normalize_language(c.language, req.question)
    cjk = is_cjk(language)
    supplied = extract_user_supplied(req.question, c)
    links = [i for i in dict.fromkeys(c.links) if i in link_ids][:3]

    # 2. out of scope: the fixed refusal chosen by code
    if c.out_of_scope:
        text = REFUSAL_EN if language.lower().startswith("en") else (translate(REFUSAL_EN) or REFUSAL_EN)
        refusal = Sentence(text=text, source_ids=[])
        links = links or link_ids
        if refusal_mode == "strict":
            data = _kb(req, clock)
            ev = build_evidence(req, data.items, data.sources.keys())
            if check_answer(refusal, ev, cjk):
                raise ReportError(502, "validation_failed", "No answer sentence passed validation.")
        return FollowupResponse(language=language, answer=[refusal], out_of_scope=True, user_supplied=supplied,
                                links=links, dropped=0)

    # 3. merge user_supplied; property values with status "found" win; re-run rules when something new is used
    pf = req.property_fields()
    chat = dict(req.chat_supplied())
    notes: list[str] = []
    used: dict[str, Any] = {}
    conflicts: list[tuple[str, str]] = []  # (field, council source id)
    for key, value in supplied.items():
        f = pf.get(key)
        if f is not None and f.status == "found" and f.value is not None:
            dr = (f.model_extra or {}).get("decade_range")
            notes.append(f"The user said the {key.replace('_', ' ')} is {value}, but the council record ({f.source_id}) "
                         f"has {f.value}{f' (decade {dr[0]}s)' if dr else ''}. The council figure is used. Say in one "
                         f"sentence, citing {f.source_id}, that the council data differs from what the user said and "
                         f"that the council figure is used. Do not write the user's value.")
            if f.source_id:
                conflicts.append((key, f.source_id))
        else:
            used[key] = value
    user_inputs = dict(req.user_inputs)
    rules_output = req.rules_output
    if used and any(chat.get(k) != v for k, v in used.items()):
        chat.update(used)
        user_inputs["chat_supplied"] = chat
        rules_output = call_rules(req.property_context, user_inputs, max(0.5, min(5.0, clock.left() * 0.3)))
    elif used:
        user_inputs["chat_supplied"] = chat
    eff = ReportRequest(property_context=req.property_context, user_inputs=user_inputs, rules_output=rules_output)

    data = _kb(eff, clock)
    ev = build_evidence(eff, data.items, data.sources.keys())
    titles = {s.id: s.title or "" for s in eff.sources()} | data.sources
    if used:
        # P-V3 exception: the validated, used year is allowed only with the items it selected.
        years = [Decimal(y) for y in _year_numbers(used.get("year_built"))]
        for i in ev.user_kb:
            ev.allowed.setdefault(i, []).extend(Allowed(y) for y in years)
        items = sorted(ev.user_kb)
        what = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in used.items())
        notes.append(f"The user said: {what}. This is unverified and is used for this answer. "
                     + (f"Items it selected: {items}; cite them for anything that depends on it"
                        + (" (the year may be written only in a sentence citing one of them)." if years else ".")
                        if items else "It selected no extra KB item; do not write the user's value."))
    elif ev.user_kb:
        notes.append(f"Items {sorted(ev.user_kb)} were selected from what the user told us earlier (unverified).")

    # 4. answer, validate; one retry when nothing passes and time allows
    base = fp.answer_prompt(eff, ev, data.items, titles, req.question, history, language, notes, cjk)
    prompt, dropped, passed, out_links = base, 0, [], []
    last: Optional[ReportError] = None
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
        rejected = []
        for s in out.answer:
            s = Sentence(text=tidy(s.text), source_ids=list(dict.fromkeys(s.source_ids)))
            fails = check_answer(s, ev, cjk)
            (rejected.append((s, fails)) if fails else passed.append(s))
        for s, f in rejected:
            log.info("dropped answer sentence: %s", ",".join(f))
        dropped = len(rejected)
        out_links = out.links
        if passed:
            break
        last = ReportError(502, "validation_failed", "No answer sentence passed validation.")
        prompt = base + ("\nYour previous answer could not be used (" +
                         ", ".join(sorted({r for _, f in rejected for r in f})) + "). Follow every rule exactly.\n")
    # The conflict must be stated (5.3): if no passing sentence cites the council source, code adds a fixed one.
    fixed = [conflict_sentence(key, sid, language, translate) for key, sid in conflicts
             if not any(sid in s.source_ids for s in passed)]
    fixed = [s for s in fixed if not check_answer(s, ev, cjk)]
    passed = fixed + passed[:max(0, MAX_ANSWER - len(fixed))]
    if not passed:
        raise last or ReportError(502, "validation_failed", "No answer sentence passed validation.")

    passed = passed[:MAX_ANSWER]
    if any(set(s.source_ids) & ev.user_kb for s in passed):
        prefix = prefix_for(language, translate)
        passed = [add_prefix(s, prefix) if set(s.source_ids) & ev.user_kb else s for s in passed]
    links = [i for i in dict.fromkeys(out_links + links) if i in link_ids][:3]
    return FollowupResponse(language=language, answer=passed, out_of_scope=False, user_supplied=supplied,
                            links=links, dropped=dropped)


def _kb(req: ReportRequest, clock: _Clock):
    try:
        return kb.load(req.selected_kb_ids(), timeout=max(0.5, min(5.0, clock.left())))
    except kb.KbUnavailable:
        raise ReportError(503, "kb_unavailable", "Knowledge-base items or sources could not be read.")
