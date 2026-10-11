"""In-process cache for /v1/report (Codex F03 point 5): LRU, REPORT_CACHE_SIZE (100) entries, REPORT_CACHE_TTL_S
(600 s), with single flight (identical requests in flight wait for the first one instead of calling Gemini again).

Key: SHA-256 of the three input objects as canonical JSON (sorted keys, array order kept) plus the model name,
CACHE_VERSION (bump it when the prompt, facts or validator change what a report says) and the fingerprint of the KB
items and sources the report is checked against (kb_fingerprint: a KB change misses the cache; Codex R2). Left out
of the key: only the timestamps that say when an input was computed or fetched and that report-api never reads
(TIMESTAMP_PATHS: rules_output.generated_at, property_context.generated_at, property_context.sources[].accessed_at),
so identical inputs computed in different seconds hit. Rates dates, warnings, amounts, source labels and array order
stay in. Only reports with at least one Gemini rewrite that passed (X-Report-Mode gemini) are stored: never drafts,
mock, errors or degraded reports, never a request whose inputs carry a value found by a web search
(user_inputs.chat_supplied_source), and never chat_supplied values whose source is missing or does not match
(Codex R4: unknown provenance). A hit keeps its original generated_at. Follow-ups are never cached. Each process has
its own cache; the global allowance still bounds what several instances spend.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

CACHE_VERSION = "2026-10-11.2"
FOLLOWER_WAIT_S = 12.0  # a waiting duplicate gives up after this and generates itself


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def cacheable(property_context: dict, user_inputs: dict) -> bool:
    chat = user_inputs.get("chat_supplied") or {}
    if isinstance(chat, dict):
        # the web page sends {"year_built": null, "cladding": null} by default: no value, nothing whose source is unknown
        chat = {k: v for k, v in chat.items() if v is not None and v != ""}
    src = user_inputs.get("chat_supplied_source")
    if isinstance(src, dict) and any(v != "user" for v in src.values()):
        return False  # a web search value: Search material is not stored
    if chat and not (isinstance(chat, dict) and isinstance(src, dict) and set(src) == set(chat)):
        return False  # chat-supplied values whose source is missing or does not match: provenance unknown
    return _env_int("REPORT_CACHE_SIZE", 100) > 0 and _env_int("REPORT_CACHE_TTL_S", 600) > 0


# Timestamps of when an input was computed or fetched; report-api never reads them (tests/test_cache.py checks app/).
TIMESTAMP_PATHS = (("rules_output", "generated_at"), ("property_context", "generated_at"),
                   ("property_context", "sources", "*", "accessed_at"))


def _drop(obj: Any, path: tuple) -> None:
    if not path:
        return
    head, rest = path[0], path[1:]
    if head == "*":
        for item in obj if isinstance(obj, list) else []:
            _drop(item, rest)
    elif isinstance(obj, dict) and head in obj:
        if rest:
            _drop(obj[head], rest)
        else:
            del obj[head]


def without_timestamps(doc: dict) -> dict:
    out = copy.deepcopy(doc)
    for path in TIMESTAMP_PATHS:
        _drop(out, path)
    return out


def kb_fingerprint(items: dict, sources: Any) -> str:
    """SHA-256 of the KB items (full content) and the source registry (titles and URLs) a report is checked against."""
    doc = {"items": items, "sources": dict(sources or {}), "urls": dict(getattr(sources, "urls", None) or {})}
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _without_null_chat(user_inputs: dict) -> dict:
    """chat_supplied entries that are null are no values (ReportRequest.chat_supplied() drops them too), so a null
    entry and an absent one give the same report and the same key."""
    chat = user_inputs.get("chat_supplied")
    if not isinstance(chat, dict) or all(v is not None for v in chat.values()):
        return user_inputs
    kept = {k: v for k, v in chat.items() if v is not None}
    out = {k: v for k, v in user_inputs.items() if k != "chat_supplied"}
    if kept:
        out["chat_supplied"] = kept
    return out


def key(property_context: dict, user_inputs: dict, rules_output: dict, model: str, kb: str = "") -> str:
    doc = without_timestamps({"property_context": property_context, "user_inputs": _without_null_chat(user_inputs),
                              "rules_output": rules_output})
    doc |= {"model": model, "version": CACHE_VERSION, "kb": kb}
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.value: Optional[dict] = None


class ReportCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.entries: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
        self.flights: dict[str, _Flight] = {}

    def get(self, k: str) -> Optional[dict]:
        with self.lock:
            hit = self.entries.get(k)
            if hit is None:
                return None
            if hit[0] <= self.clock():
                del self.entries[k]
                return None
            self.entries.move_to_end(k)
            return copy.deepcopy(hit[1])

    def join(self, k: str) -> tuple[bool, _Flight]:
        """(True, flight) for the first request with this key (the leader, which must call finish); else (False,
        the leader's flight) to wait on."""
        with self.lock:
            f = self.flights.get(k)
            if f is not None:
                return False, f
            f = self.flights[k] = _Flight()
            return True, f

    def finish(self, k: str, value: Optional[dict]) -> None:
        """The leader's result (None: not cacheable, or failed); wakes the waiting duplicates."""
        with self.lock:
            f = self.flights.pop(k, None)
            if value is not None:
                self.entries[k] = (self.clock() + _env_int("REPORT_CACHE_TTL_S", 600), copy.deepcopy(value))
                self.entries.move_to_end(k)
                while len(self.entries) > _env_int("REPORT_CACHE_SIZE", 100):
                    self.entries.popitem(last=False)
        if f is not None:
            f.value = copy.deepcopy(value) if value is not None else None
            f.done.set()

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()


REPORTS = ReportCache()


def wait(flight: _Flight, timeout: float = FOLLOWER_WAIT_S) -> Optional[dict[str, Any]]:
    """The leader's result, waiting at most `timeout` (the route passes what the request has left, at most
    FOLLOWER_WAIT_S); None: the leader failed, stored nothing, or is still running."""
    if flight.done.wait(max(0.0, timeout)):
        return copy.deepcopy(flight.value) if flight.value is not None else None
    return None
