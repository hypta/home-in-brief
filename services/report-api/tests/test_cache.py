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

    def broken(req, generate=None, debug=None, kbd=None):
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


# ---- Codex R2: a stable key (timestamps out) and KB invalidation; R4: unknown provenance is not cached -----------
def _later(b, n=1):
    """The same inputs computed again later: rules-api's and property-api's generated_at, sources' accessed_at."""
    b = copy.deepcopy(b)
    b["rules_output"]["generated_at"] = f"2026-10-1{n}T09:12:2{n}+13:00"
    b["property_context"]["generated_at"] = f"2026-10-1{n}T22:11:5{n}+13:00"
    for s in b["property_context"].get("sources", []):
        s["accessed_at"] = f"2026-10-1{n}"
    return b


def test_only_computation_timestamps_differ_cache_hit(monkeypatch):
    """web -> rules -> report twice in different seconds: identical inputs except when they were computed."""
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    first = client.post("/v1/report", json=_later(body("case1"), 1))
    again = client.post("/v1/report", json=_later(body("case1"), 2))
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json() and len(fake.calls) == 1 and counters() == (1, 1, 0)


def test_key_keeps_dates_warnings_amounts_sources_and_order():
    b = body("case1")
    k = lambda x: cache.key(x["property_context"], x["user_inputs"], x["rules_output"], "m", "kb")  # noqa: E731
    base = k(b)
    assert k(_later(b)) == base
    for change in (
        lambda x: x["rules_output"].__setitem__("rates_snapshot_date", "2026-10-01"),
        lambda x: x["rules_output"]["scenarios"][0]["per_bank"][0].__setitem__("captured_at", "2026-10-01T00:00:00+13:00"),
        lambda x: x["rules_output"]["scenarios"][0]["per_bank"].reverse(),
        lambda x: x["rules_output"].__setitem__("warnings", ["x"]),
        lambda x: x["rules_output"].__setitem__("loan_amount", (x["rules_output"].get("loan_amount") or 0) + 1),
        lambda x: x["property_context"]["sources"][0].__setitem__("title", "Another title"),
        lambda x: x["property_context"]["hazards"][0].__setitem__("data_date", "2026-01-01"),
    ):
        changed = copy.deepcopy(b)
        change(changed)
        assert k(changed) != base
    assert cache.key(b["property_context"], b["user_inputs"], b["rules_output"], "m", "other-kb") != base


def test_timestamps_left_out_of_the_key_are_never_read_by_report_api():
    """TIMESTAMP_PATHS is safe only while app/ never reads those fields: the report cannot depend on them."""
    import re
    from pathlib import Path
    app = Path(cache.__file__).resolve().parent
    for path in app.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "cache.py":
            continue
        assert "accessed_at" not in text, path.name
        uses = re.findall(r".*generated_at.*", text)
        assert all("generated_at=datetime.now" in u for u in uses), (path.name, uses)


def test_kb_change_invalidates_the_cache(monkeypatch, tmp_path):
    import json as _json
    import shutil
    from conftest import REPO
    from app import kb
    seed = tmp_path / "seed"
    shutil.copytree(REPO / "kb" / "seed", seed)
    monkeypatch.setenv("KB_DIR", str(seed))
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    assert client.post("/v1/report", json=body("case1")).headers["x-report-mode"] == "gemini"
    assert client.post("/v1/report", json=body("case1")).status_code == 200
    assert len(fake.calls) == 1  # unchanged KB: a hit
    req_ids = set(conftest_request("case1").selected_kb_ids())
    for p in sorted(seed.glob("*.json")):
        data = _json.loads(p.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else [data]
        hit = [r for r in rows if isinstance(r, dict) and r.get("id") in req_ids and r.get("claim_en")]
        if hit:
            hit[0]["claim_en"] += " Updated."
            p.write_text(_json.dumps(data), encoding="utf-8")
            break
    else:
        pytest.fail("no selected KB item found in the seed copy")
    kb.clear_cache()
    assert client.post("/v1/report", json=body("case1")).status_code == 200
    assert len(fake.calls) == 2  # the KB changed: the cached wording is not reused


def conftest_request(case):
    from conftest import request
    return request(case)


@pytest.mark.parametrize("ui,ok", [
    ({}, True),
    ({"chat_supplied": {"year_built": 1987}}, False),                                       # source missing
    ({"chat_supplied": {"year_built": 1987}, "chat_supplied_source": {}}, False),           # does not match
    ({"chat_supplied": {"year_built": 1987, "cladding": "brick"},
      "chat_supplied_source": {"year_built": "user"}}, False),                              # one field unknown
    ({"chat_supplied": {"year_built": 1987}, "chat_supplied_source": {"year_built": "user"}}, True),
    ({"chat_supplied": {"year_built": 1987}, "chat_supplied_source": {"year_built": "web_search"}}, False),
])
def test_chat_supplied_provenance_must_be_known_to_cache(ui, ok):
    assert cache.cacheable({}, ui) is ok


def test_unknown_provenance_request_is_not_stored(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    b = body("case1")
    b["user_inputs"] = b["user_inputs"] | {"chat_supplied": {"year_built": 1987}}
    client.post("/v1/report", json=b)
    client.post("/v1/report", json=b)
    assert len(fake.calls) == 2 and not cache.REPORTS.entries
