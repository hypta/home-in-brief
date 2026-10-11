"""Gemini call with structured output (google-genai, as jobs/rates-refresh does)."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Callable

from pydantic import BaseModel, ValidationError

from . import obs
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


def timeout_ms(timeout_s: float) -> int:
    """The HttpOptions timeout in milliseconds, never under the API minimum (10000)."""
    return max(int(MIN_CALL_S * 1000), math.ceil(timeout_s * 1000))


class ModelUnavailable(Exception):
    """Timeout, quota, 5xx or any failure to get an answer. `category`: obs.classify's error category."""

    def __init__(self, message: str = "", category: str = obs.OTHER):
        super().__init__(message)
        self.category = category


# No immediate retry after these: exhausted quota (429) or no key cannot succeed a second later.
NO_RETRY = frozenset({obs.QUOTA, obs.CONFIG})


def no_key() -> ModelUnavailable:
    obs.note_error(obs.CONFIG)
    return ModelUnavailable("GEMINI_API_KEY is not set", obs.CONFIG)


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

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise no_key()
    obs.note_call()
    try:
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms(timeout_s)))
        resp = client.models.generate_content(
            model=model_name(),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SummaryOut,
                temperature=TEMPERATURE,
                seed=SEED,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except Exception as e:  # httpx timeouts, google.genai.errors.APIError (429, 5xx, bad key), network
        msg, category = obs.gemini_failed(log, "gemini call", e, key)
        raise ModelUnavailable(f"{type(e).__name__}: {msg}", category) from e
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
        raise ModelOutputInvalid(str(e)[:300]) from e
