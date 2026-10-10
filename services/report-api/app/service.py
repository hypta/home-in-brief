"""POST /v1/report: KB lookup, Gemini (or mock) summary, validation and the code-built parts."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from b_common.report_schemas import Report, ReportRequest, Sentence

from . import assemble, gemini, kb, mock, prompt
from .validator import MAX_WORDS, build_evidence, relies_on_user_input, validate, with_prefix

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
        passed, dropped = _gemini_summary(req, ev, kbd.items, titles, generate or gemini.gemini_generate,
                                          deadline, call_limit, debug)

    summary = [with_prefix(s) if relies_on_user_input(s, ev) else s for s in passed[:MAX_SUMMARY]]
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


def _gemini_summary(req, ev, kb_items, titles, generate, deadline, call_limit, debug) -> tuple[list[Sentence], int]:
    base = prompt.build_prompt(req, ev, kb_items, titles)
    text = base
    last_error: Optional[ReportError] = None
    last_seconds = 0.0
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
            verdict = validate(sentences, ev, MAX_WORDS)
            rec.sentences = ([{"text": s.text, "source_ids": s.source_ids, "fails": []} for s in verdict.passed] +
                             [{"text": s.text, "source_ids": s.source_ids, "fails": f} for s, f in verdict.rejected])
            for s, f in verdict.rejected:
                log.info("dropped sentence: %s", ",".join(f))
            if verdict.passed:
                rec.seconds = time.monotonic() - t0
                return verdict.passed, verdict.dropped
            last_error = ReportError(502, "validation_failed", "No summary sentence passed validation.")
            text = base + prompt.retry_note([r for _, f in verdict.rejected for r in f])
        rec.seconds = last_seconds = time.monotonic() - t0
    raise last_error or ReportError(503, "model_unavailable", "The language model did not answer in time.")


def debug_dict(d: Debug) -> dict[str, Any]:
    return {"mode": d.mode, "mock_case": d.mock_case,
            "attempts": [{"seconds": round(a.seconds, 2), "error": a.error, "sentences": a.sentences}
                         for a in d.attempts]}
