import json

import pytest
from conftest import CASES, body, load_golden
from fastapi.testclient import TestClient

from app import assemble, gemini
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def fake(*answers):
    """A generator that returns each answer in turn (an Exception instance is raised instead)."""
    calls = []

    def gen(prompt, timeout_s):
        calls.append((prompt, timeout_s))
        a = answers[min(len(calls), len(answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps({"summary": a})

    gen.calls = calls
    return gen


GOOD = [
    {"text": "The council's Flood Plains layer covers 51.2% of this parcel.", "source_ids": ["ac_flood_plains"]},
    {"text": "Your loan counts as high-LVR lending, which the Reserve Bank lets banks offer only in limited amounts.",
     "source_ids": ["rbnz_lvr", "kb_rbnz_lvr"]},
    {"text": "LINZ records the title as Freehold.", "source_ids": ["linz_titles", "kb_title_freehold"]},
    {"text": "Your loan would be $720,000.", "source_ids": ["rbnz_lvr"]},
]


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_followup_stub():
    r = client.post("/v1/followup", json=body("case1"))
    assert r.status_code == 501 and r.json()["error"]["code"] == "not_implemented"


@pytest.mark.parametrize("payload", [{}, {"property_context": {}, "user_inputs": {}},
                                     {"property_context": [], "user_inputs": {}, "rules_output": {}}])
def test_bad_body_is_422(payload):
    r = client.post("/v1/report", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_report_with_model(monkeypatch):
    gen = fake(GOOD)
    monkeypatch.setattr(gemini, "gemini_generate", gen)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["model"] == "gemini-test" and rep["schema_version"] == "0.1"
    assert [s["text"] for s in rep["summary"]] == [s["text"] for s in GOOD[:3]]
    assert rep["dropped"] == 1
    assert rep["next_steps"][0]["kb_item"] == "kb_rbnz_lvr"  # cited in the summary first
    assert rep["warnings"] == ["year_built_unavailable", "cladding_unavailable"]
    assert rep["generated_at"].endswith(("+13:00", "+12:00"))
    prompt, timeout = gen.calls[0]
    assert timeout <= 20
    assert "720000" in prompt and "51.2" in prompt and "kb_rbnz_lvr" in prompt  # forbidden and allowed numbers listed
    assert "Freehold" in prompt


def test_retry_after_invalid_json_then_success(monkeypatch):
    gen = fake("not json", GOOD)
    monkeypatch.setattr(gemini, "gemini_generate", gen)
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and len(gen.calls) == 2


def test_invalid_json_twice_is_502(monkeypatch):
    monkeypatch.setattr(gemini, "gemini_generate", fake("{}", "[1]"))
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 502 and r.json()["error"]["code"] == "model_output_invalid"


def test_zero_passing_sentences_retries_then_502(monkeypatch):
    bad = [{"text": "Your loan would be $720,000.", "source_ids": ["rbnz_lvr"]}]
    gen = fake(bad, bad)
    monkeypatch.setattr(gemini, "gemini_generate", gen)
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 502 and r.json()["error"]["code"] == "validation_failed"
    assert len(gen.calls) == 2 and "computed_value" in gen.calls[1][0]


def test_model_unavailable_is_503(monkeypatch):
    monkeypatch.setattr(gemini, "gemini_generate", fake(gemini.ModelUnavailable("timeout")))
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_unavailable"


def test_missing_key_is_model_unavailable():
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_unavailable"


def test_no_retry_without_time(monkeypatch):
    import time

    def slow(prompt, timeout_s):
        time.sleep(0.3)
        return "not json"
    calls = []
    monkeypatch.setattr(gemini, "gemini_generate", lambda p, t: calls.append(1) or slow(p, t))
    monkeypatch.setenv("REQUEST_DEADLINE_S", "0.5")
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "0.4")
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code in (502, 503) and len(calls) <= 1


def test_kb_unavailable_is_503(monkeypatch):
    monkeypatch.setenv("KB_SOURCE", "rules-api")
    monkeypatch.setenv("RULES_API_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(gemini, "gemini_generate", fake(GOOD))
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "kb_unavailable"


def test_kb_from_rules_api(monkeypatch):
    from app import kb

    seen = []

    class Resp:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    local = kb._load_local(["kb_rbnz_lvr", "kb_title_freehold"])

    def get(url, params=None, headers=None, timeout=None):
        seen.append((url, params, headers))
        if url.endswith("/v1/sources"):
            return Resp([{"id": k, "title": v} for k, v in local.sources.items()])
        return Resp([local.items[i] for i in params["ids"].split(",") if i in local.items])

    monkeypatch.setenv("KB_SOURCE", "rules-api")
    monkeypatch.setenv("RULES_API_URL", "http://rules.test")
    monkeypatch.setattr(kb.requests, "get", get)
    data = kb.load(["kb_rbnz_lvr", "kb_title_freehold"])
    assert set(data.items) == {"kb_rbnz_lvr", "kb_title_freehold"} and "rbnz_lvr" in data.sources
    assert seen[1][0] == "http://rules.test/v1/kb/items" and seen[1][2] == {}
    kb.load(["kb_rbnz_lvr"])
    assert len(seen) == 2  # cached


def test_user_input_prefix_added_by_code(monkeypatch):
    answer = [{"text": "The cladding is monolithic, a system Settled.govt.nz discusses in its leaky-building guidance.",
               "source_ids": ["kb_monolithic_cladding"]},
              {"text": "LINZ records the title as Unit Title.", "source_ids": ["linz_titles"]}]
    monkeypatch.setattr(gemini, "gemini_generate", fake(answer))
    rep = client.post("/v1/report", json=body("case5")).json()
    assert rep["summary"][0]["text"].startswith("Based on what you told us, the cladding is monolithic")
    assert not rep["summary"][1]["text"].startswith("Based on")


def test_no_loan_case_appends_fixed_sentence_and_drops_lending(monkeypatch):
    answer = [{"text": "A mapped overland flow path crosses this parcel.", "source_ids": ["ac_overland_flow"]},
              {"text": "No loan is needed for this purchase.", "source_ids": ["linz_titles"]}]
    monkeypatch.setattr(gemini, "gemini_generate", fake(answer))
    rep = client.post("/v1/report", json=body("edge2")).json()
    assert [s["text"] for s in rep["summary"]] == [answer[0]["text"], assemble.NO_LOAN_SENTENCES["deposit_exceeds_price"]]
    assert rep["summary"][-1]["source_ids"] == [] and rep["dropped"] == 1


def test_summary_capped_at_five(monkeypatch):
    many = [{"text": "LINZ records the title as Freehold.", "source_ids": ["linz_titles"]}] * 7
    monkeypatch.setattr(gemini, "gemini_generate", fake(many))
    assert len(client.post("/v1/report", json=body("case1")).json()["summary"]) == 5


@pytest.mark.parametrize("case", CASES)
def test_mock_returns_golden_summary(case, monkeypatch):
    monkeypatch.setenv("REPORT_MOCK", "true")
    monkeypatch.setattr(gemini, "gemini_generate", fake(gemini.ModelUnavailable("must not be called")))
    r = client.post("/v1/report", json=body(case))
    assert r.status_code == 200, r.text
    rep = r.json()
    golden = [s["text"] for s in load_golden("claude", case)["summary"]]
    assert rep["model"] == "mock"
    assert all(s["text"] in golden or s["source_ids"] == [] for s in rep["summary"])
    assert len(rep["summary"]) >= 3 and len(rep["next_steps"]) <= 6


def test_mock_unknown_input(monkeypatch):
    monkeypatch.setenv("REPORT_MOCK", "true")
    b = body("case1")
    b["user_inputs"]["deposit"] = 12345
    rep = client.post("/v1/report", json=b).json()
    assert rep["summary"] == [{"text": assemble.MOCK_SENTENCE, "source_ids": []}]
    assert rep["next_steps"] and rep["not_found_notes"]
