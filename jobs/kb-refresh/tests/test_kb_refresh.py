import json

import httpx

import checker
from checker import FetchResult, check_item, fetch_page, http_fetch, next_status, normalize
from main import run_job
from store import FirestoreStore, LocalStore

def _no_browser(url):
    raise AssertionError("browser must not be used here")


QUOTE = "an insurer may limit any cover for flood damage"


def make_item(status="draft", quote=QUOTE, id="kb_x"):
    return {"id": id, "quote": quote, "source_url": "https://example.test/p", "status": status}


def page(text):
    return lambda url: FetchResult(200, text, None)


def test_quote_found():
    run = check_item(make_item(), fetch=page(f"Intro. {QUOTE}. Outro."), now="t")
    assert run["quote_found"] is True
    assert len(run["page_sha256"]) == 64 and run["error"] is None


def test_quote_missing_verified_becomes_needs_review():
    item = make_item("verified")
    run = check_item(item, fetch=page("Page was redesigned."), now="t")
    assert run["quote_found"] is False
    assert next_status(item, run) == "needs_review"


def test_draft_status_unchanged_when_quote_missing():
    item = make_item("draft")
    run = check_item(item, fetch=page("Page was redesigned."), now="t")
    assert run["quote_found"] is False
    assert next_status(item, run) == "draft"


def test_403_does_not_change_status():
    item = make_item("verified")
    run = check_item(item, fetch=lambda u: FetchResult(403, None, "HTTP 403"), now="t")
    assert run["quote_found"] is None and run["error"] == "HTTP 403"
    assert next_status(item, run) == "verified"


def test_normalize_curly_vs_straight_quotes_and_dashes():
    assert normalize("A bank’s “proof” – now") == normalize("A bank's \"proof\" - now")
    item = make_item(quote="you’ve arranged insurance")
    run = check_item(item, fetch=page("Ensure you've   arranged\ninsurance."), now="t")
    assert run["quote_found"] is True


def test_http_fetch_403_and_challenge_page_are_network_errors():
    def handler(req):
        if req.url.path == "/403":
            return httpx.Response(403, text="nope")
        return httpx.Response(200, text='<html><script src="/_Incapsula_Resource?x"></script></html>')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r403 = http_fetch("https://e.test/403", client)
    assert (r403.http_status, r403.text, r403.network_error, r403.blocked) == (403, None, True, True)
    rch = http_fetch("https://e.test/page", client)
    assert rch.text is None and "bot challenge" in rch.error and rch.blocked


def test_http_fetch_html_split_inline_tags():
    html = "<html><body><p><span>Y</span>ou own the land</p>" + "<p>filler text</p>" * 60 + "<script>x()</script></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    assert normalize(fetch_page("https://e.test/", client, browser=_no_browser).text).startswith("you own the land filler")


def test_pdf_branch_used(monkeypatch):
    monkeypatch.setattr(checker, "pdf_to_text", lambda b: "pdf text here")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"%PDF-1.4 ...", headers={"content-type": "application/pdf"})))
    assert fetch_page("https://e.test/a.pdf", client, browser=_no_browser).text == "pdf text here"


def test_local_job_writes_report_and_exit_codes(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "a.json").write_text(json.dumps([make_item("verified", id="a"), make_item("draft", id="b")]))
    (seed / "sources.json").write_text("[]")
    store = LocalStore(seed, tmp_path / "out")
    job, code = run_job(store, fetch=lambda u: FetchResult(403, None, "HTTP 403"), clock=lambda: "T")
    assert code == 0 and job["checked"] == 2 and job["changed"] == 0 and len(job["errors"]) == 2
    report = json.loads((tmp_path / "out" / "kb_refresh_report.json").read_text())
    assert [r["quote_found"] for r in report["runs"]] == [None, None]
    # seed files are never modified by the job
    assert json.loads((seed / "a.json").read_text())[0]["status"] == "verified"


def test_non_network_error_gives_nonzero_exit(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "a.json").write_text(json.dumps([make_item(id="a")]))
    store = LocalStore(seed, tmp_path / "out")
    _, code = run_job(store, fetch=lambda u: FetchResult(200, None, "extract failed: boom", False), clock=lambda: "T")
    assert code == 1


class FakeDoc:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def set(self, data):
        self.db.data[self.path] = dict(data)

    def update(self, data):
        self.db.data.setdefault(self.path, {}).update(data)

    def collection(self, name):
        return FakeColl(self.db, f"{self.path}/{name}")


class FakeColl:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, id):
        return FakeDoc(self.db, f"{self.path}/{id}")

    def stream(self):
        out = []
        for p, d in self.db.data.items():
            if p.startswith(self.path + "/") and p.count("/") == self.path.count("/") + 1:
                out.append(type("S", (), {"id": p.rsplit("/", 1)[1], "to_dict": lambda s, d=d: dict(d)})())
        return out


class FakeDB:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        return FakeColl(self, name)


def test_firestore_verified_goes_needs_review_and_logs():
    db = FakeDB()
    db.data["kb_items/a"] = make_item("verified", id="a")
    db.data["kb_items/b"] = make_item("draft", id="b")
    job, code = run_job(FirestoreStore(db), fetch=page("redesigned page"), clock=lambda: "20260101T000000Z")
    assert code == 0 and job["checked"] == 2 and job["changed"] == 1
    assert db.data["kb_items/a"]["status"] == "needs_review"
    assert db.data["kb_items/b"]["status"] == "draft"
    assert db.data["kb_items/b"]["last_check"]["quote_found"] is False
    assert "kb_source_checks/a/runs/20260101T000000Z" in db.data
    assert db.data["job_runs/20260101T000000Z"]["job"] == "kb-refresh"


def test_pdf_block_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(checker, "PDF_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(checker, "pdf_to_text", lambda b: "pdf ok")
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(200, text="<html>blocked</html>") if len(n) < 3 else httpx.Response(200, content=b"%PDF-1.4 x")

    assert fetch_page("https://e.test/a.pdf", _client(handler), browser=_no_browser).text == "pdf ok" and len(n) == 3


def test_pdf_url_returning_html_is_network_error(monkeypatch):
    monkeypatch.setattr(checker, "PDF_RETRY_DELAYS", (0, 0))
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>blocked</html>")))
    r = fetch_page("https://e.test/a.pdf", client, browser=_no_browser)
    assert r.text is None and r.network_error and "non-PDF" in r.error


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


BIG = "<html><body>" + "<p>real page text</p>" * 60 + "<p>an insurer may limit cover</p></body></html>"


def test_403_switches_to_browser():
    calls = []

    def browser(url):
        calls.append(url)
        return FetchResult(200, "real page text " * 60, None, method="browser")

    r = fetch_page("https://e.test/p", _client(lambda q: httpx.Response(403, text="no")), browser=browser)
    assert calls == ["https://e.test/p"] and r.method == "browser" and r.text and r.error is None


def test_challenge_page_and_short_text_switch_to_browser():
    got = []
    browser = lambda u: (got.append(u), FetchResult(200, "x " * 400, None, method="browser"))[1]
    chal = '<script src="/_Incapsula_Resource?x"></script>'
    assert fetch_page("https://e.test/a", _client(lambda q: httpx.Response(200, text=chal)), browser=browser).method == "browser"
    shell = "<html><body><div id=app></div></body></html>"
    assert fetch_page("https://e.test/b", _client(lambda q: httpx.Response(200, text=shell)), browser=browser).method == "browser"
    assert len(got) == 2


def test_ok_http_does_not_use_browser():
    r = fetch_page("https://e.test/p", _client(lambda q: httpx.Response(200, text=BIG)), browser=_no_browser)
    assert r.method == "http" and "an insurer may limit cover" in r.text


def test_pdf_block_never_uses_browser(monkeypatch):
    monkeypatch.setattr(checker, "PDF_RETRY_DELAYS", (0, 0))
    r = fetch_page("https://e.test/a.pdf", _client(lambda q: httpx.Response(403)), browser=_no_browser)
    assert r.text is None and r.blocked and r.network_error


def test_browser_also_blocked_is_network_error_not_quote_gone():
    browser = lambda u: FetchResult(200, None, "still blocked in browser", method="browser")
    r = fetch_page("https://e.test/p", _client(lambda q: httpx.Response(403)), browser=browser)
    assert r.text is None and "HTTP 403" in r.error and "still blocked" in r.error and r.network_error
    item = make_item("verified")
    run = check_item(item, fetch=lambda u: r, now="t")
    assert run["quote_found"] is None and next_status(item, run) == "verified"


def test_browser_fetched_text_records_method():
    browser = lambda u: FetchResult(200, f"{QUOTE} " + "pad " * 200, None, method="browser")
    fetch = lambda u: fetch_page(u, _client(lambda q: httpx.Response(403)), browser=browser)
    run = check_item(make_item(), fetch=fetch, now="t")
    assert run["quote_found"] is True and run["fetch_method"] == "browser"
