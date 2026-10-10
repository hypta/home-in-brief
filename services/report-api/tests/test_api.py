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


# Gemini rewrites the case1 fact list (F1 hazards, F2 lending, F3 First Home Loan, F4 household (optional),
# F5 title, F6 non-residential use); values are placeholders that code fills in.
GOOD = [
    {"text": "Council maps show Flood Plains covering {{F1.flood_plain_pct}} of this parcel, Flood Prone Areas "
             "{{F1.flood_prone_area_pct}}, and an overland flow path crossing it.", "fact_ids": ["F1"]},
    {"text": "Your loan counts as high-LVR lending (borrowing over {{F2.lvr_threshold}} of the price), which the "
             "Reserve Bank lets banks offer only in limited amounts.", "fact_ids": ["F2"]},
    {"text": "LINZ records the title as {{F5.title_type}}; Settled.govt.nz says you own the land and generally "
             "anything built on it.", "fact_ids": ["F5"]},
    {"text": "Your loan would be $720,000.", "fact_ids": ["F2"]},
]
GOOD_FILLED = [
    "Council maps show Flood Plains covering 51.2% of this parcel, Flood Prone Areas 50.8%, and an overland flow "
    "path crossing it.",
    "Your loan counts as high-LVR lending (borrowing over 80% of the price), which the Reserve Bank lets banks "
    "offer only in limited amounts.",
    "LINZ records the title as Freehold; Settled.govt.nz says you own the land and generally anything built on it.",
]
CASE1_FHL = "A First Home Loan is shown because your income is within Kāinga Ora's cap; it is exempt from Reserve Bank LVR limits."
CASE1_USE = ("The council valuation describes the improvements as RESERVE LAND and the land use as Recreational - "
             "vacant; the year built could not be checked.")


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


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
    # rewrites kept, the blocked one (a bare number) dropped, the uncovered must facts added as drafts, in fact order
    assert [s["text"] for s in rep["summary"]] == GOOD_FILLED[:2] + [CASE1_FHL, GOOD_FILLED[2], CASE1_USE]
    assert rep["summary"][0]["source_ids"] == ["ac_flood_plains", "ac_flood_prone", "ac_overland_flow"]
    assert rep["dropped"] == 1
    assert rep["next_steps"][0]["kb_item"] == "kb_rbnz_lvr"  # cited in the summary first
    assert rep["warnings"] == ["year_built_unavailable", "cladding_unavailable"]
    assert rep["generated_at"].endswith(("+13:00", "+12:00"))
    prompt, timeout = gen.calls[0]
    assert timeout <= 20
    # the prompt carries placeholders, never the values
    assert "{{F1.flood_plain_pct}}" in prompt and "{{F5.title_type}}" in prompt
    assert "51.2" not in prompt and "720000" not in prompt and "RESERVE LAND" not in prompt


def test_retry_after_invalid_json_then_success(monkeypatch):
    gen = fake("not json", GOOD)
    monkeypatch.setattr(gemini, "gemini_generate", gen)
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and len(gen.calls) == 2


def test_invalid_json_twice_is_502(monkeypatch):
    monkeypatch.setattr(gemini, "gemini_generate", fake("{}", "[1]"))
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 502 and r.json()["error"]["code"] == "model_output_invalid"


def test_zero_passing_sentences_retries_then_falls_back_to_drafts(monkeypatch):
    bad = [{"text": "Your loan would be $720,000.", "fact_ids": ["F2"]}]
    gen = fake(bad, bad)
    monkeypatch.setattr(gemini, "gemini_generate", gen)
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200
    assert len(gen.calls) == 2 and "bare_number" in gen.calls[1][0]
    rep = r.json()
    assert rep["dropped"] == 1 and len(rep["summary"]) == 5 and rep["summary"][2]["text"] == CASE1_FHL


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
    answer = [{"text": "LINZ records a {{F3.title_type}} title; Settled.govt.nz says owners automatically become "
                       "members of the body corporate.", "fact_ids": ["F3"]},
              {"text": "Your year falls in the same decade, and the cladding is monolithic, which Settled.govt.nz "
                       "discusses in its leaky-building guidance.", "fact_ids": ["F5"]}]
    monkeypatch.setattr(gemini, "gemini_generate", fake(answer))
    rep = client.post("/v1/report", json=body("case5")).json()
    texts = [s["text"] for s in rep["summary"]]
    assert texts[2] == ("LINZ records a Unit Title title; Settled.govt.nz says owners automatically become members "
                        "of the body corporate.")
    assert texts[4].startswith("Based on what you told us, your year falls in the same decade")
    assert sum(t.startswith("Based on") for t in texts) == 1


def test_no_loan_case_appends_fixed_sentence_and_drops_lending(monkeypatch):
    answer = [{"text": "A mapped overland flow path crosses this parcel, and Auckland Council says water can build up "
                       "and enter homes if the path is blocked.", "fact_ids": ["F1"]},
              {"text": "No loan is needed for this purchase.", "fact_ids": ["F3"]}]
    monkeypatch.setattr(gemini, "gemini_generate", fake(answer))
    rep = client.post("/v1/report", json=body("edge2")).json()
    texts = [s["text"] for s in rep["summary"]]
    assert texts[0] == answer[0]["text"] and texts[-1] == assemble.NO_LOAN_SENTENCES["deposit_exceeds_price"]
    assert len(texts) == 5 and not any("loan" in t.lower() for t in texts[:-1])  # F2-F4 drafts in between
    assert rep["summary"][-1]["source_ids"] == [] and rep["dropped"] == 1


def test_summary_capped_at_five(monkeypatch):
    many = [{"text": "LINZ records the title as {{F5.title_type}}; Settled.govt.nz says you own the land and "
                     "generally anything built on it.", "fact_ids": ["F5"]}] * 7
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
