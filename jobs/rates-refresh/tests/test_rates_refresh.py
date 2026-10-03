from datetime import datetime

import pytest

from rates_refresh import main as job
from rates_refresh.extract import Band, Extracted, RateItem
from rates_refresh.fetch import FetchResult, fetch_bank
from rates_refresh.store import FirestoreStore, LocalStore

NOW = datetime.fromisoformat("2026-10-04T06:00:00+13:00")
LONG = "x" * 3000 + " rates 4.89% 4.79% 4.99% 5.19% 5.29% 5.39% 5.49% 5.59% 5.69%"


def bank(bid="b1", render="http", tos="allowed"):
    return {
        "bank_id": bid, "name": bid.upper(), "source_url": f"https://{bid}.example/",
        "render": render, "tos_status": tos, "mechanism": "lem", "lem_base": "special",
        "fhl": {"lender": True, "lmi_passthrough_pct": 1.2},
    }


def ext(special=None, standard=None, floating=6.5):
    special = {"1y": 4.9, "2y": 4.8} if special is None else special
    return Extracted(
        special=[RateItem(term=t, rate_pct=r) for t, r in special.items()],
        standard=[RateItem(term=t, rate_pct=r) for t, r in (standard or {}).items()],
        floating=floating,
        bands=[Band(lvr_from=80.01, lvr_to=85, margin_pct=0.3)],
        page_effective_date="1 Oct 2026",
    )


def ok_fetcher(b):
    return FetchResult(LONG, "http", 200, False, None)


class MemStore:
    def __init__(self, prev=None):
        self.prev, self.snapshots, self.runs = prev, [], []

    def load_previous(self):
        return self.prev

    def write_snapshot(self, date, snap):
        self.snapshots.append((date, snap))

    def write_job_run(self, run_id, run):
        self.runs.append((run_id, run))


def prev_snapshot(**over):
    rec = {"bank_id": "b1", "name": "B1", "source_url": "u", "page_sha256": "old",
           "page_effective_date": None, "special": {"1y": 5.0, "2y": 4.9}, "standard": None,
           "floating": 6.6, "special_requires_equity_pct": 20,
           "low_equity": {"mechanism": "lem", "lem_base": "special", "bands": []},
           "fhl": {"lender": True, "lmi_passthrough_pct": 1.2}, "notes": []}
    rec.update(over)
    return {"schema_version": "0.1", "captured_at": "x", "banks": [rec]}


def run(store, extractor, banks=None, fetcher=ok_fetcher):
    return job.run(banks or [bank()], store, extractor=extractor, fetcher=fetcher, now=NOW)


def test_normal_update():
    store = MemStore(prev_snapshot())
    snap, jr, code = run(store, lambda t, b: ext())
    assert code == 0 and jr["updated"] == ["b1"] and jr["failed"] == []
    assert store.snapshots[0][0] == "2026-10-04"
    rec = snap["banks"][0]
    assert rec["special"] == {"1y": 4.9, "2y": 4.8}
    assert rec["low_equity"]["bands"][0]["margin_pct"] == 0.3
    assert rec["fhl"] == {"lender": True, "lmi_passthrough_pct": 1.2}
    assert store.runs[0][1]["job"] == "rates-refresh"


@pytest.mark.parametrize("special", [{"1y": 1.5}, {"1y": 12.5}])
def test_out_of_range_rejected_keeps_old(special):
    store = MemStore(prev_snapshot(special={"1y": 5.0}))
    # second bank succeeds so a snapshot is written and we can inspect the stale carry-over
    banks = [bank("b1"), bank("b2")]
    snap, jr, code = run(store, lambda t, b: ext(special) if b["bank_id"] == "b1" else ext(), banks)
    assert jr["failed"][0]["bank_id"] == "b1" and "outside" in jr["failed"][0]["reason"]
    old = next(b for b in snap["banks"] if b["bank_id"] == "b1")
    assert old["special"] == {"1y": 5.0} and "stale_since: 2026-10-04" in old["notes"]


def test_jump_over_1pp_rejected_keeps_old():
    store = MemStore(prev_snapshot())
    banks = [bank("b1"), bank("b2")]
    snap, jr, _ = run(store, lambda t, b: ext({"1y": 6.2}) if b["bank_id"] == "b1" else ext(), banks)
    assert "moved" in jr["failed"][0]["reason"]
    old = next(b for b in snap["banks"] if b["bank_id"] == "b1")
    assert old["special"]["1y"] == 5.0
    assert "stale_since: 2026-10-04" in old["notes"]


def test_missing_1y_rejected():
    store = MemStore(prev_snapshot())
    banks = [bank("b1"), bank("b2")]
    _, jr, _ = run(store, lambda t, b: ext({"2y": 4.8}) if b["bank_id"] == "b1" else ext(), banks)
    assert "missing special.1y" in jr["failed"][0]["reason"]


def test_stale_since_keeps_earliest_date():
    store = MemStore(prev_snapshot(notes=["stale_since: 2026-09-30"]))
    snap, _, _ = run(store, lambda t, b: ext({"1y": 9.0}) if b["bank_id"] == "b1" else ext(),
                     [bank("b1"), bank("b2")])
    old = next(b for b in snap["banks"] if b["bank_id"] == "b1")
    assert old["notes"] == ["stale_since: 2026-09-30"]


def test_all_fail_no_snapshot_nonzero_exit():
    store = MemStore(prev_snapshot())
    snap, jr, code = run(store, lambda t, b: ext({"1y": 99}))
    assert snap is None and store.snapshots == []
    assert code == 1 and jr["updated"] == [] and len(jr["failed"]) == 1
    assert len(store.runs) == 1  # job_runs is always written


def test_fetch_error_does_not_crash_other_banks():
    def fetcher(b):
        if b["bank_id"] == "b1":
            raise RuntimeError("boom")
        return ok_fetcher(b)

    store = MemStore(None)
    snap, jr, code = run(store, lambda t, b: ext(), [bank("b1"), bank("b2")], fetcher=fetcher)
    assert code == 0 and jr["updated"] == ["b2"] and "boom" in jr["failed"][0]["reason"]
    assert [b["bank_id"] for b in snap["banks"]] == ["b2"]  # no previous data -> omitted


def test_blocked_page_not_sent_to_extractor():
    called = []
    blocked = lambda b: FetchResult("denied", "browser", 403, True, "http_403")
    _, jr, code = run(MemStore(), lambda t, b: called.append(1), fetcher=blocked)
    assert code == 1 and not called and "blocked" in jr["failed"][0]["reason"]


ROBOTS_OK = lambda url: (True, "robots_ok")


def test_blocked_http_stops_without_browser():
    calls = []

    def http(url):
        calls.append("http")
        return FetchResult("Incapsula incident", "http", 403, True, "http_403")

    def browser(url):
        calls.append("browser")
        return FetchResult(LONG, "browser", 200, False, None)

    r = fetch_bank(bank(), http=http, browser=browser, robots=ROBOTS_OK)
    assert calls == ["http"] and r.blocked and r.block_reason == "http_403"


def test_challenge_page_does_not_switch_to_browser():
    calls = []
    http = lambda url: (calls.append("http"), FetchResult("x", "http", 200, True, "challenge_page"))[1]
    browser = lambda url: calls.append("browser")
    assert fetch_bank(bank(), http=http, browser=browser, robots=ROBOTS_OK).blocked
    assert calls == ["http"]


def test_render_browser_only_when_configured():
    http = lambda url: FetchResult(LONG, "http", 200, False, None)
    browser = lambda url: FetchResult(LONG, "browser", 200, False, None)
    assert fetch_bank(bank(), http=http, browser=browser, robots=ROBOTS_OK).method == "http"
    assert fetch_bank(bank(render="browser"), http=http, browser=browser, robots=ROBOTS_OK).method == "browser"


def test_robots_disallow_means_no_page_request():
    calls = []
    http = lambda url: calls.append(url)
    r = fetch_bank(bank(), http=http, browser=http, robots=lambda u: (False, "robots_disallow"))
    assert calls == [] and r.blocked and r.block_reason == "robots_disallow"


def test_robots_parsing():
    from rates_refresh.fetch import robots_allows
    url = "https://x.example/rates/"
    assert robots_allows(url, getter=lambda u: (200, "User-agent: *\nDisallow: /rates/"))[0] is False
    assert robots_allows(url, getter=lambda u: (200, "User-agent: *\nDisallow: /admin/"))[0] is True
    assert robots_allows(url, getter=lambda u: (200, "User-agent: home-buyer-copilot\nDisallow: /"))[0] is False
    assert robots_allows(url, getter=lambda u: (404, ""))[0] is True
    assert robots_allows(url, getter=lambda u: (403, ""))[0] is False
    assert robots_allows(url, getter=lambda u: (503, ""))[0] is False


def test_user_agent_is_honest():
    from rates_refresh.fetch import USER_AGENT
    assert USER_AGENT.startswith("home-buyer-copilot/") and "Mozilla" not in USER_AGENT


def test_not_allowed_banks_skipped_without_any_request():
    called = []
    fetcher = lambda b: called.append(b["bank_id"])
    prev = prev_snapshot()
    store = MemStore(prev)
    banks = [bank("b1", tos="unclear"), bank("b2", tos="prohibited")]
    snap, jr, code = run(store, lambda t, b: called.append("extract"), banks, fetcher=fetcher)
    assert called == [] and jr["skipped_by_policy"] == ["b1", "b2"]
    assert jr["updated"] == [] and jr["failed"] == []
    assert code == 0 and snap is None and store.snapshots == []  # nothing written but the job_run
    assert len(store.runs) == 1


def test_mixed_policy_keeps_skipped_bank_data_as_is():
    store = MemStore(prev_snapshot())
    banks = [bank("b1", tos="unclear"), bank("b2")]
    snap, jr, code = run(store, lambda t, b: ext(), banks)
    assert code == 0 and jr["updated"] == ["b2"] and jr["skipped_by_policy"] == ["b1"]
    kept = next(b for b in snap["banks"] if b["bank_id"] == "b1")
    assert kept == prev_snapshot()["banks"][0]  # untouched, not marked stale


def test_main_without_allowed_banks_exits_zero_with_message(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_BACKEND", "local")
    monkeypatch.setenv("OUT_DIR", str(tmp_path))
    monkeypatch.setattr(job, "fetch_bank", lambda b: pytest.fail("must not fetch"))
    assert job.main([]) == 0
    assert "No bank has tos_status=allowed" in capsys.readouterr().out
    assert job.main(["--fetch-only"]) == 0


def test_detect_block():
    from rates_refresh.fetch import detect_block
    assert detect_block(200, "short") .startswith("short_content")
    assert detect_block(200, "Request unsuccessful. Incapsula incident ID: 1") == "challenge_page"
    assert detect_block(403, LONG) == "http_403"
    assert detect_block(200, LONG) is None
    assert detect_block(200, "y" * 3000 + " fee 1.20% 0.50%").startswith("no_rates_in_content")


def test_local_store_missing_seed_and_outputs(tmp_path):
    store = LocalStore(seed_path=tmp_path / "nope.json", out_dir=tmp_path / "out")
    assert store.load_previous() is None
    snap, jr, code = run(store, lambda t, b: ext())
    assert code == 0
    assert (tmp_path / "out" / "rates_snapshot.json").exists()
    assert (tmp_path / "out" / "job_run.json").exists()


class FakeDoc:
    def __init__(self, db, path): self.db, self.path = db, path
    def set(self, data): self.db.data[self.path] = data
    def get(self):
        d = self.db.data.get(self.path)
        return type("S", (), {"exists": d is not None, "to_dict": lambda s: d})()


class FakeDB:
    def __init__(self): self.data = {}
    def collection(self, c):
        return type("C", (), {"document": lambda s, i: FakeDoc(self, f"{c}/{i}")})()


def test_firestore_store_paths():
    db = FakeDB()
    store = FirestoreStore(client=db)
    run(store, lambda t, b: ext())
    assert "rates_latest/current" in db.data and "rates_snapshots/2026-10-04" in db.data
    assert db.data["rates_latest/current"] == db.data["rates_snapshots/2026-10-04"]
    assert any(k.startswith("job_runs/") for k in db.data)


def test_banks_config_is_complete():
    banks = job.load_banks()
    ids = [b["bank_id"] for b in banks]
    assert len(ids) == len(set(ids)) and {"tsb", "coop", "sbs", "unity", "nbs", "nzhl"} <= set(ids)
    for b in banks:
        assert b["source_url"].startswith("https://")
        assert b["mechanism"] in ("lem", "standard_rate")
        assert "lender" in b["fhl"] and "lmi_passthrough_pct" in b["fhl"]
        assert b["tos_status"] in ("allowed", "unclear", "prohibited")
        assert b["tos_checked_at"] and b["tos_note"] and "tos_url" in b
        assert b.get("render", "http") in ("http", "browser")
        if b.get("render") == "browser":
            assert b["tos_status"] == "allowed"  # never render-by-browser a site we may not automate
    assert next(b for b in banks if b["bank_id"] == "nzhl")["tos_status"] == "prohibited"


def test_store_imports_from_shallow_path(monkeypatch, tmp_path):
    # Docker puts the package at /app/rates_refresh/store.py: only two parent directories
    import rates_refresh.store as real
    src = open(real.__file__).read()
    for fake in ("/app/rates_refresh/store.py", "/store.py"):
        ns = {"__file__": fake, "__name__": "shallow_store"}
        exec(compile(src, fake, "exec"), ns)  # must not raise at import time
        monkeypatch.delenv("RATES_SEED_PATH", raising=False)
        monkeypatch.delenv("OUT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        store = ns["LocalStore"]()  # falls back to cwd, still no crash
        assert store.load_previous() is None
