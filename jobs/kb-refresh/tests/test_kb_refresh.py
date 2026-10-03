import json

import httpx

import checker
from checker import Fetcher, FetchResult, check_item, http_fetch, next_status, normalize
from main import run_job
from store import FirestoreStore, LocalStore

QUOTE = "an insurer may limit any cover for flood damage"


SOURCES = [{"id": "s_ok", "tos_status": "allowed"}, {"id": "s_unclear", "tos_status": "unclear"},
           {"id": "s_prohibited", "tos_status": "prohibited"}]


def make_item(status="draft", quote=QUOTE, id="kb_x", source_id="s_ok"):
    return {"id": id, "quote": quote, "source_url": "https://example.test/p", "status": status,
            "source_id": source_id}


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


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


ROBOTS_OK = "User-agent: *\nDisallow: /private/\n"
BIG = "<html><body>" + "<p>real page text</p>" * 60 + "<p>an insurer may limit cover</p></body></html>"


def site(pages, robots=ROBOTS_OK, log=None):
    def handler(req):
        if log is not None:
            log.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots) if robots is not None else httpx.Response(404)
        return pages(req)
    return handler


def test_403_and_challenge_page_are_blocked_not_retried():
    seen = []

    def pages(req):
        seen.append(req.url.path)
        if req.url.path == "/403":
            return httpx.Response(403, text="no")
        return httpx.Response(200, text='<html><script src="/_Incapsula_Resource?x"></script></html>')

    f = Fetcher(_client(site(pages)))
    r403, rch = f("https://e.test/403"), f("https://e.test/page")
    assert (r403.http_status, r403.text, r403.network_error, r403.blocked) == (403, None, True, True)
    assert rch.text is None and "bot challenge" in rch.error and rch.blocked
    assert seen == ["/403", "/page"]  # one request each: no retry, no browser


def test_challenge_page_never_switches_to_browser():
    import sys
    assert "playwright" not in sys.modules and not hasattr(checker, "browser_fetch")
    short_shell = "<html><body><div id=app></div></body></html>"
    r = Fetcher(_client(site(lambda q: httpx.Response(200, text=short_shell))))("https://e.test/a")
    assert r.text is None and r.blocked and "too short" in r.error


def test_blocked_verified_item_keeps_status():
    f = Fetcher(_client(site(lambda q: httpx.Response(403))))
    item = make_item("verified")
    run = check_item(item, fetch=f, now="t")
    assert run["quote_found"] is None and run["blocked"] and next_status(item, run) == "verified"


def test_ok_page_uses_honest_user_agent_and_finds_quote():
    uas = []

    def pages(req):
        uas.append(req.headers["user-agent"])
        return httpx.Response(200, text=BIG)

    client = httpx.Client(transport=httpx.MockTransport(site(pages)), headers=checker.HEADERS)
    run = check_item(make_item(quote="an insurer may limit cover"), fetch=Fetcher(client), now="t")
    assert run["quote_found"] is True
    assert uas and all(u.startswith("home-buyer-copilot/0.1 (+https://github.com/") for u in uas)
    assert "Mozilla" not in checker.USER_AGENT


def test_robots_disallow_means_no_page_request():
    log = []
    f = Fetcher(_client(site(lambda q: httpx.Response(200, text=BIG), log=log)))
    r = f("https://e.test/private/page")
    assert r.text is None and r.blocked and "robots.txt" in r.error and r.network_error
    assert log == ["https://e.test/robots.txt"]
    assert f("https://e.test/public").text  # allowed path still fetched
    assert log.count("https://e.test/robots.txt") == 1  # cached per host


def test_robots_forbidden_or_error_is_conservative_and_missing_is_allowed():
    for status in (403, 500):
        log = []
        f = Fetcher(_client(lambda q, s=status, log=log: (log.append(q.url.path), httpx.Response(s))[1]))
        r = f("https://e.test/p")
        assert r.text is None and r.blocked and "robots.txt unavailable" in r.error and log == ["/robots.txt"]
    f = Fetcher(_client(site(lambda q: httpx.Response(200, text=BIG), robots=None)))
    assert f("https://e.test/p").text


def test_html_split_inline_tags():
    html = "<html><body><p><span>Y</span>ou own the land</p>" + "<p>filler text</p>" * 60 + "</body></html>"
    r = Fetcher(_client(site(lambda q: httpx.Response(200, text=html))))("https://e.test/")
    assert normalize(r.text).startswith("you own the land filler")


def test_pdf_branch_and_html_on_pdf_url(monkeypatch):
    monkeypatch.setattr(checker, "pdf_to_text", lambda b: "pdf text here")
    pdf = Fetcher(_client(site(lambda q: httpx.Response(200, content=b"%PDF-1.4 x"))))
    assert pdf("https://e.test/a.pdf").text == "pdf text here"
    bad = Fetcher(_client(site(lambda q: httpx.Response(200, text="<html>blocked</html>"))))("https://e.test/a.pdf")
    assert bad.text is None and bad.blocked and bad.network_error and "non-PDF" in bad.error


def test_local_job_writes_report_and_exit_codes(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "a.json").write_text(json.dumps([make_item("verified", id="a"), make_item("draft", id="b")]))
    (seed / "sources.json").write_text(json.dumps(SOURCES))
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
    (seed / "sources.json").write_text(json.dumps(SOURCES))
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
    db.data["sources/s_ok"] = {"id": "s_ok", "tos_status": "allowed"}
    db.data["kb_items/a"] = make_item("verified", id="a")
    db.data["kb_items/b"] = make_item("draft", id="b")
    job, code = run_job(FirestoreStore(db), fetch=page("redesigned page"), clock=lambda: "20260101T000000Z")
    assert code == 0 and job["checked"] == 2 and job["changed"] == 1
    assert db.data["kb_items/a"]["status"] == "needs_review"
    assert db.data["kb_items/b"]["status"] == "draft"
    assert db.data["kb_items/b"]["last_check"]["quote_found"] is False
    assert "kb_source_checks/a/runs/20260101T000000Z" in db.data
    assert db.data["job_runs/20260101T000000Z"]["job"] == "kb-refresh"


def test_non_allowed_sources_are_not_fetched_and_status_unchanged(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    items = [make_item("verified", id=i, source_id=s) for i, s in
             [("ok", "s_ok"), ("un", "s_unclear"), ("pr", "s_prohibited"), ("nosrc", "s_missing")]]
    (seed / "a.json").write_text(json.dumps(items))
    (seed / "sources.json").write_text(json.dumps(SOURCES))
    fetched = []

    def fetch(url):
        fetched.append(url)
        return FetchResult(200, "page without the quote", None)

    store = LocalStore(seed, tmp_path / "out")
    job, code = run_job(store, fetch=fetch, clock=lambda: "T")
    assert code == 0 and len(fetched) == 1  # only the allowed source made a request
    assert job["checked"] == 1 and job["manual_check_required"] == 3
    runs = {r["id"]: r for r in store.report["runs"]}
    for i in ("un", "pr", "nosrc"):
        assert runs[i]["manual_check_required"] and runs[i]["quote_found"] is None
        assert runs[i]["status_after"] == "verified"  # manual items are never demoted
    assert runs["ok"]["status_after"] == "needs_review"  # allowed + quote gone
    assert "tos_status=prohibited" in runs["pr"]["manual_reason"]


def test_run_ids_are_utc_compact_and_at_is_iso():
    import re
    from datetime import datetime
    db = FakeDB()
    db.data["sources/s_ok"] = {"id": "s_ok", "tos_status": "allowed"}
    db.data["kb_items/a"] = make_item("verified", id="a")
    job, _ = run_job(FirestoreStore(db), fetch=page("redesigned page"))  # real clock
    ids = [k.split("/")[-1] for k in db.data if k.startswith("job_runs/") or "/runs/" in k]
    assert ids and all(re.fullmatch(r"\d{8}T\d{6}Z", i) for i in ids)
    assert re.fullmatch(r"\d{8}T\d{6}Z", job["run_id"])
    assert datetime.fromisoformat(job["at"]).utcoffset().total_seconds() == 0
    assert db.data["kb_items/a"]["last_check"]["at"].endswith("+00:00")
