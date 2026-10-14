"""No network: the council fetch is replaced by a fake; one test talks to the server over loopback only."""
import json
import threading
import urllib.error
import urllib.request
from http.client import HTTPMessage

import pytest

import relay
from relay import Config, Relay, UpstreamError

TOKEN = "test-token-0123456789abcdef"
GOOD = {"X-Relay-Token": TOKEN}
PAGE = "<html>Total floor area 120 m2</html>"


class Fake:
    def __init__(self, result=PAGE):
        self.urls, self.result = [], result

    def __call__(self, url):
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make(fake=None, clock=None, **cfg):
    fake = fake or Fake()
    return Relay(Config(token=TOKEN, **cfg), fetch=fake, clock=clock or Clock()), fake


def get(r, key="12345450368", headers=GOOD):
    return r.handle(f"/v1/property-page?key={key}", headers)


def test_good_request_returns_the_page_from_the_council_url():
    r, fake = make()
    status, ctype, body = get(r)
    assert (status, body) == (200, PAGE) and ctype.startswith("text/html")
    assert fake.urls == [relay.RATES_PAGE.format(key="12345450368")]


def test_user_agent_is_the_honest_project_one():
    assert relay.USER_AGENT.startswith("home-in-brief/") and "non-commercial" in relay.USER_AGENT
    assert "Mozilla" not in relay.USER_AGENT


@pytest.mark.parametrize("key", ["", "abc", "1234", "12345678901234567", "123 456", "12345%2F..", "../../x",
                                 "https://evil.example/x", "12345.html", "12345;6", "-12345", "１２３４５６"])
def test_invalid_keys_are_400_and_never_fetched(key):
    r, fake = make()
    assert get(r, key)[0] == 400 and fake.urls == []


def test_missing_or_repeated_key_is_400():
    r, fake = make()
    assert r.handle("/v1/property-page", GOOD)[0] == 400
    assert r.handle("/v1/property-page?key=12345678&key=87654321", GOOD)[0] == 400
    assert r.handle("/v1/property-page?url=https://evil.example/", GOOD)[0] == 400
    assert fake.urls == []


def test_only_council_urls_are_ever_fetched():
    r, fake = make(rate_per_minute=1000)
    for key in ["12345678", "99999999999", "https://evil.example/", "12345678/../../x", "1" * 15]:
        get(r, key)
    assert fake.urls and all(u.startswith("https://www.aucklandcouncil.govt.nz/en/property-rates-valuations/")
                             for u in fake.urls)


@pytest.mark.parametrize("headers", [{}, {"X-Relay-Token": ""}, {"X-Relay-Token": "wrong"}, {"X-Relay-Token": TOKEN + "x"}])
def test_token_is_required_and_exact(headers):
    r, fake = make()
    assert get(r, headers=headers)[0] == 401 and fake.urls == []


def test_unauthenticated_requests_do_not_use_the_rate_budget():
    r, fake = make(rate_per_minute=1)
    for _ in range(5):
        get(r, headers={})
    assert get(r)[0] == 200


def test_healthz_needs_no_token_and_never_calls_the_council():
    r, fake = make()
    assert r.handle("/healthz", {})[0] == 200 and fake.urls == []


def test_unknown_path_is_404():
    r, _ = make()
    assert r.handle("/v1/other?key=12345678", GOOD)[0] == 404
    assert r.handle("/", GOOD)[0] == 404


def test_rate_limit_then_recovery_after_a_minute():
    clock = Clock()
    r, fake = make(clock=clock, rate_per_minute=3)
    assert [get(r, f"1000000{i}")[0] for i in range(4)] == [200, 200, 200, 429]
    assert len(fake.urls) == 3
    clock.t += 61
    assert get(r, "10000009")[0] == 200


def test_cache_hit_does_not_refetch_or_use_rate_budget():
    r, fake = make(rate_per_minute=1)
    assert get(r)[0] == 200
    assert [get(r)[0] for _ in range(5)] == [200] * 5
    assert len(fake.urls) == 1


def test_cache_expires_after_ttl():
    clock = Clock()
    r, fake = make(clock=clock, cache_ttl_s=100)
    get(r)
    clock.t += 101
    get(r)
    assert len(fake.urls) == 2


def test_cache_is_bounded_lru():
    r, fake = make(cache_max=2, rate_per_minute=1000)
    for k in ["10000001", "10000002", "10000001", "10000003"]:  # 10000002 is the least recently used
        get(r, k)
    n = len(fake.urls)
    get(r, "10000001")
    get(r, "10000003")
    assert len(fake.urls) == n
    get(r, "10000002")
    assert len(fake.urls) == n + 1


def test_upstream_status_becomes_502_and_is_not_cached():
    fake = Fake(UpstreamError(406, "upstream_status"))
    r, _ = make(fake)
    status, ctype, body = get(r)
    assert status == 502 and json.loads(body) == {"error": "upstream_status", "upstream_status": 406}
    fake.result = PAGE
    assert get(r)[0] == 200 and len(fake.urls) == 2


def test_upstream_timeout_is_504():
    r, _ = make(Fake(UpstreamError(None, "upstream_timeout")))
    assert get(r)[0] == 504


def test_token_and_html_are_not_logged(caplog):
    caplog.set_level("INFO", logger="council-relay")
    r, _ = make()
    get(r)
    get(r, headers={"X-Relay-Token": "wrong-token-value"})
    text = caplog.text
    assert "property-page" in text and TOKEN not in text and "wrong-token-value" not in text and "floor area" not in text


def test_refuses_to_start_without_a_token():
    for env in ({}, {"RELAY_TOKEN": ""}, {"RELAY_TOKEN": "short"}):
        with pytest.raises(SystemExit):
            Config.from_env(env)
    assert Config.from_env({"RELAY_TOKEN": TOKEN, "RATE_LIMIT_PER_MINUTE": "5"}).rate_per_minute == 5


def test_redirects_are_never_followed():
    h = relay._NoRedirect()
    req = urllib.request.Request("https://www.aucklandcouncil.govt.nz/x")
    assert h.redirect_request(req, None, 302, "Found", HTTPMessage(), "https://evil.example/") is None


def test_fetch_council_maps_http_errors_without_network(monkeypatch):
    class Opener:
        def open(self, req, timeout=None):
            assert req.get_method() == "GET" and req.get_header("User-agent") == relay.USER_AGENT
            assert timeout == relay.UPSTREAM_TIMEOUT
            raise urllib.error.HTTPError(req.full_url, 406, "Not Acceptable", HTTPMessage(), None)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(UpstreamError) as e:
        relay.fetch_council("https://www.aucklandcouncil.govt.nz/x")
    assert (e.value.status, e.value.reason) == (406, "upstream_status")


def test_over_http_on_loopback():
    r, fake = make()
    server = relay.make_server(r, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert urllib.request.urlopen(base + "/healthz").status == 200
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/v1/property-page?key=12345678")
        assert e.value.code == 401
        req = urllib.request.Request(base + "/v1/property-page?key=12345678", headers=GOOD)
        assert urllib.request.urlopen(req).read().decode() == PAGE
    finally:
        server.shutdown()
        server.server_close()


def test_slim_drops_scripts_styles_and_svg_but_keeps_text_and_comments():
    page = ('<p>Total floor area</p><script>var big = "x";</script><style>.a{}</style>'
            '<svg><path d="M0"/></svg><p class="lead">120<!-- --> m²</p>')
    out = relay.slim(page)
    assert "var big" not in out and ".a{}" not in out and "<path" not in out
    assert "Total floor area" in out and "120<!-- --> m²" in out


def test_fetch_council_asks_for_gzip_and_returns_the_slim_page(monkeypatch):
    import gzip as gz
    body = gz.compress(b'<p>Land area</p><script>' + b"x" * 50_000 + b'</script><p>3257</p>')
    seen = {}

    class Resp:
        status = 200
        headers = HTTPMessage()

        def __init__(self):
            self.headers["Content-Encoding"] = "gzip"
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def read(self, n):
            return body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout):
            seen["enc"] = req.get_header("Accept-encoding")
            return Resp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    out = relay.fetch_council("https://www.aucklandcouncil.govt.nz/x")
    assert seen["enc"] == "gzip"
    assert out == "<p>Land area</p><p>3257</p>"
