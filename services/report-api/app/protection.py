"""Abuse and cost protection (Codex F03): generation slots per process and a global Gemini call allowance.

Generation slots: `GENERATION_SLOTS` (2) /v1/report and /v1/followup requests run at once per process; a further
request is refused at once (no queue) with 503 `busy`, `Retry-After: 5`. /health takes no slot.

Allowance: every outbound Gemini SDK call (report rewrite, classify, answer, translation, extraction, Google
Search) reserves one call BEFORE it is sent (`admit`), in one Firestore transaction over two documents:

    protection/control          {disabled: bool, event_calls: int, event_searches: int, updated_at}
    protection/daily-YYYY-MM-DD {date, calls: int, searches: int, updated_at}   (UTC date of the server)

A call is refused when `disabled` is true, `event_calls` >= GEMINI_EVENT_CALLS (whole judging period),
`calls` >= GEMINI_DAILY_CALLS, or (Search only) `searches` >= SEARCH_DAILY_CALLS. A Search call counts once in
`calls` and once in `searches`. Reserved calls are never refunded (failures and timeouts count). The transaction
makes no Gemini call; it gets PROTECTION_TIMEOUT_S (1 s) in all, at most 2 attempts on contention. Firestore
unavailable, slow or conflicting: the call is refused (`backend_error`).

Refused calls never reach the network: the caller falls back to the program's drafts (gemini.ModelUnavailable
with category obs.ALLOWANCE, in gemini.NO_RETRY). A refused Search (obs.SEARCH_ALLOWANCE) stops only that search.
REPORT_DEGRADED=true refuses every call without reading Firestore; a prompt over GEMINI_INPUT_MAX_BYTES is refused
without counting.

PROTECTION_BACKEND: firestore (default on Cloud Run, where K_SERVICE is set) | memory (default elsewhere: one
process, for local runs and tests) | off (no allowance; slots, REPORT_DEGRADED and the size cap still apply).
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional

from . import obs

log = logging.getLogger("report-api.protection")

# allowance states (completion log field "allowance"); OK = every call was allowed
OK = "ok"
DAILY = "daily_exhausted"
EVENT = "event_exhausted"
SEARCH = "search_exhausted"
DISABLED = "disabled"          # protection/control.disabled is true
BACKEND = "backend_error"      # Firestore unavailable, slow or conflicting
DEGRADED = "degraded"          # REPORT_DEGRADED=true
TOO_LARGE = "prompt_too_large"  # prompt over GEMINI_INPUT_MAX_BYTES

GENERATION_DEGRADED = "generation_degraded"  # response warning when a model call was refused (not for SEARCH)
COLLECTION = "protection"
CONTROL_DOC = "control"

DEFAULTS = {"GEMINI_DAILY_CALLS": 100, "SEARCH_DAILY_CALLS": 30, "GEMINI_EVENT_CALLS": 3000,
            "GENERATION_SLOTS": 2, "GEMINI_MAX_OUTPUT_TOKENS": 2048, "GEMINI_INPUT_MAX_BYTES": 32000}
TIMEOUT_S = 1.0


def env_int(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name, DEFAULTS[name])))
    except ValueError:
        return DEFAULTS[name]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def degraded_switch() -> bool:
    return os.environ.get("REPORT_DEGRADED", "").lower() in ("1", "true", "yes")


def backend_name() -> str:
    default = "firestore" if os.environ.get("K_SERVICE") else "memory"
    name = (os.environ.get("PROTECTION_BACKEND") or default).lower()
    return name if name in ("firestore", "memory", "off") else "firestore"  # unknown value: the safe backend


def max_output_tokens() -> int:
    return max(1, env_int("GEMINI_MAX_OUTPUT_TOKENS"))


def utc_day(now: Optional[datetime] = None) -> str:
    """The server's UTC date; replaced in tests."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


# ---- generation slots ------------------------------------------------------------------------------------------
class Busy(Exception):
    """All generation slots are taken: 503 busy."""


_slots_lock = threading.Lock()
_slots: Optional[threading.BoundedSemaphore] = None


def _semaphore() -> threading.BoundedSemaphore:
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(max(1, env_int("GENERATION_SLOTS")))
        return _slots


def reset_slots() -> None:
    """Re-read GENERATION_SLOTS on the next request (tests)."""
    global _slots
    with _slots_lock:
        _slots = None


@contextmanager
def generation_slot() -> Iterator[None]:
    """Holds one slot for the whole request; refuses at once (Busy) when none is free. Released in finally."""
    sem = _semaphore()
    if not sem.acquire(blocking=False):
        obs.note_busy()
        log.warning("generation busy: request refused", extra={"json_fields": {"event": "busy_rejected"}})
        raise Busy()
    try:
        yield
    finally:
        sem.release()


# ---- per-request state (warning generation_degraded) -----------------------------------------------------------
@dataclass
class Scope:
    refused: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return any(r != SEARCH for r in self.refused)


_scope: contextvars.ContextVar[Optional[Scope]] = contextvars.ContextVar("protection_scope", default=None)


@contextmanager
def request_scope() -> Iterator[Scope]:
    sc = Scope()
    token = _scope.set(sc)
    try:
        yield sc
    finally:
        _scope.reset(token)


# ---- allowance -------------------------------------------------------------------------------------------------
@dataclass
class Limits:
    daily: int
    search: int
    event: int

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(env_int("GEMINI_DAILY_CALLS"), env_int("SEARCH_DAILY_CALLS"), env_int("GEMINI_EVENT_CALLS"))


def decide(control: dict, day: dict, search: bool, lim: Limits) -> str:
    """The allowance decision for one call, from the two documents' current values."""
    if control.get("disabled"):
        return DISABLED
    if int(control.get("event_calls") or 0) >= lim.event:
        return EVENT
    if int(day.get("calls") or 0) >= lim.daily:
        return DAILY
    if search and int(day.get("searches") or 0) >= lim.search:
        return SEARCH
    return OK


class MemoryBackend:
    """One process only (local runs, tests). Same documents and decision as the Firestore backend."""

    def __init__(self):
        self.lock = threading.Lock()
        self.control: dict = {}
        self.days: dict[str, dict] = {}

    def reserve(self, search: bool, lim: Limits, day: str) -> str:
        with self.lock:
            d = self.days.setdefault(day, {"date": day, "calls": 0, "searches": 0})
            reason = decide(self.control, d, search, lim)
            if reason == OK:
                self.control["event_calls"] = int(self.control.get("event_calls") or 0) + 1
                self.control["event_searches"] = int(self.control.get("event_searches") or 0) + int(search)
                d["calls"] += 1
                d["searches"] += int(search)
            return reason


class FirestoreBackend:
    """protection/control and protection/daily-<UTC date>, read and incremented in one transaction."""

    def __init__(self, client_factory: Optional[Callable] = None):
        self._client_factory = client_factory
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="allowance")
        # One transaction at a time per process: two at once on the same documents contend, and contention takes
        # longer than the time limit to resolve (about 2 s on the emulator). Held until the transaction ends.
        self._one = threading.Lock()

    def client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from b_common.firestore import get_client
        return get_client()

    def _transaction(self, search: bool, lim: Limits, day: str) -> str:
        from google.cloud import firestore

        client = self.client()
        coll = client.collection(COLLECTION)
        ctrl_ref, day_ref = coll.document(CONTROL_DOC), coll.document(f"daily-{day}")
        read_timeout = max(0.1, _env_float("PROTECTION_TIMEOUT_S", TIMEOUT_S) * 0.6)

        @firestore.transactional
        def run(txn) -> str:  # no Gemini call in here: Firestore may run it twice (contention)
            snaps = {s.reference.id: s for s in txn.get_all([ctrl_ref, day_ref], retry=None, timeout=read_timeout)}
            ctrl = snaps.get(CONTROL_DOC)
            dsnap = snaps.get(day_ref.id)
            control = (ctrl.to_dict() if ctrl is not None and ctrl.exists else None) or {}
            d = (dsnap.to_dict() if dsnap is not None and dsnap.exists else None) or {}
            reason = decide(control, d, search, lim)
            if reason == OK:
                now = firestore.SERVER_TIMESTAMP
                txn.set(ctrl_ref, {"event_calls": int(control.get("event_calls") or 0) + 1,
                                   "event_searches": int(control.get("event_searches") or 0) + int(search),
                                   "updated_at": now}, merge=True)
                txn.set(day_ref, {"date": day, "calls": int(d.get("calls") or 0) + 1,
                                  "searches": int(d.get("searches") or 0) + int(search), "updated_at": now},
                        merge=True)
            return reason

        return run(client.transaction(max_attempts=2))

    def reserve(self, search: bool, lim: Limits, day: str) -> str:
        """Bounded to PROTECTION_TIMEOUT_S: a transaction still running after that is abandoned (it may still
        commit, counting a call that is not made: never the other way round)."""
        limit = _env_float("PROTECTION_TIMEOUT_S", TIMEOUT_S)
        t0 = time.monotonic()
        try:
            if not self._one.acquire(timeout=limit):
                raise TimeoutError("another reservation is still running")
            try:
                fut = self._pool.submit(self._transaction, search, lim, day)
            except BaseException:
                self._one.release()
                raise
            fut.add_done_callback(lambda _: self._one.release())
            return fut.result(timeout=max(0.0, limit - (time.monotonic() - t0)))
        except Exception as e:  # timeout, unavailable, permission denied, contention after 2 attempts
            log.warning("allowance backend failed: %s", type(e).__name__,
                        extra={"json_fields": {"event": "allowance_backend_error", "exception": type(e).__name__}})
            return BACKEND

    def warm_up(self) -> None:
        """Creates the client (and its channel) in the background, so the first request's transaction is fast."""
        def run():
            try:
                self.client().collection(COLLECTION).document(CONTROL_DOC).get(retry=None, timeout=5.0)
            except Exception as e:
                log.warning("allowance warm-up failed: %s", type(e).__name__)
        self._pool.submit(run)


class OffBackend:
    def reserve(self, search: bool, lim: Limits, day: str) -> str:
        return OK


_backend_lock = threading.Lock()
_backends: dict[str, object] = {}


def backend():
    name = backend_name()
    with _backend_lock:
        if name not in _backends:
            _backends[name] = {"firestore": FirestoreBackend, "memory": MemoryBackend, "off": OffBackend}[name]()
        return _backends[name]


def set_backend(name: str, instance) -> None:
    """Tests: replace the backend used for PROTECTION_BACKEND=name."""
    with _backend_lock:
        _backends[name] = instance


def reset() -> None:
    """Tests: fresh backends and slots."""
    with _backend_lock:
        _backends.clear()
    reset_slots()


def warm_up() -> None:
    if not degraded_switch() and backend_name() == "firestore":
        b = backend()
        if isinstance(b, FirestoreBackend):
            b.warm_up()


def admit(prompt: str, search: bool = False) -> str:
    """Called by every Gemini SDK wrapper right before the call is sent. OK: one call (and one search) reserved;
    otherwise the reason the call must not be made."""
    if degraded_switch():
        reason = DEGRADED
    elif len(prompt.encode("utf-8")) > env_int("GEMINI_INPUT_MAX_BYTES"):
        reason = TOO_LARGE
    else:
        reason = backend().reserve(search, Limits.from_env(), utc_day())
    if reason != OK:
        sc = _scope.get()
        if sc is not None:
            sc.refused.append(reason)
        log.info("gemini call refused: %s", reason,
                 extra={"json_fields": {"event": "allowance_refused", "allowance": reason, "search": search}})
    obs.note_allowance(reason)
    return reason
