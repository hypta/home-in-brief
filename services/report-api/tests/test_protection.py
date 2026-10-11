"""Codex F03: generation slots, the global Gemini allowance, REPORT_DEGRADED, prompt/output caps.

Every test here runs with sockets blocked: any network call (Gemini, Firestore, rules-api) fails the test. The real
SDK wrappers (gemini.gemini_generate, followup.gemini_json, followup.gemini_search) run against a fake
google.genai.Client, so the allowance gate inside them is exercised, not bypassed.
"""
import ast
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import body
from fastapi.testclient import TestClient

from app import followup, gemini, main, obs, protection
from app.followup import FollowupRequest
from b_common.report_schemas import Report
from test_api import GOOD, fake as fake_report
from test_followup import FLOOD_EN, ans, cls, fake as fake_followup

client = TestClient(main.app, raise_server_exceptions=False)
APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError(f"unexpected network call: {a[1:] if len(a) > 1 else a}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("rules-api must not be called"))


class FakeGenai:
    """Stands in for google.genai.Client. `brain(prompt, schema)` answers JSON calls; a call with the Search tool
    gets a grounded answer (or `search_reply`)."""

    def __init__(self, brain=None):
        self.brain = brain
        self.calls: list[dict] = []
        self.http_options = []

    def install(self, monkeypatch):
        from google import genai
        fake = self

        class Client:
            def __init__(self, api_key=None, http_options=None, **kw):
                fake.http_options.append(http_options)
                self.models = self

            def generate_content(self, model, contents, config):
                fake.calls.append({"prompt": contents, "config": config})
                return fake.reply(contents, config)

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(genai, "Client", Client)
        return self

    def reply(self, prompt, config):
        from google.genai import types
        if config.tools:
            chunk = types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://redirect.invalid/1", title="x.nz"))
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text="Zoned for Example School.")]),
                grounding_metadata=types.GroundingMetadata(
                    grounding_chunks=[chunk], search_entry_point=types.SearchEntryPoint(rendered_content="<div/>")))])
        text = self.brain(prompt, config.response_schema)
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=text)]))])

    @property
    def searches(self):
        return sum(bool(c["config"].tools) for c in self.calls)


def report_brain(*answers):
    gen = fake_report(*answers)
    return lambda prompt, schema: gen(prompt, 20)


def followup_brain(*a, **k):
    gen = fake_followup(*a, **k)
    return lambda prompt, schema: gen(prompt, schema, 20)


def counters():
    """The memory backend's documents: (event_calls, today's calls, today's searches)."""
    b = protection.backend()
    d = b.days.get(protection.utc_day(), {})
    return b.control.get("event_calls", 0), d.get("calls", 0), d.get("searches", 0)


def post_followup(question, case="case1", **extra):
    return client.post("/v1/followup", json=body(case) | {"question": question, "history": []} | extra)


# ---- generation slots ------------------------------------------------------------------------------------------
def test_third_concurrent_request_is_busy_and_health_is_not(monkeypatch):
    started, release = threading.Barrier(3), threading.Event()

    def slow_report(req, generate=None, debug=None, kbd=None):
        started.wait(5)
        release.wait(5)
        raise RuntimeError("stop here")  # the slot must still be released

    monkeypatch.setattr(main, "build_report", slow_report)
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.post("/v1/report", json=body("case1"))))
               for _ in range(2)]
    for t in threads:
        t.start()
    started.wait(5)  # both requests hold a slot
    try:
        busy = client.post("/v1/report", json=body("case1"))
        busy_f = post_followup("Is there a flood risk?")
        health = client.get("/health")
    finally:
        release.set()
        for t in threads:
            t.join(5)
    for r in (busy, busy_f):
        assert r.status_code == 503
        assert r.json() == {"error": {"code": "busy", "message": r.json()["error"]["message"], "fields": []}}
        assert r.headers["retry-after"] == "5" and r.headers["cache-control"] == "no-store"
    assert health.status_code == 200
    assert [r.status_code for r in results] == [500, 500]
    # released in finally after the exceptions: both slots are free again
    sem = protection._semaphore()
    assert sem.acquire(blocking=False) and sem.acquire(blocking=False)
    sem.release(), sem.release()


def test_generation_slot_releases_on_exception_and_reads_the_env(monkeypatch):
    monkeypatch.setenv("GENERATION_SLOTS", "1")
    protection.reset_slots()
    with pytest.raises(ValueError):
        with protection.generation_slot():
            with pytest.raises(protection.Busy):
                with protection.generation_slot():
                    pass
            raise ValueError
    with protection.generation_slot():  # free again
        pass


def test_busy_is_in_the_completion_record(monkeypatch, caplog):
    monkeypatch.setenv("GENERATION_SLOTS", "1")
    protection.reset_slots()
    caplog.set_level("INFO")
    with protection.generation_slot():
        r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 503
    rec = [x.json_fields for x in caplog.records if getattr(x, "json_fields", {}).get("event") == "request_complete"]
    assert rec[-1]["busy"] is True and rec[-1]["error"] == "busy" and rec[-1]["status"] == 503
    assert any(getattr(x, "json_fields", {}).get("event") == "busy_rejected" for x in caplog.records)


# ---- allowance decisions ---------------------------------------------------------------------------------------
def test_daily_allowance_boundary_and_utc_day_reset(monkeypatch):
    lim = protection.Limits(daily=3, search=1, event=100)
    b = protection.MemoryBackend()
    assert [b.reserve(False, lim, "2026-10-18") for _ in range(4)] == ["ok", "ok", "ok", protection.DAILY]
    assert b.days["2026-10-18"]["calls"] == 3  # the refused call is not counted
    assert b.reserve(False, lim, "2026-10-19") == "ok"  # a new UTC day


def test_event_allowance_spans_days():
    lim = protection.Limits(daily=3, search=1, event=5)
    b = protection.MemoryBackend()
    assert [b.reserve(False, lim, "2026-10-18") for _ in range(3)] == ["ok"] * 3
    assert [b.reserve(False, lim, "2026-10-19") for _ in range(3)] == ["ok", "ok", protection.EVENT]
    assert b.control["event_calls"] == 5


def test_search_allowance_stops_only_searches():
    lim = protection.Limits(daily=10, search=1, event=100)
    b = protection.MemoryBackend()
    assert b.reserve(True, lim, "d") == "ok"
    assert b.reserve(True, lim, "d") == protection.SEARCH
    assert b.reserve(False, lim, "d") == "ok"
    assert b.days["d"] == {"date": "d", "calls": 2, "searches": 1}  # a search counts in calls too


def test_disabled_control_flag():
    b = protection.MemoryBackend()
    b.control["disabled"] = True
    assert b.reserve(False, protection.Limits(10, 10, 10), "d") == protection.DISABLED


def test_env_defaults(monkeypatch):
    assert protection.Limits.from_env() == protection.Limits(daily=100, search=30, event=3000)
    assert protection.max_output_tokens() == 2048 and protection.env_int("GEMINI_INPUT_MAX_BYTES") == 32000
    assert protection.env_int("GENERATION_SLOTS") == 2
    monkeypatch.delenv("PROTECTION_BACKEND")
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert protection.backend_name() == "memory"
    monkeypatch.setenv("K_SERVICE", "report-api")  # Cloud Run
    assert protection.backend_name() == "firestore"
    monkeypatch.setenv("PROTECTION_BACKEND", "bogus")
    assert protection.backend_name() == "firestore"


def test_utc_day_is_the_server_utc_date():
    from datetime import datetime, timedelta, timezone
    nz_morning = datetime(2026, 10, 19, 8, 0, tzinfo=timezone(timedelta(hours=13)))  # 18 Oct 19:00 UTC
    assert protection.utc_day(nz_morning.astimezone(timezone.utc)) == "2026-10-18"


# ---- Firestore backend: errors refuse the call -----------------------------------------------------------------
def test_firestore_error_refuses_the_call(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    monkeypatch.setenv("PROTECTION_BACKEND", "firestore")

    def broken():
        raise RuntimeError("firestore unavailable")

    protection.set_backend("firestore", protection.FirestoreBackend(client_factory=broken))
    with pytest.raises(gemini.ModelUnavailable) as e:
        gemini.gemini_generate("prompt", 15)
    assert e.value.category == obs.ALLOWANCE and not fake.calls


def test_slow_firestore_is_abandoned_within_the_timeout(monkeypatch):
    monkeypatch.setenv("PROTECTION_TIMEOUT_S", "0.2")

    def slow():
        time.sleep(1.5)
        raise RuntimeError("late")

    b = protection.FirestoreBackend(client_factory=slow)
    t0 = time.monotonic()
    assert b.reserve(False, protection.Limits(10, 10, 10), "d") == protection.BACKEND
    assert time.monotonic() - t0 < 1.0


def test_firestore_error_report_is_drafts(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    monkeypatch.setenv("PROTECTION_BACKEND", "firestore")
    protection.set_backend("firestore", protection.FirestoreBackend(client_factory=lambda: 1 / 0))
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and r.headers["x-report-mode"] == "drafts"
    assert r.json()["warnings"][-1] == protection.GENERATION_DEGRADED and not fake.calls


@pytest.mark.skipif(not os.environ.get("FIRESTORE_EMULATOR_HOST"), reason="needs the Firestore emulator")
def test_firestore_emulator_transactions(monkeypatch):
    """Real transactions against the emulator (make up): boundary, search, concurrency, disabled flag."""
    from google.cloud import firestore

    monkeypatch.undo()  # this test talks to the emulator on localhost
    project = f"protection-test-{time.time_ns()}"
    db = firestore.Client(project=project)
    b = protection.FirestoreBackend(client_factory=lambda: db)
    lim = protection.Limits(daily=5, search=1, event=6)
    out = []
    threads = [threading.Thread(target=lambda: out.append(b.reserve(False, lim, "2026-10-18"))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    day = db.collection("protection").document("daily-2026-10-18").get().to_dict()
    assert day["calls"] == out.count("ok") == 5  # never over the limit; reservations queue in the process
    assert set(out) <= {"ok", protection.DAILY, protection.BACKEND}
    # two instances at once: contention may refuse calls (backend_error), never let one over the limit
    b2 = protection.FirestoreBackend(client_factory=lambda: firestore.Client(project=project))
    out = []
    threads = [threading.Thread(target=lambda x=x: out.append(x.reserve(False, protection.Limits(3, 1, 100), "d2")))
               for x in (b, b2) * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(3)  # abandoned transactions finish (they may still commit: counted, never refunded)
    calls = (db.collection("protection").document("daily-d2").get().to_dict() or {}).get("calls", 0)
    assert calls >= out.count("ok") and calls <= 3
    lim = protection.Limits(daily=5, search=1, event=100)
    assert b.reserve(True, lim, "2026-10-19") == "ok"
    assert b.reserve(True, lim, "2026-10-19") == protection.SEARCH
    assert b.reserve(False, protection.Limits(daily=5, search=1, event=0), "2026-10-19") == protection.EVENT
    db.collection("protection").document("control").set({"disabled": True}, merge=True)
    assert b.reserve(False, lim, "2026-10-20") == protection.DISABLED


# ---- every SDK call is gated -----------------------------------------------------------------------------------
def test_every_generate_content_call_site_passes_the_gate():
    """Static check: each function in app/ that calls the SDK's generate_content also calls admit() first."""
    sites = []
    for path in APP.glob("*.py"):
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, ast.FunctionDef):
                continue
            calls = [(n, getattr(n.func, "attr", getattr(n.func, "id", ""))) for n in ast.walk(fn)
                     if isinstance(n, ast.Call)]
            gen = [n for n, name in calls if name == "generate_content"]
            if gen:
                sites.append(f"{path.name}:{fn.name}")
                admit = [n for n, name in calls if name == "admit"]
                assert admit and admit[0].lineno < gen[0].lineno, f"{path.name}:{fn.name} is not gated"
    assert sorted(sites) == ["followup.py:gemini_json", "followup.py:gemini_search", "gemini.py:gemini_generate"]


def test_each_wrapper_reserves_one_call(monkeypatch):
    fake = FakeGenai(lambda prompt, schema: json.dumps({"texts": ["x"]})).install(monkeypatch)
    fake.brain = lambda prompt, schema: json.dumps({"summary": []})
    gemini.gemini_generate("p", 15)
    assert counters() == (1, 1, 0)
    fake.brain = lambda prompt, schema: json.dumps({"texts": ["x"]})
    followup.gemini_json("p", followup.TranslateOut, 15)
    assert counters() == (2, 2, 0)
    assert followup.gemini_search("p", 15) is not None
    assert counters() == (3, 3, 1)
    assert len(fake.calls) == 3


def test_output_token_cap_and_single_attempt(monkeypatch):
    fake = FakeGenai(lambda prompt, schema: json.dumps({"summary": []})).install(monkeypatch)
    gemini.gemini_generate("p", 15)
    followup.gemini_json("p", followup.TranslateOut, 15)
    followup.gemini_search("p", 15)
    assert [c["config"].max_output_tokens for c in fake.calls] == [2048] * 3
    assert [o.retry_options.attempts for o in fake.http_options] == [1] * 3
    monkeypatch.setenv("GEMINI_MAX_OUTPUT_TOKENS", "512")
    gemini.gemini_generate("p", 15)
    assert fake.calls[-1]["config"].max_output_tokens == 512


def test_report_calls_are_all_counted(monkeypatch):
    fake = FakeGenai(report_brain("not json", GOOD)).install(monkeypatch)  # invalid JSON, then the retry
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and r.headers["x-report-mode"] == "gemini"
    assert len(fake.calls) == 2 and counters() == (2, 2, 0)
    assert protection.GENERATION_DEGRADED not in r.json()["warnings"]


def test_followup_calls_and_searches_are_all_counted(monkeypatch):
    fake = FakeGenai(followup_brain(cls(out_of_scope=True, searchable=True))).install(monkeypatch)
    r = post_followup("Which school zone is this in?")
    assert r.status_code == 200 and r.json()["web"]["text"] == "Zoned for Example School."
    assert len(fake.calls) == 2 and fake.searches == 1 and counters() == (2, 2, 1)


def test_followup_translation_is_counted(monkeypatch):
    fake = FakeGenai(followup_brain(cls(language="ja", out_of_scope=True, searchable=False))).install(monkeypatch)
    r = post_followup("この家を買うべきですか？")
    assert r.status_code == 200
    assert len(fake.calls) == 2 and counters() == (2, 2, 0)  # classify + refusal translation


# ---- allowance used up / REPORT_DEGRADED / prompt size ---------------------------------------------------------
def test_daily_allowance_exhausted_report_is_drafts(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "1")
    assert client.post("/v1/report", json=body("case1")).headers["x-report-mode"] == "gemini"
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and r.headers["x-report-mode"] == "drafts"
    assert r.json()["warnings"][-1] == protection.GENERATION_DEGRADED
    Report.model_validate(r.json())
    assert len(fake.calls) == 1 and counters() == (1, 1, 0)


def test_event_allowance_exhausted(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    monkeypatch.setenv("GEMINI_EVENT_CALLS", "0")
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and r.json()["warnings"][-1] == protection.GENERATION_DEGRADED and not fake.calls


@pytest.mark.parametrize("how", ["switch", "exhausted"])
def test_degraded_report(monkeypatch, caplog, how):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    if how == "switch":
        monkeypatch.setenv("REPORT_DEGRADED", "true")
        monkeypatch.setenv("PROTECTION_BACKEND", "firestore")  # never read with the switch on
        protection.set_backend("firestore", protection.FirestoreBackend(client_factory=lambda: pytest.fail("read")))
    else:
        monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    caplog.set_level("INFO")
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and r.headers["x-report-mode"] == "drafts" and not fake.calls
    out = r.json()
    assert out["warnings"][-1] == protection.GENERATION_DEGRADED and out["summary"] and out["model"]
    assert set(out) == set(Report.model_fields)
    rec = [x.json_fields for x in caplog.records if getattr(x, "json_fields", {}).get("event") == "request_complete"]
    assert rec[-1]["allowance"] == ("degraded" if how == "switch" else protection.DAILY)
    assert rec[-1]["gemini_calls"] == 0 and rec[-1]["mode"] == "drafts"


@pytest.mark.parametrize("extra", [{}, {"search_field": "year_built", "question": ""}])
def test_degraded_followup_no_search_no_values_no_translation(monkeypatch, extra):
    fake = FakeGenai(followup_brain(cls(year_built="1987"))).install(monkeypatch)
    monkeypatch.setenv("REPORT_DEGRADED", "true")
    b = body("edge1") if extra else body("case1")
    r = client.post("/v1/followup", json=b | {"question": "The house was built in 1987. Flood risk?", "history": []}
                    | extra)
    assert r.status_code == 200, r.text
    out = r.json()
    assert not fake.calls and out["warnings"] == [protection.GENERATION_DEGRADED]
    assert out["user_supplied"] == {} and "web" not in out and out["answer"]


def test_degraded_followup_in_japanese_stays_english(monkeypatch):
    fake = FakeGenai(followup_brain(cls(language="ja"))).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    r = post_followup("洪水のリスクはありますか？")
    assert r.status_code == 200 and not fake.calls
    assert r.json()["warnings"] == [protection.GENERATION_DEGRADED]
    assert r.json()["language"] == "en"  # Codex R6: no Japanese sentences written by code, so said to be English


def test_search_allowance_exhausted_only_skips_the_search(monkeypatch, caplog):
    fake = FakeGenai(followup_brain(cls(out_of_scope=True, searchable=True))).install(monkeypatch)
    monkeypatch.setenv("SEARCH_DAILY_CALLS", "0")
    caplog.set_level("INFO")
    r = post_followup("Which school zone is this in?")
    assert r.status_code == 200
    out = r.json()
    assert out["answer"][0]["text"] == followup.REFUSAL_EN and "web" not in out
    assert out["warnings"] == []  # the model still answered: not degraded
    assert len(fake.calls) == 1 and fake.searches == 0 and counters() == (1, 1, 0)
    rec = [x.json_fields for x in caplog.records if getattr(x, "json_fields", {}).get("event") == "request_complete"]
    assert rec[-1]["allowance"] == protection.SEARCH


def test_in_scope_followup_still_answers_with_search_exhausted(monkeypatch):
    fake = FakeGenai(followup_brain(cls(), [ans(FLOOD_EN)])).install(monkeypatch)
    monkeypatch.setenv("SEARCH_DAILY_CALLS", "0")
    r = post_followup("Is there a flood risk?")
    assert r.status_code == 200 and r.headers["x-report-mode"] == "gemini" and r.json()["warnings"] == []
    assert len(fake.calls) == 2


def test_oversized_prompt_is_not_sent_nor_counted(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    monkeypatch.setenv("GEMINI_INPUT_MAX_BYTES", "100")
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and r.json()["warnings"][-1] == protection.GENERATION_DEGRADED
    assert not fake.calls and counters() == (0, 0, 0)


def test_largest_legitimate_followup_prompts_fit_the_default_cap(monkeypatch):
    """A maximal question (1,000 CJK characters) and history (6 x 2,000) stay under GEMINI_INPUT_MAX_BYTES."""
    sizes = []
    inner = fake_followup(cls(language="zh-Hant"), [ans(FLOOD_EN)])

    def gen(prompt, schema, timeout_s):
        sizes.append(len(prompt.encode("utf-8")))
        return inner(prompt, schema, timeout_s)

    q = ("洪水" * 499) + "？"
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": "淹水風險" * 500} for i in range(6)]
    for case in ("case1", "case2", "edge3"):
        req = FollowupRequest.model_validate(body(case) | {"question": q, "history": history})
        followup.answer_followup(req, generate=gen)
    assert sizes and max(sizes) < protection.env_int("GEMINI_INPUT_MAX_BYTES")


def test_completion_record_fields():
    st = obs.Stats(request_id="r", endpoint="report")
    f = obs.completion_fields(st, 200)
    assert f["allowance"] == "ok" and f["busy"] is False
