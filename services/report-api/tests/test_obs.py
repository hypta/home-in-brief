"""F07: one JSON completion record per request, without the question, history, prompts or financial inputs."""
import json
import logging

import pytest
from conftest import body
from fastapi.testclient import TestClient

from app import followup, gemini, obs
from app.main import app
from test_api import GOOD, fake as fake_report
from test_followup import FLOOD_EN, cls, ans, fake as fake_followup

client = TestClient(app, raise_server_exceptions=False)
QUESTION = "Is there a flood risk at my secret address 42 Example Lane?"


def records(caplog, event="request_complete"):
    return [r for r in caplog.records if getattr(r, "json_fields", {}).get("event") == event]


def financial_numbers(b):
    return [str(int(v)) for v in b["user_inputs"].values() if isinstance(v, (int, float)) and v > 100]


def counted(gen, search=False):
    """The fakes replace the real Gemini functions, which count the calls; count here instead."""
    def wrapped(*a):
        obs.note_call(search)
        return gen(*a)
    return wrapped


def assert_clean(caplog, b, *secrets):
    text = "\n".join(obs.JsonFormatter().format(r) for r in caplog.records)
    for s in [*secrets, *financial_numbers(b)]:
        assert s not in text, s


def test_report_completion_record(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(gemini, "gemini_generate", counted(fake_report(GOOD)))
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    b = body("case1")
    r = client.post("/v1/report", json=b, headers={"X-Cloud-Trace-Context": "abc123def/456;o=1"})
    assert r.status_code == 200
    [rec] = records(caplog)
    f = rec.json_fields
    assert {"endpoint", "status", "elapsed_ms", "model", "gemini_calls", "searches", "error", "sentences",
            "remaining_s", "mode"} <= set(f)
    assert f["endpoint"] == "report" and f["status"] == 200 and f["model"] == "gemini-test"
    assert f["gemini_calls"] == 1 and f["searches"] == 0 and f["error"] == "none" and f["mode"] == "gemini"
    assert f["sentences"]["passed"] == 3 and f["sentences"]["blocked"] == 1 and f["sentences"]["drafts"] >= 1
    assert 0 < f["remaining_s"] <= 25
    line = json.loads(obs.JsonFormatter().format(rec))
    assert line["severity"] == "INFO" and line["request_id"] == "abc123def"
    assert_clean(caplog, b, "51.2", "RESERVE LAND", "{{F1")


def test_followup_completion_record(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(followup, "gemini_json", counted(fake_followup(cls(), [ans(FLOOD_EN)])))
    b = body("case1") | {"question": QUESTION, "history": [{"role": "user", "text": "my income is 130000"}]}
    r = client.post("/v1/followup", json=b, headers={"X-Request-Id": "req-1"})
    assert r.status_code == 200, r.text
    [rec] = records(caplog)
    f = rec.json_fields
    assert f["endpoint"] == "followup" and f["status"] == 200 and f["language"] == "en"
    assert f["gemini_calls"] == 2 and f["mode"] == "gemini" and f["sentences"]["passed"] == 1
    assert json.loads(obs.JsonFormatter().format(rec))["request_id"] == "req-1"
    assert_clean(caplog, b, "secret address", "Example Lane", "my income", "130000")


def test_validation_error_is_logged_too(caplog):
    caplog.set_level(logging.INFO)
    assert client.post("/v1/followup", json={"question": QUESTION}).status_code == 422
    [rec] = records(caplog)
    assert rec.json_fields["status"] == 422 and rec.json_fields["mode"] == "drafts"
    assert len(json.loads(obs.JsonFormatter().format(rec))["request_id"]) == 32  # generated


@pytest.mark.parametrize("exc,category", [
    (TimeoutError("The read operation timed out"), "timeout"),
    (RuntimeError("429 RESOURCE_EXHAUSTED. Quota exceeded"), "quota"),
    (RuntimeError("400 INVALID_ARGUMENT. API key not valid"), "http_4xx"),
    (RuntimeError("503 UNAVAILABLE"), "http_5xx"),
    (RuntimeError("connection reset"), "other"),
])
def test_gemini_failure_warning_has_category_and_no_key(caplog, exc, category):
    key = "AIzaSECRETKEY123"
    exc.args = (f"{exc.args[0]} key={key}",)
    with caplog.at_level(logging.WARNING):
        msg, got = obs.gemini_failed(logging.getLogger("report-api.gemini"), "gemini call", exc)
    assert key not in msg and got == category
    [rec] = caplog.records
    assert rec.levelname == "WARNING" and rec.json_fields["error"] == category
    assert key not in obs.JsonFormatter().format(rec)


def test_hooks_are_noops_outside_a_request():
    obs.note_call(), obs.note_error("schema"), obs.track_budget(object())
    assert obs.current() is None and obs.attach({"x": 1}) == {"x": 1}


def test_model_failure_is_logged_as_drafts(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def quota(prompt, timeout_s):
        obs.note_call()
        obs.note_error(obs.QUOTA)  # what gemini_generate records through obs.gemini_failed
        raise gemini.ModelUnavailable("429", obs.QUOTA)

    monkeypatch.setattr(gemini, "gemini_generate", quota)
    assert client.post("/v1/report", json=body("case1")).status_code == 200
    f = records(caplog)[0].json_fields
    assert f["mode"] == "drafts" and f["error"] == "quota" and f["gemini_calls"] == 1
    assert f["sentences"]["passed"] == 0 and f["sentences"]["drafts"] >= 1


def test_report_mode_header(monkeypatch):
    monkeypatch.setattr(gemini, "gemini_generate", fake_report(GOOD))
    r = client.post("/v1/report", json=body("case1"))
    assert r.headers["X-Report-Mode"] == "gemini"
    assert "mode" not in r.json()  # the JSON contract is unchanged
    monkeypatch.setattr(gemini, "gemini_generate", fake_report(gemini.ModelUnavailable("x", "quota")))
    assert client.post("/v1/report", json=body("case1")).headers["X-Report-Mode"] == "drafts"
    monkeypatch.setenv("REPORT_MOCK", "true")
    assert client.post("/v1/report", json=body("case1")).headers["X-Report-Mode"] == "mock"


def test_followup_mode_header(monkeypatch):
    monkeypatch.setattr(followup, "gemini_json", fake_followup(cls(), [ans(FLOOD_EN)]))
    r = client.post("/v1/followup", json=body("case1") | {"question": "Flood risk?", "history": []})
    assert r.status_code == 200 and r.headers["X-Report-Mode"] == "gemini"


# ---- Codex R7: no exception message reaches the logs ------------------------------------------------------------
SENTINELS = ("household income 130000", "12 Sentinel Street, Remuera", "is my house a good buy")


class LeakyError(Exception):
    """A provider error whose message echoes request content (the hypothesis in Codex R7)."""

    def __init__(self, code=500):
        super().__init__("; ".join(SENTINELS))
        self.code = code
        self.response = type("Resp", (), {"headers": {"x-goog-request-id": "req-123 <x>"}})()


def all_log_text(caplog, capsys=None):
    fmt_json, fmt_plain = obs.JsonFormatter(), logging.Formatter("%(message)s")
    out = []
    for r in caplog.records:
        out.append(fmt_json.format(r))
        out.append(fmt_plain.format(r))  # also what a plain handler (uvicorn's) would print
    if capsys is not None:
        cap = capsys.readouterr()
        out += [cap.out, cap.err]
    return "\n".join(out)


def test_sentinel_sdk_error_logs_class_category_status_and_request_id_only(monkeypatch, caplog, capsys):
    from test_protection import FakeGenai

    def leaky(prompt, schema):
        raise LeakyError(500)

    FakeGenai(leaky).install(monkeypatch)
    caplog.set_level(logging.INFO)
    r = client.post("/v1/report", json=body("case1"))
    assert r.status_code == 200 and r.headers["X-Report-Degraded-Reason"] == "model_error"
    text = all_log_text(caplog, capsys)
    for s in SENTINELS:
        assert s not in text
    warns = [x for x in caplog.records if x.levelname == "WARNING" and "failed" in x.getMessage()]
    assert warns and all(w.json_fields == warns[0].json_fields for w in warns)  # the 5xx retry logs the same
    assert warns[0].json_fields == {"error": "http_5xx", "exception": "LeakyError", "http_status": 500,
                                "provider_request_id": "req-123x"}


def test_sentinel_unhandled_exception_is_logged_without_its_message(monkeypatch, caplog, capsys):
    from app import main

    def broken(req, generate=None, debug=None, kbd=None):
        raise ValueError("; ".join(SENTINELS))

    monkeypatch.setattr(main, "build_report", broken)
    caplog.set_level(logging.INFO)
    assert client.post("/v1/report", json=body("case1")).status_code == 500
    text = all_log_text(caplog, capsys)
    for s in SENTINELS:
        assert s not in text
    [err] = [x for x in caplog.records if x.getMessage() == "unhandled error"]
    assert err.exc_info is None and err.json_fields["exception"].startswith("ValueError at")
    assert "main.py" in err.json_fields["exception"]


def test_sentinel_uvicorn_traceback_is_sanitized(caplog):
    """Starlette re-raises after the 500 handler; uvicorn logs "Exception in ASGI application" with exc_info."""
    caplog.set_level(logging.INFO)
    try:
        try:
            raise KeyError("; ".join(SENTINELS))
        except KeyError as inner:
            raise RuntimeError("; ".join(SENTINELS)) from inner
    except RuntimeError:
        logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=True)
    text = all_log_text(caplog)
    for s in SENTINELS:
        assert s not in text
    assert "RuntimeError at" in text and "caused by KeyError" in text


def test_followup_sentinel_question_and_search_errors(monkeypatch, caplog, capsys):
    from test_protection import FakeGenai

    def leaky(prompt, schema):
        raise LeakyError(503)

    FakeGenai(leaky).install(monkeypatch)
    caplog.set_level(logging.INFO)
    q = "When was it built? " + SENTINELS[2]
    r = client.post("/v1/followup", json=body("case1") | {"question": q, "history": []})
    assert r.status_code in (200, 502, 503)
    text = all_log_text(caplog, capsys)
    for s in SENTINELS:
        assert s not in text
