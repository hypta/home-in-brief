"""The relay is tested against real upstream responses recorded in tests/fixtures (see its README)."""
import json
import re
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parents[1]))

from app import main  # noqa: E402

FIXTURES = HERE.parent / "fixtures"
CONTEXT = ROOT / "services/property-api/fixtures/property_context_740_sandringham_road_extension.json"
client = TestClient(main.app)


def upstream(status, body):
    r = requests.Response()
    r.status_code, r._content = status, body
    r.headers["content-type"] = "application/json"
    return r


@pytest.fixture
def sent(monkeypatch):
    call = Mock(return_value=upstream(200, b"[]"))
    monkeypatch.setattr(main.requests, "request", call)
    return call


def test_get_is_relayed_to_property_api_with_its_query(sent):
    sent.return_value = upstream(200, CONTEXT.read_bytes())
    r = client.get("/api/property-context?address_id=2100210&input=740+sand")
    assert r.status_code == 200 and r.json()["address"]["linz_address_id"] == 2100210
    assert sent.call_args.args == ("GET", "http://localhost:8081/v1/property-context")
    assert sent.call_args.kwargs["params"] == [("address_id", "2100210"), ("input", "740 sand")]
    assert sent.call_args.kwargs["headers"] == {}


def test_rules_post_is_relayed_and_a_validation_error_passes_through(sent):
    body = {"property_context": json.loads(CONTEXT.read_text()), "user_inputs": {"deposit": 60000}}
    sent.return_value = upstream(200, (FIXTURES / "rules_output_low_deposit.json").read_bytes())
    r = client.post("/api/rules", json=body)
    assert r.status_code == 200 and r.json()["lvr_pct"] == 82.35
    assert sent.call_args.args == ("POST", "http://localhost:8080/v1/rules") and sent.call_args.kwargs["json"] == body

    sent.return_value = upstream(422, (FIXTURES / "rules_error_deposit_over_price.json").read_bytes())
    r = client.post("/api/rules", json=body)
    assert r.status_code == 422 and r.json()["error"]["fields"][0]["field"] == "user_inputs.deposit"


def test_property_api_error_is_reshaped_for_the_page(sent):
    sent.return_value = upstream(422, (FIXTURES / "property_api_error_short_query.json").read_bytes())
    r = client.get("/api/addresses?q=ab")
    assert r.status_code == 422 and r.json()["error"] == {
        "code": "upstream_error", "message": "The request could not be completed.", "fields": []}


@pytest.mark.parametrize("method, path", [("GET", "/api/rules"), ("POST", "/api/addresses"), ("GET", "/api/healthz"),
                                          ("GET", "/api/kb/items/extra")])
def test_only_listed_paths_are_relayed(sent, method, path):
    r = client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found" and not sent.called


def test_oversized_body_is_refused(sent):
    r = client.post("/api/rules", json={"pad": "x" * main.MAX_BODY})
    assert r.status_code == 413 and not sent.called


def test_upstream_failures_become_502(sent, monkeypatch):
    # Cloud Run's own 403 for a caller without run.invoker cannot be recorded locally; only its status matters here.
    sent.return_value = upstream(403, b"")
    r = client.get("/api/sources")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_auth"

    monkeypatch.setattr(main.requests, "request", Mock(side_effect=requests.ConnectionError))
    r = client.get("/api/sources")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unavailable"


def test_https_upstream_gets_a_cached_id_token_until_it_is_refused(sent, monkeypatch):
    # The metadata server only exists on Google Cloud, so its answer cannot be recorded; "TOKEN" stands in for it.
    rules = "https://rules-api.example.run.app"
    monkeypatch.setitem(main.ROUTES, ("GET", "sources"), (rules, "/v1/sources"))
    monkeypatch.setattr(main, "_tokens", {})
    metadata = Mock(return_value=Mock(text="TOKEN"))
    monkeypatch.setattr(main.requests, "get", metadata)
    client.get("/api/sources")
    client.get("/api/sources")
    assert sent.call_args.kwargs["headers"] == {"Authorization": "Bearer TOKEN"}
    metadata.assert_called_once()
    assert metadata.call_args.kwargs["params"] == {"audience": rules}
    assert metadata.call_args.kwargs["headers"] == {"Metadata-Flavor": "Google"}

    sent.return_value = upstream(401, b"")
    client.get("/api/sources")
    client.get("/api/sources")
    assert metadata.call_count == 2


def test_page_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "<title>" in r.text and r.headers["cache-control"] == "no-cache"


def test_config_hands_the_basemap_key_to_the_page(monkeypatch):
    monkeypatch.setenv("LINZ_BASEMAPS_KEY", "k")
    assert client.get("/config.json").json() == {"linz_basemaps_key": "k"}
    monkeypatch.delenv("LINZ_BASEMAPS_KEY")
    assert client.get("/config.json").json() == {"linz_basemaps_key": None}


def test_report_page_words_every_flag_and_warning_in_codes_md():
    codes_md = (ROOT / "CODES.md").read_text().split("## Other fields")[0]
    codes = set(re.findall(r"^\| `([a-z0-9_]+)", codes_md, re.M))
    page = (HERE.parents[1] / "app/static/report.js").read_text()
    assert len(codes) > 25 and not {c for c in codes if c not in page}
