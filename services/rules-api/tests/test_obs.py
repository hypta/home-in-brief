"""F07: rules-api completion record (endpoint, status, elapsed_ms, warnings count), no financial inputs."""
import logging

from fastapi.testclient import TestClient

from app import obs
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def completions(caplog):
    return [r for r in caplog.records if getattr(r, "json_fields", {}).get("event") == "request_complete"]


def test_rules_completion_record_without_amounts(caplog):
    caplog.set_level(logging.INFO)
    ui = {"price_override": 987654, "deposit": 123456, "household_income": 135791}
    r = client.post("/v1/rules", json={"user_inputs": ui}, headers={"X-Request-Id": "r-7"})
    assert r.status_code == 200
    [rec] = completions(caplog)
    f = rec.json_fields
    assert f["endpoint"] == "/v1/rules" and f["status"] == 200 and f["request_id"] == "r-7"
    assert isinstance(f["elapsed_ms"], int) and f["warnings"] == len(r.json()["warnings"])
    text = "\n".join(obs.JsonFormatter().format(x) for x in caplog.records)
    for v in ui.values():
        assert str(v) not in text


def test_rules_422_is_logged(caplog):
    caplog.set_level(logging.INFO)
    assert client.post("/v1/rules", json={"user_inputs": {"household_income": -5}}).status_code == 422
    [rec] = completions(caplog)
    assert rec.json_fields["status"] == 422 and rec.json_fields["warnings"] is None


def test_health_is_not_logged(caplog):
    caplog.set_level(logging.INFO)
    client.get("/health")
    assert completions(caplog) == []
