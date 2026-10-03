import json

import httpx

import checker
from checker import FetchResult, check_item, fetch_page, next_status, normalize
from main import run_job
from store import FirestoreStore, LocalStore

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
    r403 = fetch_page("https://e.test/403", client)
    assert (r403.http_status, r403.text, r403.network_error) == (403, None, True)
    rch = fetch_page("https://e.test/page", client)
    assert rch.text is None and "bot challenge" in rch.error


def test_http_fetch_html_split_inline_tags():
    html = "<html><body><p><span>Y</span>ou own the land</p><script>x()</script></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    assert normalize(fetch_page("https://e.test/", client).text) == "you own the land"


def test_pdf_branch_used(monkeypatch):
    monkeypatch.setattr(checker, "pdf_to_text", lambda b: "pdf text here")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"%PDF-1.4 ...", headers={"content-type": "application/pdf"})))
    assert fetch_page("https://e.test/a.pdf", client).text == "pdf text here"


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


def test_pdf_url_returning_html_is_network_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>blocked</html>")))
    r = fetch_page("https://e.test/a.pdf", client)
    assert r.text is None and r.network_error and "non-PDF" in r.error
