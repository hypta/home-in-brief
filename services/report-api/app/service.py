"""POST /v1/report: KB lookup, fact list, Gemini (or mock) rewrite, validation, draft fallback and the code-built parts."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from b_common.report_schemas import Report, ReportRequest, Sentence

from . import assemble, gemini, kb, mock, obs, prompt, protection, rewrite
from .validator import (MAX_WORDS, PREFIX, WEB_PREFIX, Rewrite, build_evidence, check_rewrite, check_sentence, relies_on_user_input,
                        validate, with_prefix)

log = logging.getLogger("report-api")
NZ = ZoneInfo("Pacific/Auckland")
MAX_SUMMARY = 5
CODE_DRAFT = "code-draft"  # Report.model when no sentence is a Gemini rewrite: no model wrote the wording


class ReportError(Exception):
    def __init__(self, status: int, code: str, message: str, category: str = ""):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.category = category  # a model failure's obs category (gemini.NO_RETRY: no immediate retry)


@dataclass
class Attempt:
    seconds: float
    error: Optional[str] = None
    sentences: list[dict] = field(default_factory=list)  # {text, source_ids, fails}


@dataclass
class Debug:
    mode: str = "gemini"
    mock_case: Optional[str] = None
    attempts: list[Attempt] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)     # {id, text, draft, must, user, source_ids}
    composed: list[dict] = field(default_factory=list)  # final model sentences: {text, fact_ids, kind}
    rewrite: dict = field(default_factory=dict)         # rewrite.stats(): rewritten / fallback_drafts / must_added


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def mock_enabled() -> bool:
    return os.environ.get("REPORT_MOCK", "").lower() in ("1", "true", "yes")


def load_kb(req: ReportRequest, budget: gemini.Budget) -> kb.KbData:
    """KB items and sources within the request's deadline (at most 5 s). 503 kb_unavailable."""
    try:
        return kb.load(req.selected_kb_ids(), timeout=min(5.0, max(0.1, budget.left())))
    except kb.KbUnavailable:
        raise ReportError(503, "kb_unavailable", "Knowledge-base items or sources could not be read.")


def build_report(req: ReportRequest, generate: Optional[gemini.Generator] = None,
                 debug: Optional[Debug] = None, kbd: Optional[kb.KbData] = None) -> Report:
    """The report (_build_report). When protection refused the model call (allowance used up, REPORT_DEGRADED,
    prompt too large) the summary is the checked drafts and warnings end with generation_degraded. `kbd`: the KB
    the route already read (its fingerprint is in the cache key)."""
    with protection.request_scope() as scope:
        rep = _build_report(req, generate, debug, kbd)
    if scope.degraded and protection.GENERATION_DEGRADED not in rep.warnings:
        rep = rep.model_copy(update={"warnings": [*rep.warnings, protection.GENERATION_DEGRADED]})
    return rep


def _build_report(req: ReportRequest, generate: Optional[gemini.Generator] = None,
                  debug: Optional[Debug] = None, kbd: Optional[kb.KbData] = None) -> Report:
    debug = debug if debug is not None else Debug()
    budget = gemini.Budget()  # inside a route: the request's one deadline (gemini.request_budget)

    kbd = kbd if kbd is not None else load_kb(req, budget)
    ev = build_evidence(req, kbd.items, kbd.sources.keys())
    titles = {s.id: s.title or "" for s in req.sources()} | kbd.sources

    user_flags: list[bool] = []
    item_fields: list[frozenset] = []  # the chat-supplied fields each sentence's facts depend on
    if mock_enabled():
        debug.mode = "mock"
        case = mock.match_case(req)
        debug.mock_case = case
        model = "mock"
        if case:
            # A known fixture gets the facts' drafts (no model call), through the same checks as a real summary.
            fs = prompt.fact_list(req, ev, kbd.items)
            blocked = [Rewrite(None, [f.id], []) for f in fs]
            items = rewrite.compose(blocked, fs, MAX_SUMMARY, add_musts=True,
                                    check_draft=lambda s: check_sentence(s, ev, MAX_WORDS))
            passed, dropped = [it.sentence for it in items], 0
            user_flags = [it.user for it in items]
            item_fields = [it.fields for it in items]
            debug.composed = [{"text": it.sentence.text, "fact_ids": it.fact_ids, "kind": it.kind} for it in items]
        else:
            passed, dropped = [Sentence(text=assemble.MOCK_SENTENCE, source_ids=[])], 0
    else:
        items, dropped = _gemini_summary(req, ev, kbd.items, titles, generate or gemini.gemini_generate,
                                         budget, debug)
        model = gemini.model_name() if any(it.kind == rewrite.REWRITE for it in items) else CODE_DRAFT
        passed, user_flags = [it.sentence for it in items], [it.user for it in items]
        item_fields = [it.fields for it in items]

    user_flags += [False] * (len(passed) - len(user_flags))
    item_fields += [frozenset()] * (len(passed) - len(item_fields))
    # The prefix names where the value came from: the user, or a web search (user_inputs.chat_supplied_source). A
    # sentence that uses any value found by a web search gets the web prefix, also when it uses the user's values.
    summary = [with_prefix(s, WEB_PREFIX if prompt.from_web(s.source_ids, req, ev, kbd.items, fields) else PREFIX)
               if user or relies_on_user_input(s, ev) else s
               for s, user, fields in list(zip(passed, user_flags, item_fields))[:MAX_SUMMARY]]
    fixed = assemble.no_loan_sentence(req)
    if fixed:
        summary.append(fixed)
    return Report(
        generated_at=datetime.now(NZ).isoformat(timespec="seconds"),
        model=model,
        summary=summary,
        next_steps=assemble.next_steps(req, kbd.items, summary, ev.no_loan),
        not_found_notes=assemble.not_found_notes(req),
        warnings=assemble.reader_warnings(req),
        dropped=dropped,
    )


def _gemini_summary(req, ev, kb_items, titles, generate, budget: gemini.Budget, debug) -> tuple[list, int]:
    """Gemini rewrites the fact list; each rewrite is checked (validator.check_rewrite) and blocked ones fall back
    to their facts' drafts (rewrite.compose). One retry when no rewrite passes and time allows. A call is made only
    with gemini.ADMIT_S left (and sent with at least gemini.MIN_CALL_S); when there is no time for a first call, or
    for another after a failed one, the summary is the facts' drafts. The drafts also answer when every call failed
    (model unavailable or invalid JSON; no retry after quota or a missing key, gemini.NO_RETRY): an error only when
    not even a draft passes. Returns the composed items and the number of blocked model sentences."""
    fs = prompt.fact_list(req, ev, kb_items)
    debug.facts = [{"id": f.id, "text": f.text, "draft": f.draft(), "must": f.must, "user": f.user,
                    "source_ids": f.source_ids} for f in fs]
    base = prompt.build_prompt(req, ev, kb_items, titles, fs)
    text = base
    last_error: Optional[ReportError] = None
    last_seconds = 0.0
    best: Optional[list] = None
    out_of_time = stop = False
    for attempt in range(2):
        if not budget.can_call() or (attempt and budget.left() < last_seconds + 1.0):
            out_of_time = True  # not enough time left for a (full second) call
            break
        t0 = gemini.clock()
        rec = Attempt(0.0)
        debug.attempts.append(rec)
        try:
            raw = generate(text, budget.timeout())
            sentences = gemini.parse(raw)
        except gemini.ModelUnavailable as e:
            rec.error = f"model_unavailable: {e}"
            last_error = ReportError(503, "model_unavailable", "The language model did not answer.")
            text = base
            stop = e.category in gemini.NO_RETRY
        except gemini.ModelOutputInvalid as e:
            rec.error = f"model_output_invalid: {e}"
            last_error = ReportError(502, "model_output_invalid", "The language model's answer did not match the schema.")
            text = base + prompt.retry_note(["invalid JSON"])
        else:
            checked = [check_rewrite(s, fs, ev, MAX_WORDS) for s in sentences]
            rec.sentences = [{"text": s.text, "fact_ids": s.fact_ids, "filled": r.filled,
                              "source_ids": r.sentence.source_ids if r.sentence else [], "fails": r.fails}
                             for s, r in zip(sentences, checked)]
            for r in checked:
                if r.fails:
                    log.info("rewrite blocked: %s", ",".join(r.fails))
            if best is None or sum(r.sentence is not None for r in checked) > sum(r.sentence is not None for r in best):
                best = checked
            rec.seconds = last_seconds = gemini.clock() - t0
            if any(r.sentence is not None for r in checked):
                break
            last_error = None
            text = base + prompt.retry_note([f for r in checked for f in r.fails] or ["no sentences"])
            continue
        rec.seconds = last_seconds = gemini.clock() - t0
        if stop:
            break
    if best is None and out_of_time and last_error is None:
        obs.note_degraded(obs.DEADLINE)
        log.info("no time for a model call; summary from the drafts")
    elif best is None:
        log.warning("no usable model answer (%s); summary from the drafts", last_error.code if last_error else "none")
    elif best is not None and not any(r.sentence is not None for r in best):
        obs.note_degraded(obs.MODEL_ERROR)  # the model answered, but no rewrite passed the checks
    best = best if best is not None else []
    items = rewrite.compose(best, fs, MAX_SUMMARY, add_musts=True,
                            check_draft=lambda s: check_sentence(s, ev, MAX_WORDS))
    if not items:
        raise last_error or ReportError(502, "validation_failed", "No summary sentence passed validation.")
    debug.composed = [{"text": it.sentence.text, "fact_ids": it.fact_ids, "kind": it.kind} for it in items]
    debug.rewrite = rewrite.stats(items, best)
    return items, sum(r.sentence is None for r in best)


def debug_dict(d: Debug) -> dict[str, Any]:
    return {"mode": d.mode, "mock_case": d.mock_case,
            "attempts": [{"seconds": round(a.seconds, 2), "error": a.error, "sentences": a.sentences}
                         for a in d.attempts],
            "facts": d.facts, "composed": d.composed, "rewrite": d.rewrite}
