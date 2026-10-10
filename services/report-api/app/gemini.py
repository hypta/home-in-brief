"""Gemini call with structured output (google-genai, as jobs/rates-refresh does)."""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

from pydantic import BaseModel, ValidationError

from b_common.report_schemas import Sentence

log = logging.getLogger("report-api.gemini")

DEFAULT_MODEL = "gemini-3.5-flash-lite"  # same default as jobs/rates-refresh; override with GEMINI_MODEL
TEMPERATURE = 0.0  # the drafts are finished sentences; the model only copies and smooths them
SEED = 20261010    # fixed seed: same prompt, same answer as far as the API allows


class ModelUnavailable(Exception):
    """Timeout, quota, 5xx or any failure to get an answer."""


class ModelOutputInvalid(Exception):
    """The answer did not match the response schema."""


class SummaryOut(BaseModel):
    summary: list[Sentence]


# (prompt, timeout_s) -> raw JSON text. Replaced in tests.
Generator = Callable[[str, float], str]


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


def gemini_generate(prompt: str, timeout_s: float) -> str:
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ModelUnavailable("GEMINI_API_KEY is not set")
    try:
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
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
        msg = str(e).replace(key, "***")[:300]
        log.warning("gemini call failed: %s: %s", type(e).__name__, msg)
        raise ModelUnavailable(f"{type(e).__name__}: {msg}") from e
    text = resp.text
    if not text:
        raise ModelOutputInvalid("empty response")
    return text


def parse(raw: str) -> list[Sentence]:
    try:
        return SummaryOut.model_validate(json.loads(raw)).summary
    except (ValueError, ValidationError) as e:
        raise ModelOutputInvalid(str(e)[:300]) from e
