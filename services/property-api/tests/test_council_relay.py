"""COUNCIL_RELAY_URL / COUNCIL_RELAY_TOKEN: the council page comes through the relay. No network."""
from unittest.mock import Mock

import pytest
import requests

from app import context, council, http
from test_property_api import FLOODED, build

KEY = "12345450368"  # a recorded council property page (tests/fixtures)
RELAY = "https://pi.example.ts.net"


def recorded_page():
    return http.request("GET", council.RATES_PAGE.format(key=KEY), as_json=False)  # replayed by conftest


def reply(status=200, text=""):
    r = requests.Response()
    r.status_code, r._content = status, text.encode()
    return r


@pytest.fixture
def relay_env(monkeypatch):
    monkeypatch.setenv("COUNCIL_RELAY_URL", RELAY + "/")
    monkeypatch.setenv("COUNCIL_RELAY_TOKEN", "secret-token")


def test_without_relay_settings_the_council_is_fetched_directly(monkeypatch):
    get = Mock()
    monkeypatch.setattr(requests, "get", get)
    assert council.property_page(KEY)["property_category"]
    get.assert_not_called()


@pytest.mark.parametrize("missing", ["COUNCIL_RELAY_URL", "COUNCIL_RELAY_TOKEN"])
def test_one_setting_alone_does_not_switch_to_the_relay(monkeypatch, relay_env, missing):
    monkeypatch.delenv(missing)
    get = Mock()
    monkeypatch.setattr(requests, "get", get)
    assert council.property_page(KEY)["property_category"]
    get.assert_not_called()


def test_with_relay_settings_the_page_comes_through_the_relay_with_the_token(monkeypatch, relay_env):
    with monkeypatch.context() as m:
        m.delenv("COUNCIL_RELAY_URL")
        direct = council.property_page(KEY)
    get = Mock(return_value=reply(text=recorded_page()))
    monkeypatch.setattr(http, "request", Mock(side_effect=AssertionError("must not call the council directly")))
    monkeypatch.setattr(requests, "get", get)
    assert council.property_page(KEY) == direct
    args, kw = get.call_args
    assert args[0] == RELAY + "/v1/property-page" and kw["params"] == {"key": KEY}
    assert kw["headers"]["X-Relay-Token"] == "secret-token"
    assert kw["timeout"] <= 6 and kw["allow_redirects"] is False


@pytest.mark.parametrize("outcome", [reply(429), reply(502), reply(401), reply(302),
                                     requests.Timeout("simulated"), requests.ConnectionError("simulated")])
def test_relay_failure_leaves_the_fields_unavailable_not_the_request_failed(monkeypatch, relay_env, outcome):
    kw = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    monkeypatch.setattr(requests, "get", Mock(**kw))
    p = build(FLOODED).property
    assert p.cv.value == 340000  # the GeoMaps valuation is untouched
    assert (p.year_built.status, p.floor_area_m2.status, p.floor_area_m2.reason) == (
        "unavailable", "unavailable", "source_unavailable")


def test_council_page_off_wins_over_the_relay(monkeypatch, relay_env):
    monkeypatch.setenv("COUNCIL_PAGE", "off")
    get = Mock()
    monkeypatch.setattr(requests, "get", get)
    build(FLOODED)
    get.assert_not_called()


def test_the_token_is_not_in_the_logged_failure(monkeypatch, relay_env, caplog):
    monkeypatch.setattr(requests, "get", Mock(side_effect=requests.ConnectionError("simulated")))
    build(FLOODED)
    assert "secret-token" not in caplog.text
