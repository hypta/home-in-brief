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
    assert store.snapshots[0][0].startswith("2026-10-04_")
    rec = snap["banks"][0]
    assert rec["special"] == {"1y": 4.9, "2y": 4.8}
    assert rec["low_equity"]["bands"][0]["margin_pct"] == 0.3
    assert rec["fhl"] == {"lender": True, "lmi_passthrough_pct": 1.2, "special_applies": None}
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
    snap_keys = [k for k in db.data if k.startswith("rates_snapshots/2026-10-04_")]
    assert "rates_latest/current" in db.data and len(snap_keys) == 1
    assert db.data["rates_latest/current"] == db.data[snap_keys[0]]
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


def test_job_run_id_is_utc_compact_and_times_are_iso():
    import re
    store = MemStore()
    run(store, lambda t, b: ext())
    run_id, jr = store.runs[0]
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{4}", run_id) and run_id.startswith("20261003T170000Z-")  # NOW is 06:00+13:00
    assert jr["run_id"] == run_id
    assert datetime.fromisoformat(jr["finished_at"]).utcoffset() is not None
    assert datetime.fromisoformat(store.snapshots[0][1]["captured_at"]).utcoffset() is not None
    assert store.snapshots[0][0].startswith("2026-10-04_")  # NZ date + run id


# ---- review fixes F19-F26 ----
import httpx

from rates_refresh import fetch as fetchmod
from rates_refresh.validate import build_bank


def multi_prev():
    prev = prev_snapshot()
    for bid in ("b2", "b3"):
        prev["banks"].append({**prev["banks"][0], "bank_id": bid, "name": bid.upper(),
                              "captured_at": f"2026-09-0{bid[-1]}T00:00:00+12:00"})
    return prev


def test_bank_filter_cli_keeps_other_banks(monkeypatch, capsys):
    store = MemStore(multi_prev())
    monkeypatch.setattr(job, "make_store", lambda: store)
    monkeypatch.setattr(job, "load_banks", lambda: [bank("b1"), bank("b2"), bank("b3", tos="unclear")])
    monkeypatch.setattr(job, "fetch_bank", ok_fetcher)
    monkeypatch.setattr(job, "gemini_extractor", lambda t, b: ext({"1y": 4.95, "2y": 4.85}))
    assert job.main(["--bank", "b2"]) == 0
    banks = {b["bank_id"]: b for b in store.snapshots[0][1]["banks"]}
    assert set(banks) == {"b1", "b2", "b3"}
    assert banks["b2"]["special"]["1y"] == 4.95 and banks["b2"]["captured_at"] != "2026-09-02T00:00:00+12:00"
    prev = {b["bank_id"]: b for b in multi_prev()["banks"]}
    assert banks["b1"] == prev["b1"] and banks["b3"] == prev["b3"]  # untouched, timestamps kept
    assert job.main(["--bank", "nope"]) == 2


def test_skipped_bank_row_kept_in_snapshot_when_another_updates():
    store = MemStore(multi_prev())
    snap, _, _ = run(store, lambda t, b: ext(), [bank("b1", tos="unclear"), bank("b2")])
    assert {b["bank_id"] for b in snap["banks"]} == {"b1", "b2", "b3"}


@pytest.mark.parametrize("status", [401, 403, 406, 429, 500, 503])
def test_robots_block_statuses_mean_no_fetch(status):
    from rates_refresh.fetch import robots_allows
    ok, why = robots_allows("https://x.example/r", getter=lambda u: (status, ""))
    assert ok is False and why == f"robots_status_{status}"


def test_robots_timeout_redirect_and_html_challenge_mean_no_fetch():
    from rates_refresh.fetch import RedirectBlocked, robots_allows
    url = "https://x.example/r"

    def timeout(u): raise httpx.ReadTimeout("slow")
    def redirect(u): raise RedirectBlocked("elsewhere")

    assert robots_allows(url, getter=timeout)[0] is False
    assert robots_allows(url, getter=redirect)[0] is False
    assert robots_allows(url, getter=lambda u: (200, "<html><body>Access denied</body></html>")) == (False, "robots_not_text")
    assert robots_allows(url, getter=lambda u: (410, ""))[0] is True


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_redirect_to_other_host_or_http_is_blocked():
    from rates_refresh.fetch import RedirectBlocked, safe_get

    def other_host(req):
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    def downgrade(req):
        return httpx.Response(301, headers={"location": "http://a.example/x"})

    with pytest.raises(RedirectBlocked):
        safe_get("https://a.example/rates", client=_client(other_host))
    with pytest.raises(RedirectBlocked):
        safe_get("https://a.example/rates", client=_client(downgrade))


def test_same_host_https_redirect_is_followed():
    from rates_refresh.fetch import safe_get

    def handler(req):
        if req.url.path == "/rates":
            return httpx.Response(302, headers={"location": "/rates/new"})
        return httpx.Response(200, text="ok")

    assert safe_get("https://a.example/rates", client=_client(handler)).text == "ok"


def test_http_fetch_reports_redirect_as_blocked(monkeypatch):
    from rates_refresh.fetch import RedirectBlocked
    monkeypatch.setattr(fetchmod, "safe_get", lambda url, **k: (_ for _ in ()).throw(RedirectBlocked("x")))
    r = fetchmod.http_fetch("https://a.example/rates")
    assert r.blocked and r.block_reason.startswith("redirect_blocked")


def test_browser_stops_before_any_interaction_when_blocked(monkeypatch):
    import sys
    import types

    log = []

    class Resp:
        status = 403

    class Page:
        url = "https://a.example/rates"
        def goto(self, *a, **k): log.append("goto"); return Resp()
        def inner_text(self, sel): return "Access denied. Incapsula incident"
        def evaluate(self, *a, **k): log.append("evaluate")
        def wait_for_timeout(self, ms): log.append("wait")
        def wait_for_load_state(self, *a, **k): log.append("load_state")
        def locator(self, sel): log.append("locator")

    class Browser:
        def new_context(self, **k): return types.SimpleNamespace(new_page=lambda: Page())
        def close(self): pass

    class PW:
        chromium = types.SimpleNamespace(launch=lambda **k: Browser())
        def __enter__(self): return self
        def __exit__(self, *a): pass

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: PW()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    r = fetchmod.browser_fetch("https://a.example/rates")
    assert r.blocked and r.block_reason == "http_403"
    assert log == ["goto"]  # no waits, clicks, expansions or tab handling


def _prev_full():
    p = prev_snapshot()["banks"][0]
    p["special"] = {"1y": 5.0, "2y": 4.9, "3y": 4.8}
    p["standard"] = {"1y": 5.8, "2y": 5.7}
    p["low_equity"]["bands"] = [{"lvr_from": 80.01, "lvr_to": 85, "margin_pct": 0.3}]
    return p


def test_lem_band_jump_over_1pp_rejected():
    e = ext({"1y": 5.0})
    e.bands = [Band(lvr_from=80.01, lvr_to=85, margin_pct=1.6)]
    rec, problems = build_bank(bank(), e, "h", _prev_full(), "t")
    assert rec is None and any("margin moved" in p for p in problems)


def test_lem_margin_above_3pp_or_overlap_rejected():
    e = ext({"1y": 5.0})
    e.bands = [Band(lvr_from=80.01, lvr_to=85, margin_pct=3.5)]
    assert build_bank(bank(), e, "h", None, "t")[0] is None
    e.bands = [Band(lvr_from=80.01, lvr_to=90, margin_pct=0.3), Band(lvr_from=85, lvr_to=95, margin_pct=0.5)]
    assert any("overlapping" in p for p in build_bank(bank(), e, "h", None, "t")[1])


def test_partial_extraction_keeps_old_terms_and_says_so():
    e = ext({"1y": 5.05}, standard=None, floating=None)
    e.bands = []
    rec, problems = build_bank(bank(), e, "h", _prev_full(), "t")
    assert problems == []
    assert rec["special"] == {"1y": 5.05, "2y": 4.9, "3y": 4.8}
    assert rec["standard"] == {"1y": 5.8, "2y": 5.7}
    assert rec["floating"] == 6.6 and rec["low_equity"]["bands"][0]["margin_pct"] == 0.3
    note = next(n for n in rec["notes"] if n.startswith("carried_over:"))
    assert "special.2y" in note and "standard.1y" in note and "floating" in note and "low_equity.bands" in note


def test_refresh_preserves_hand_maintained_fields():
    prev = _prev_full()
    prev["fhl"] = {"lender": True, "lmi_passthrough_pct": 1.2, "special_applies": True}
    prev["notes"] = ["Special rates need salary credited.", "stale_since: 2026-09-30"]
    cfg = bank()
    cfg["fhl"] = {"lender": False, "lmi_passthrough_pct": None}
    full = ext({"1y": 5.0, "2y": 4.9, "3y": 4.8}, standard={"1y": 5.8, "2y": 5.7})
    full.bands = [Band(lvr_from=80.01, lvr_to=85, margin_pct=0.3)]
    rec, _ = build_bank(cfg, full, "h", prev, "t")
    assert rec["fhl"] == {"lender": True, "lmi_passthrough_pct": 1.2, "special_applies": True}
    assert rec["notes"] == ["Special rates need salary credited."]  # stale marker cleared, manual note kept


def test_new_bank_takes_fhl_from_config_including_special_applies():
    cfg = bank()
    cfg["fhl"] = {"lender": True, "lmi_passthrough_pct": 1.2, "special_applies": True}
    rec, _ = build_bank(cfg, ext(), "h", None, "t")
    assert rec["fhl"]["special_applies"] is True


def test_same_second_runs_get_distinct_ids_and_snapshot_keys():
    import re
    db = FakeDB()
    store = FirestoreStore(client=db)
    run(store, lambda t, b: ext())
    run(store, lambda t, b: ext())
    runs = [k for k in db.data if k.startswith("job_runs/")]
    snaps = [k for k in db.data if k.startswith("rates_snapshots/")]
    assert len(runs) == 2 and len(snaps) == 2  # nothing overwritten (random suffix; 1-in-65536 clash is tolerable)
    assert all(re.fullmatch(r"job_runs/\d{8}T\d{6}Z-[0-9a-f]{4}", k) for k in runs)


def test_carried_values_keep_original_capture_time():
    prev = _prev_full()
    prev["captured_at"] = "2026-09-01T00:00:00+12:00"
    e = ext({"1y": 5.05})  # special 1y only; special 2y/3y and standard come from the old row
    e.bands = []
    e.floating = None
    rec, _ = build_bank(bank(), e, "h", prev, "2026-10-04T06:00:00+13:00")
    assert rec["captured_at"] == "2026-09-01T00:00:00+12:00"  # not refreshed
    assert rec["last_fetched_at"] == "2026-10-04T06:00:00+13:00"
    prov = rec["carried_over_captured_at"]
    assert prov["special.2y"] == prov["standard.1y"] == prov["floating"] == "2026-09-01T00:00:00+12:00"
    # a second partial refresh must not reset the clock of a value that is still carried
    rec2, _ = build_bank(bank(), e, "h2", rec, "2026-10-05T06:00:00+13:00")
    assert rec2["captured_at"] == "2026-09-01T00:00:00+12:00"
    assert rec2["carried_over_captured_at"]["special.2y"] == "2026-09-01T00:00:00+12:00"
    assert rec2["special"]["1y"] == 5.05 and rec2["special"]["2y"] == 4.9


def test_carried_row_without_own_capture_time_uses_snapshot_time():
    prev = _prev_full()
    prev.pop("captured_at", None)
    e = ext({"1y": 5.05})
    rec, _ = build_bank(bank(), e, "h", prev, "2026-10-04T06:00:00+13:00", "2026-08-15T00:00:00+12:00")
    assert rec["captured_at"] == "2026-08-15T00:00:00+12:00"


def test_complete_extraction_refreshes_time_and_clears_provenance():
    prev = _prev_full()
    prev["carried_over_captured_at"] = {"special.3y": "2026-09-01T00:00:00+12:00"}
    prev["notes"] = ["stale_since: 2026-09-30", "carried_over: special.3y kept from previous snapshot"]
    full = ext({"1y": 5.0, "2y": 4.9, "3y": 4.8}, standard={"1y": 5.8, "2y": 5.7})
    full.bands = [Band(lvr_from=80.01, lvr_to=85, margin_pct=0.3)]
    rec, _ = build_bank(bank(), full, "h", prev, "2026-10-04T06:00:00+13:00")
    assert rec["captured_at"] == "2026-10-04T06:00:00+13:00"
    assert "carried_over_captured_at" not in rec and rec["notes"] == []


def test_missing_lem_bands_keep_old_ones():
    prev = _prev_full()
    prev["low_equity"]["bands"] = [
        {"lvr_from": 80.01, "lvr_to": 85, "margin_pct": 0.3},
        {"lvr_from": 85.01, "lvr_to": 90, "margin_pct": 0.75},
    ]
    e = ext({"1y": 5.0, "2y": 4.9, "3y": 4.8}, standard={"1y": 5.8, "2y": 5.7})
    e.bands = [Band(lvr_from=80.01, lvr_to=85, margin_pct=0.4)]  # only the first band extracted
    rec, _ = build_bank(bank(), e, "h", prev, "t")
    assert rec["low_equity"]["bands"] == [
        {"lvr_from": 80.01, "lvr_to": 85, "margin_pct": 0.4},
        {"lvr_from": 85.01, "lvr_to": 90, "margin_pct": 0.75},
    ]
    assert "low_equity.bands[85.01-90]" in rec["carried_over_captured_at"]
    assert any(n.startswith("carried_over:") and "bands[85.01-90]" in n for n in rec["notes"])


def test_redirect_hop_is_checked_against_robots():
    from rates_refresh.fetch import RedirectBlocked, safe_get

    def handler(req):
        if req.url.path == "/rates":
            return httpx.Response(302, headers={"location": "/private/rates"})
        return httpx.Response(200, text="ok")

    seen = []

    def allow(url):
        seen.append(url)
        return (False, "robots_disallow") if "/private/" in url else (True, "ok")

    with pytest.raises(RedirectBlocked, match="robots_disallow"):
        safe_get("https://a.example/rates", client=_client(handler), allow=allow)
    assert seen == ["https://a.example/private/rates"]
    assert safe_get("https://a.example/rates", client=_client(handler), allow=lambda u: (True, "ok")).text == "ok"
