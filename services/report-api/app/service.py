"""POST /v1/report: KB lookup, fact list, Gemini (or mock) rewrite, validation, draft fallback and the code-built parts."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from b_common.report_schemas import Report, ReportRequest, Sentence

from . import assemble, gemini, kb, mock, prompt, rewrite
from .validator import (MAX_WORDS, PREFIX, WEB_PREFIX, build_evidence, check_rewrite, check_sentence, relies_on_user_input,
                        validate, with_prefix)

log = logging.getLogger("report-api")
NZ = ZoneInfo("Pacific/Auckland")
MAX_SUMMARY = 5


class ReportError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


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


def build_report(req: ReportRequest, generate: Optional[gemini.Generator] = None,
                 debug: Optional[Debug] = None) -> Report:
    debug = debug if debug is not None else Debug()
    start = time.monotonic()
    deadline = start + _env_float("REQUEST_DEADLINE_S", 25.0)
    call_limit = _env_float("GEMINI_TIMEOUT_S", 20.0)

    try:
        kbd = kb.load(req.selected_kb_ids(), timeout=min(5.0, deadline - time.monotonic()))
    except kb.KbUnavailable:
        raise ReportError(503, "kb_unavailable", "Knowledge-base items or sources could not be read.")
    ev = build_evidence(req, kbd.items, kbd.sources.keys())
    titles = {s.id: s.title or "" for s in req.sources()} | kbd.sources

    user_flags: list[bool] = []
    if mock_enabled():
        debug.mode = "mock"
        case = mock.match_case(req)
        debug.mock_case = case
        model = "mock"
        if case:
            # The golden reports predate the 25-word limit, so mock sentences skip the length check.
            verdict = validate(mock.summary_for(case), ev, max_words=None)
            passed, dropped = verdict.passed, verdict.dropped
            debug.attempts.append(Attempt(0.0, None, [
                {"text": s.text, "source_ids": s.source_ids, "fails": f} for s, f in verdict.rejected]))
        else:
            passed, dropped = [Sentence(text=assemble.MOCK_SENTENCE, source_ids=[])], 0
    else:
        model = gemini.model_name()
        items, dropped = _gemini_summary(req, ev, kbd.items, titles, generate or gemini.gemini_generate,
                                         deadline, call_limit, debug)
        passed, user_flags = [it.sentence for it in items], [it.user for it in items]

    user_flags += [False] * (len(passed) - len(user_flags))
    # The prefix names where the value came from: the user, or a web search (user_inputs.chat_supplied_source).
    summary = [with_prefix(s, WEB_PREFIX if prompt.from_web(s.source_ids, req, ev, kbd.items) else PREFIX)
               if user or relies_on_user_input(s, ev) else s
               for s, user in list(zip(passed, user_flags))[:MAX_SUMMARY]]
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


def _gemini_summary(req, ev, kb_items, titles, generate, deadline, call_limit, debug) -> tuple[list, int]:
    """Gemini rewrites the fact list; each rewrite is checked (validator.check_rewrite) and blocked ones fall back
    to their facts' drafts (rewrite.compose). One retry when no rewrite passes and time allows. Returns the
    composed items and the number of blocked model sentences."""
    fs = prompt.fact_list(req, ev, kb_items)
    debug.facts = [{"id": f.id, "text": f.text, "draft": f.draft(), "must": f.must, "user": f.user,
                    "source_ids": f.source_ids} for f in fs]
    base = prompt.build_prompt(req, ev, kb_items, titles, fs)
    text = base
    last_error: Optional[ReportError] = None
    last_seconds = 0.0
    best: Optional[list] = None
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if attempt and remaining < last_seconds + 1.0:
            break  # not enough time left for a full second attempt
        if remaining <= 1.0:
            break
        t0 = time.monotonic()
        rec = Attempt(0.0)
        debug.attempts.append(rec)
        try:
            raw = generate(text, min(call_limit, remaining))
            sentences = gemini.parse(raw)
        except gemini.ModelUnavailable as e:
            rec.error = f"model_unavailable: {e}"
            last_error = ReportError(503, "model_unavailable", "The language model did not answer.")
            text = base
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
            rec.seconds = last_seconds = time.monotonic() - t0
            if any(r.sentence is not None for r in checked):
                break
            last_error = None
            text = base + prompt.retry_note([f for r in checked for f in r.fails] or ["no sentences"])
            continue
        rec.seconds = last_seconds = time.monotonic() - t0
    if best is None:
        raise last_error or ReportError(503, "model_unavailable", "The language model did not answer in time.")
    items = rewrite.compose(best, fs, MAX_SUMMARY, add_musts=True,
                            check_draft=lambda s: check_sentence(s, ev, MAX_WORDS))
    if not items:
        raise ReportError(502, "validation_failed", "No summary sentence passed validation.")
    debug.composed = [{"text": it.sentence.text, "fact_ids": it.fact_ids, "kind": it.kind} for it in items]
    debug.rewrite = rewrite.stats(items, best)
    return items, sum(r.sentence is None for r in best)


def debug_dict(d: Debug) -> dict[str, Any]:
    return {"mode": d.mode, "mock_case": d.mock_case,
            "attempts": [{"seconds": round(a.seconds, 2), "error": a.error, "sentences": a.sentences}
                         for a in d.attempts],
            "facts": d.facts, "composed": d.composed, "rewrite": d.rewrite}
