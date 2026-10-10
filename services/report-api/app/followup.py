"""POST /v1/followup: answer one question in English, validated like the summary (INTERFACE_DRAFT 5, 6.1).

The validated answer is English only (2026-10-10). A question in any language is understood; `answer` is always
English, and a sentence with CJK script is blocked. `language` in the response describes `answer` and is always
"en" (kept for the contract).

Flow: classify (Gemini: out-of-scope, stated year/cladding) -> code checks user_supplied ->
optional rules-api re-run with the merged chat_supplied -> fact list (prompt.followup_facts, rebuilt from the
re-run RulesOutput) -> answer (Gemini rewrites facts, values as placeholders) -> validator.check_rewrite with the
English-only check -> blocked sentences fall back to their facts' drafts -> code-added prefix. Out of scope: an
answer from a Google Search when classify says web pages can answer it (`web`, in the language of the question,
shown whole with its sources and never run through the validator), else a fixed English refusal chosen by code.

question and history are untrusted: they never enter the evidence. The one exception (P-V3) is a validated
year_built that was used for the re-run; it is allowed only in sentences citing kb_items_from_user_input.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import replace
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
from .facts import RewriteSentence
from .service import ReportError, _env_float, mock_enabled
from .validator import (MAX_WORDS, Allowed, Evidence, build_evidence, check_rewrite, strip_prefix, to_decimal,
                        validate)

log = logging.getLogger("report-api.followup")
MAX_ANSWER = 3

REFUSAL_EN = ("This question is outside what this property report covers; the original sources linked below "
              "may help you look it up.")
PREFIX_EN = "Based on what you told us, "
LANGUAGE = "en"  # response field kept for the contract; always English
NON_ENGLISH = "non_english"


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
    out_of_scope: bool = False
    searchable: bool = False
    year_built: Optional[str] = None
    cladding: Optional[Literal[cladding_table.VALUES]] = None  # type: ignore[valid-type]  # the 13 shared values
    hypothetical: bool = False
    links: list[str] = Field(default_factory=list)


class AnswerOut(BaseModel):
    answer: list[RewriteSentence]
    links: list[str] = Field(default_factory=list)


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


# ---- validation (validator.validate plus the English-only rule) ---------------------------------
_CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef]")


def check_answer(s: Sentence, ev: Evidence) -> list[str]:
    """validator.check_sentence rules (25-word limit), plus: any CJK script blocks the sentence."""
    v = validate([s], ev, MAX_WORDS)
    fails = list(v.rejected[0][1]) if v.rejected else []
    if _CJK_RE.search(s.text):
        fails.append(NON_ENGLISH)
    return fails


_PROPER_START = re.compile(r"^(?:\S*\.\S|[A-Z]\S*[A-Z]|Auckland\b|Kāinga\b|Kainga\b|Settled\b)")


def add_prefix(s: Sentence) -> Sentence:
    """Code-added prefix; lower-case the first letter unless it starts a name ("Settled.govt.nz", "LINZ")."""
    body = strip_prefix(s.text).strip()
    if body and not _PROPER_START.match(body):
        body = body[:1].lower() + body[1:]
    return Sentence(text=PREFIX_EN + body, source_ids=s.source_ids)


# ---- user value vs. property value (5.3; user decision 10/10: show both) -------------------------
def comparison_sentence(key: str, value: Any, f: FieldView) -> Optional[Sentence]:
    """A code-written sentence comparing the user's value with the property field (status found).

    It names both values and cites the field's source_id. It is a program sentence: the user's value is
    allowed in it (and only in it); the validator still checks it (see check_comparison).
    """
    council = (f.source_id or "").startswith("ac_")
    src_en = "the council" if council else "public records"
    if key == "year_built":
        dr = (f.model_extra or {}).get("decade_range")
        decade = dr[0] if (isinstance(dr, list) and len(dr) == 2 and all(isinstance(x, int) for x in dr)
                           and dr[0] % 10 == 0 and dr[1] == dr[0] + 9) else None
        u_dec = re.fullmatch(r"(\d{3}0)s", str(value))
        u_int = value if isinstance(value, int) else None
        if decade is not None:
            same = (u_int is not None and decade <= u_int <= decade + 9) or (u_dec and int(u_dec.group(1)) == decade)
            c_en = f"the {decade}s ({decade}–{decade + 9})"
        else:
            cv = to_decimal(f.value)
            if cv is None:
                return None
            same = u_int is not None and Decimal(u_int) == cv
            c_en = f"{_n(cv)}"
        u_en = f"the {u_dec.group(1)}s" if u_dec else f"{u_int}"
        if same:
            en_t = (f"The year you gave, {u_en}, falls within {src_en}'s recorded decade, {c_en}."
                    if decade is not None and u_int is not None
                    else f"The year you gave, {u_en}, matches {src_en}'s record, {c_en}.")
        else:
            en_t = f"You gave the year built as {u_en}; {src_en} records {c_en}, and this answer uses {src_en}'s record."
    else:
        cv = str(f.value)
        shown = cladding_table.label(str(value))
        same = cv.strip().lower() == shown.lower() or cladding_table.normalize(cv) == value
        if same:
            en_t = f"The cladding you gave, {shown}, matches {src_en}'s record, {cv}."
        else:
            en_t = f"You gave the cladding as {shown}; {src_en} records {cv}, and this answer uses {src_en}'s record."
    return Sentence(text=en_t.replace("  ", " "), source_ids=[f.source_id])


def check_comparison(s: Sentence, value: Any, ev: Evidence) -> list[str]:
    """Program sentence rule: the validator as usual, with the user's year allowed under the cited field only."""
    years = [Decimal(y) for y in _year_numbers(value)]
    allowed = {k: list(v) for k, v in ev.allowed.items()}
    for sid in s.source_ids:
        allowed.setdefault(sid, []).extend(Allowed(y) for y in years)
    return check_answer(s, replace(ev, allowed=allowed))


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


def mock_classify(question: str) -> ClassifyOut:
    """The classify step by keywords. Values still go through extract_user_supplied."""
    year = None
    m = re.search(r"(?<!\d)(\d{3}0s)(?![\w])", question) or re.search(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", question)
    if m:
        year = m.group(1)
    cladding = cladding_table.normalize(question)
    oos = bool(_MOCK_OOS.search(question)) and not (_FLOOD_Q.search(question) or _HAZARD_Q.search(question))
    return ClassifyOut(out_of_scope=oos, year_built=year,
                       cladding=cladding)


def mock_answer(ev: Evidence, kb_items: dict[str, dict], used: dict) -> tuple[list[Sentence], int]:
    """claim_en of the items the user's value selected, verbatim; else one fixed mock sentence."""
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
        s = Sentence(text=MOCK_ANSWER, source_ids=[sid] if sid else [])
        if not check_answer(s, ev):
            passed.append(s)
    return passed, dropped


# ---- program facts (prompt.program_facts): shown as their drafts, never rewritten ------------------
# Questions about a field, for the safety net when the model names no program fact for it. Any language is
# understood by the model; these cover the common English and Chinese wordings of the demo questions.
_FIELD_Q = {
    "cladding": re.compile(r"cladding|exterior|outside walls?|\bwalls?\b|weatherboard|stucco|plaster|monolithic|"
                           r"roughcast|brick|leaky|外牆|牆|漏水", re.I),
    "year_built": re.compile(r"\bbuilt\b|\bbuild\b|year built|how old|\bage\b|decade|建造|屋齡|哪一年蓋|幾年蓋", re.I),
    "last_sale": re.compile(r"last sale|last sold|sale price|sold for|成交", re.I),
}


def program_sentences(fs: list, named: list[str], question: str, ev: Evidence) -> list[Sentence]:
    """The drafts of the program facts the model named, plus (safety net) the program fact of a field the
    question asks about; each checked by prompt.check_program and prefixed when it states what the user entered."""
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
        fails = facts_prompt.check_program(s, ev, f.slots) + (
            [NON_ENGLISH] if _CJK_RE.search(s.text) else [])
        if fails:
            log.warning("program sentence %s dropped: %s", i, ",".join(fails))
            continue
        out.append(add_prefix(s) if f.user else s)
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

    # 1. classify
    if mock:
        c = mock_classify(req.question)
    else:
        c = _call(generate, fp.classify_prompt(req.question, history, [{"id": link.id, "label": link.label}
                                                                        for link in req.links()]),
                  ClassifyOut, clock.budget(0.4))
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
                return FollowupResponse(language=LANGUAGE, answer=[], out_of_scope=True, user_supplied=supplied,
                                        links=links, dropped=0, warnings=warnings, web=web)
        refusal = Sentence(text=REFUSAL_EN, source_ids=[])
        links = links or fallback_links(req.question, link_ids)
        if refusal_mode == "strict":
            data = _kb(req, clock)
            ev = build_evidence(req, data.items, data.sources.keys(), source_urls=data.sources.urls)
            if check_answer(refusal, ev):
                raise ReportError(502, "validation_failed", "No answer sentence passed validation.")
        return FollowupResponse(language=LANGUAGE, answer=[refusal], out_of_scope=True, user_supplied=supplied,
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
    if used:
        allow_user_year(ev, used)
        what = ", ".join(k.replace("_", " ") for k in used)
        notes.append(f"The user just told us the {what}. This is unverified and is used for this answer: the facts "
                     "with \"user\": true depend on it" + ("." if ev.user_kb else
                                                         "; it selected no extra fact, so do not write the value."))
    elif ev.user_kb:
        notes.append("Facts with \"user\": true depend on what the user told us earlier (unverified).")

    # Program sentences comparing the user's value with the property value (both values, cited).
    fixed = []
    for key, value, f in compared:
        s = comparison_sentence(key, value, f)
        fails = check_comparison(s, value, ev) if s else ["no_council_value"]
        if fails:
            log.warning("comparison sentence dropped: %s", ",".join(fails))
        else:
            fixed.append(s)

    # 4. answer from the fact list; check each rewrite; one retry when none passes and time allows; blocked
    #    sentences fall back to their facts' drafts
    passed: list[Sentence] = []
    dropped, out_links = 0, []
    last: Optional[ReportError] = None
    limit = max(0, MAX_ANSWER - len(fixed))
    if mock:
        passed, dropped = mock_answer(ev, data.items, used)
        if fixed and not used:
            passed = []  # the comparison is the answer; no filler sentence
        passed = passed[:limit]
        if any(set(s.source_ids) & ev.user_kb for s in passed):
            passed = [add_prefix(s) if set(s.source_ids) & ev.user_kb else s for s in passed]
    else:
        fs = facts_prompt.followup_facts(eff, ev, data.items, used)
        debug["facts"] = [{"id": f.id, "text": f.text, "draft": f.draft(), "user": f.user,
                           "source_ids": f.source_ids} for f in fs]
        debug["attempts"] = []

        def final(s: Sentence) -> list[str]:
            return check_answer(s, ev)

        prog_ids = {f.id for f in fs if f.program}
        base = fp.answer_prompt(eff, ev, data.items, titles, req.question, history, notes, fs)
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
        debug["program"] = [s.text for s in prog]
        passed.extend(prog)
        limit -= len(prog)
        items = rewrite.compose(best or [], fs, limit, add_musts=False, sort=False,
                                check_draft=lambda s: check_answer(s, ev)) if limit else []
        dropped = sum(r.sentence is None for r in best or [])
        debug["composed"] = [{"text": it.sentence.text, "fact_ids": it.fact_ids, "kind": it.kind} for it in items]
        debug["rewrite"] = rewrite.stats(items, best or [])
        for it in items:
            s = it.sentence
            if it.user or set(s.source_ids) & ev.user_kb:
                s = add_prefix(s)
            passed.append(s)
    if not passed and not fixed:
        raise last or ReportError(502, "validation_failed", "No answer sentence passed validation.")

    links = [i for i in dict.fromkeys(out_links + links) if i in link_ids][:3]
    return FollowupResponse(language=LANGUAGE, answer=fixed + passed, out_of_scope=False, user_supplied=supplied,
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
