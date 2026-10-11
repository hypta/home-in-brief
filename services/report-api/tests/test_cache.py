"""Codex F03 point 5: the /v1/report in-process cache (LRU, TTL, single flight). Sockets blocked."""
import copy
import threading

import pytest
from conftest import body
from fastapi.testclient import TestClient

from app import cache, gemini, main, protection
from test_api import GOOD, fake as fake_report
from test_protection import FakeGenai, counters, no_network, post_followup, report_brain  # noqa: F401
from test_followup import ans, cls, FLOOD_EN
from test_protection import followup_brain

client = TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def cache_on(monkeypatch):
    monkeypatch.setenv("REPORT_CACHE_SIZE", "100")
    monkeypatch.setenv("REPORT_CACHE_TTL_S", "600")


def test_identical_request_is_served_from_the_cache(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    first = client.post("/v1/report", json=body("case1"))
    again = client.post("/v1/report", json=body("case1"))
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()  # generated_at kept
    assert again.headers["x-report-mode"] == "gemini" and again.headers["cache-control"] == "no-store"
    assert len(fake.calls) == 1 and counters() == (1, 1, 0)


def test_key_is_canonical_and_covers_every_input_and_the_model(monkeypatch):
    b = body("case1")
    k = cache.key(b["property_context"], b["user_inputs"], b["rules_output"], "m")
    reordered = {k2: b["user_inputs"][k2] for k2 in reversed(list(b["user_inputs"]))}
    assert cache.key(b["property_context"], reordered, b["rules_output"], "m") == k
    changed = copy.deepcopy(b)
    changed["user_inputs"]["deposit"] = (changed["user_inputs"].get("deposit") or 0) + 1
    assert cache.key(changed["property_context"], changed["user_inputs"], changed["rules_output"], "m") != k
    changed = copy.deepcopy(b)
    changed["rules_output"]["warnings"] = ["x", *changed["rules_output"].get("warnings", [])]
    assert cache.key(changed["property_context"], changed["user_inputs"], changed["rules_output"], "m") != k
    assert cache.key(b["property_context"], b["user_inputs"], b["rules_output"], "other-model") != k


def test_drafts_and_degraded_reports_are_not_cached(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    monkeypatch.setenv("REPORT_DEGRADED", "true")
    assert client.post("/v1/report", json=body("case1")).headers["x-report-mode"] == "drafts"
    monkeypatch.delenv("REPORT_DEGRADED")
    assert client.post("/v1/report", json=body("case1")).headers["x-report-mode"] == "gemini"
    assert len(fake.calls) == 1


def test_errors_are_not_cached(monkeypatch):
    calls = []

    def broken(req, generate=None, debug=None):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "build_report", broken)
    assert client.post("/v1/report", json=body("case1")).status_code == 500
    assert client.post("/v1/report", json=body("case1")).status_code == 500
    assert len(calls) == 2 and not cache.REPORTS.flights


def test_mock_reports_are_not_cached(monkeypatch):
    monkeypatch.setenv("REPORT_MOCK", "true")
    client.post("/v1/report", json=body("case1"))
    assert not cache.REPORTS.entries


def test_web_search_inputs_are_never_cached(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    b = body("case1")
    b["user_inputs"] = b["user_inputs"] | {"chat_supplied": {"year_built": 1987},
                                           "chat_supplied_source": {"year_built": "web_search"}}
    client.post("/v1/report", json=b)
    client.post("/v1/report", json=b)
    assert len(fake.calls) == 2 and not cache.REPORTS.entries


def test_followups_are_never_cached(monkeypatch):
    fake = FakeGenai(followup_brain(cls(), [ans(FLOOD_EN)])).install(monkeypatch)
    post_followup("Is there a flood risk?")
    post_followup("Is there a flood risk?")
    assert len(fake.calls) == 4 and not cache.REPORTS.entries


def test_ttl_and_lru_limits(monkeypatch):
    now = [0.0]
    c = cache.ReportCache(clock=lambda: now[0])
    monkeypatch.setenv("REPORT_CACHE_SIZE", "2")
    monkeypatch.setenv("REPORT_CACHE_TTL_S", "600")
    for k in "abc":
        c.join(k)
        c.finish(k, {"k": k})
    assert c.get("a") is None and c.get("b") == {"k": "b"}  # a was the least recently used
    now[0] = 600.0
    assert c.get("b") is None and c.get("c") is None  # expired


def test_single_flight_duplicates_wait_for_the_leader(monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    inner = fake_report(GOOD)

    def slow(prompt, timeout_s):
        entered.set()
        gate.wait(5)
        return inner(prompt, timeout_s)

    fake = FakeGenai(lambda prompt, schema: slow(prompt, 20)).install(monkeypatch)
    out = []
    lead = threading.Thread(target=lambda: out.append(client.post("/v1/report", json=body("case1"))))
    lead.start()
    assert entered.wait(5)
    dups = [threading.Thread(target=lambda: out.append(client.post("/v1/report", json=body("case1"))))
            for _ in range(3)]
    for t in dups:
        t.start()
    gate.set()
    for t in [lead, *dups]:
        t.join(10)
    assert [r.status_code for r in out] == [200] * 4  # duplicates neither got busy nor called Gemini
    assert len(fake.calls) == 1 and len({r.json()["generated_at"] for r in out}) == 1


def test_leader_failure_releases_the_duplicates(monkeypatch):
    k = "x"
    leader, flight = cache.REPORTS.join(k)
    assert leader and cache.REPORTS.join(k)[0] is False
    cache.REPORTS.finish(k, None)
    assert cache.wait(flight, 0.1) is None and not cache.REPORTS.flights
