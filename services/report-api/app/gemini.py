"""Gemini call with structured output (google-genai, as jobs/rates-refresh does)."""
from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Mapping, Optional

from pydantic import BaseModel, ValidationError

from . import obs, protection
from .facts import RewriteSentence

log = logging.getLogger("report-api.gemini")

DEFAULT_MODEL = "gemini-3.5-flash-lite"  # same default as jobs/rates-refresh; override with GEMINI_MODEL
TEMPERATURE = 0.0  # values are placeholders filled by code; a fixed temperature keeps eval runs comparable
SEED = 20261010    # fixed seed: same prompt, same answer as far as the API allows


# ---- time budget for every Gemini call (summary, follow-up, translation, searches) --------------------------
# The Gemini API refuses a deadline under 10 s (400 "Minimum allowed deadline is 10s"), so every call is sent with
# at least MIN_CALL_S, and a call is made only when at least ADMIT_S is left of the request's deadline: a call can
# then never run past the deadline (REQUEST_DEADLINE_S, 25 s by default; web waits 30 s for the whole request).
# When too little time is left, callers use the program's drafts or fixed sentences instead of calling the model.
MIN_CALL_S = 10.0
ADMIT_S = 11.0
MAX_SEARCHES = 2  # Google Search calls per follow-up request, field search and out-of-scope search together
# One deadline per request (Codex R1): set at route entry (request_budget), REQUEST_DEADLINE_S or, when web sends it,
# X-Request-Deadline-Ms (web's remaining milliseconds), whichever is smaller. The single-flight wait, KB reads, the
# allowance reservation and every Gemini call count against it; every Budget made during the request shares it.
DEADLINE_HEADER = "x-request-deadline-ms"
_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar("request_deadline", default=None)


def clock() -> float:
    """Monotonic seconds. Replaced in tests by a fake clock."""
    return time.monotonic()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class Budget:
    """One request's deadline. `timeout(share)` is the deadline for one call: at least MIN_CALL_S, at most
    GEMINI_TIMEOUT_S, `share` of what is left and 1 s less than what is left; `can_call()` says whether a call may be
    made at all (ADMIT_S left). `searches` counts the Google Search calls made (MAX_SEARCHES)."""

    def __init__(self, total_s: float | None = None):
        shared = _deadline.get() if total_s is None else None
        if shared is not None:
            self.deadline = shared  # the request's deadline (request_budget): never a fresh budget
        else:
            total = _env_float("REQUEST_DEADLINE_S", 25.0) if total_s is None else total_s
            self.deadline = clock() + total
        self.call_limit = max(MIN_CALL_S, _env_float("GEMINI_TIMEOUT_S", 20.0))
        self.searches = 0
        obs.track_budget(self)

    def left(self) -> float:
        return self.deadline - clock()

    def can_call(self) -> bool:
        return self.left() >= ADMIT_S

    def can_search(self) -> bool:
        return self.searches < MAX_SEARCHES and self.can_call()

    def timeout(self, share: float = 1.0) -> float:
        left = self.left()
        return max(MIN_CALL_S, min(self.call_limit, left * share, left - 1.0))


def header_seconds(headers: Mapping[str, str]) -> Optional[float]:
    """X-Request-Deadline-Ms as seconds: a finite number >= 0, else None (ignored)."""
    raw = (headers.get(DEADLINE_HEADER) or "").strip()
    if not raw:
        return None
    try:
        ms = float(raw)
    except ValueError:
        return None
    return ms / 1000.0 if math.isfinite(ms) and ms >= 0 else None


@contextmanager
def request_budget(headers: Optional[Mapping[str, str]] = None) -> Iterator[Budget]:
    """The request's one deadline: REQUEST_DEADLINE_S (25 s), or less when web's X-Request-Deadline-Ms says less
    (it can never extend it). Every Budget created inside shares it."""
    total = _env_float("REQUEST_DEADLINE_S", 25.0)
    sent = header_seconds(headers or {})
    if sent is not None:
        total = min(total, sent)
    token = _deadline.set(clock() + total)
    try:
        yield Budget()
    finally:
        _deadline.reset(token)


def left() -> Optional[float]:
    """Seconds left of the request's deadline; None outside a request (eval runs, unit tests)."""
    d = _deadline.get()
    return None if d is None else d - clock()


def timeout_ms(timeout_s: float) -> int:
    """The HttpOptions timeout in milliseconds, never under the API minimum (10000)."""
    return max(int(MIN_CALL_S * 1000), math.ceil(timeout_s * 1000))


class ModelUnavailable(Exception):
    """Timeout, quota, 5xx or any failure to get an answer. `category`: obs.classify's error category."""

    def __init__(self, message: str = "", category: str = obs.OTHER):
        super().__init__(message)
        self.category = category


# No immediate retry after these: exhausted quota (429) or no key cannot succeed a second later.
# Nor after a call protection refused (allowance used up, REPORT_DEGRADED, prompt too large), nor when the
# request's deadline left no time after the reservation: the drafts answer.
NO_RETRY = frozenset({obs.QUOTA, obs.CONFIG, obs.ALLOWANCE, obs.DEADLINE})
# These also stop every further model call of the request (Codex R5: protection.stop_request).
STOPS_REQUEST = NO_RETRY


def check_stopped() -> None:
    """Every SDK wrapper calls this first: after quota, a config error, an allowance refusal or no time left after a
    reservation, the request makes no further model call (nothing reserved, nothing sent)."""
    if protection.stopped():
        raise ModelUnavailable("request stopped", obs.ALLOWANCE)


def stop_request(category: str) -> None:
    if category in STOPS_REQUEST:
        protection.stop_request(category)


def admit(prompt: str, timeout_s: float, search: bool = False) -> float:
    """The allowance gate every SDK call passes right before it is sent (protection.admit): raises
    ModelUnavailable (obs.ALLOWANCE; obs.SEARCH_ALLOWANCE when only the Search allowance is used up) when the call
    must not be made. Nothing is sent then. The reservation's wait counts against the request's deadline: after
    it, the time left is checked again (under ADMIT_S: the reserved call is kept, nothing is sent, ModelUnavailable
    obs.DEADLINE) and the call's timeout recomputed from what is left. Returns that timeout."""
    check_stopped()
    reason = protection.admit(prompt, search, wait_s=left())
    if reason != protection.OK:
        category = obs.SEARCH_ALLOWANCE if reason == protection.SEARCH else obs.ALLOWANCE
        stop_request(category)
        raise ModelUnavailable(f"call refused: {reason}", category)
    remaining = left()
    if remaining is None:
        return timeout_s
    if remaining < ADMIT_S:
        protection.refused_after_reservation()
        obs.note_degraded(obs.DEADLINE)
        stop_request(obs.DEADLINE)
        raise ModelUnavailable("no time left after the reservation", obs.DEADLINE)
    return max(MIN_CALL_S, min(timeout_s, remaining - 1.0))


def http_options(timeout_s: float):
    """One transport attempt per reserved call: google-genai 2.28.0 retries only when retry_options is set
    (_api_client.retry_args: None -> stop_after_attempt(1)); attempts=1 says so explicitly."""
    from google.genai import types
    return types.HttpOptions(timeout=timeout_ms(timeout_s), retry_options=types.HttpRetryOptions(attempts=1))


def no_key() -> ModelUnavailable:
    obs.note_error(obs.CONFIG)
    stop_request(obs.CONFIG)
    return ModelUnavailable("GEMINI_API_KEY is not set", obs.CONFIG)


def failed(logger: logging.Logger, what: str, exc: BaseException) -> ModelUnavailable:
    """A failed SDK call: logged by category only (obs.gemini_failed, no exception text); quota stops the request."""
    msg, category = obs.gemini_failed(logger, what, exc)
    stop_request(category)
    return ModelUnavailable(msg, category)


class ModelOutputInvalid(Exception):
    """The answer did not match the response schema."""


class SummaryOut(BaseModel):
    """The rewrite: sentences with placeholders and the fact ids they use (validator.check_rewrite)."""
    summary: list[RewriteSentence]


# (prompt, timeout_s) -> raw JSON text. Replaced in tests.
Generator = Callable[[str, float], str]


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


def gemini_generate(prompt: str, timeout_s: float) -> str:
    from google import genai
    from google.genai import types

    check_stopped()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise no_key()
    timeout_s = admit(prompt, timeout_s)
    obs.note_call()
    try:
        client = genai.Client(api_key=key, http_options=http_options(timeout_s))
        resp = client.models.generate_content(
            model=model_name(),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SummaryOut,
                temperature=TEMPERATURE,
                seed=SEED,
                max_output_tokens=protection.max_output_tokens(),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except Exception as e:  # httpx timeouts, google.genai.errors.APIError (429, 5xx, bad key), network
        raise failed(log, "gemini call", e) from e
    text = resp.text
    if not text:
        obs.note_error(obs.SCHEMA)
        raise ModelOutputInvalid("empty response")
    return text


def parse(raw: str) -> list[RewriteSentence]:
    try:
        return SummaryOut.model_validate(json.loads(raw)).summary
    except (ValueError, ValidationError) as e:
        obs.note_error(obs.SCHEMA)
        raise ModelOutputInvalid(type(e).__name__) from e  # no model text in the message (Codex R7)
