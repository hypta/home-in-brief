"""One request deadline (X-Request-Deadline-Ms), the reservation
inside it, X-Report-Degraded-Reason, model "code-draft", the request-wide stop after quota/config/refusal, and the
fixed fallback sentences in the asker's language (Chinese) or English (language "en").

Sockets are blocked (test_protection.no_network); the real SDK wrappers run against a fake google.genai.Client.
"""
import json
import threading
import time

import pytest
from conftest import body
from fastapi.testclient import TestClient

from app import cache, followup, gemini, kb, main, obs, protection
from app import followup_prompt as fp
from app.service import CODE_DRAFT
from test_api import GOOD
from test_followup import FLOOD_EN, FLOOD_JA, ans, cls
from test_leaky_user_facts import council_off
from test_protection import FakeGenai, counters, followup_brain, no_network, report_brain  # noqa: F401

client = TestClient(main.app, raise_server_exceptions=False)
HEADER = "X-Request-Deadline-Ms"
REASON = "x-report-degraded-reason"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(gemini, "clock", c)
    return c


def timeouts_ms(fake):
    return [o.timeout for o in fake.http_options]


def post_report(case="case1", headers=None, b=None):
    return client.post("/v1/report", json=b or body(case), headers=headers or {})


def post_followup(question, case="case1", headers=None, b=None, **extra):
    return client.post("/v1/followup", json=(b or body(case)) | {"question": question, "history": []} | extra,
                       headers=headers or {})


class SlowReserve(protection.MemoryBackend):
    """The memory allowance, with each reservation taking `seconds` on the fake clock."""

    def __init__(self, clock, seconds):
        super().__init__()
        self.clock, self.seconds, self.waits = clock, seconds, []

    def reserve(self, search, lim, day, wait_s=None):
        self.waits.append(wait_s)
        self.clock.t += self.seconds
        return super().reserve(search, lim, day, wait_s)


# ---- one deadline per request ----------------------------------------------------------------------------------
def test_header_shortens_the_deadline_drafts_without_a_call(monkeypatch, clock):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    r = post_report(headers={HEADER: "5000"})  # web has 5 s left: no time for a call (ADMIT_S 11)
    assert r.status_code == 200 and not fake.calls and counters() == (0, 0, 0)
    assert r.headers["x-report-mode"] == "drafts" and r.headers[REASON] == "deadline"
    assert r.json()["model"] == CODE_DRAFT


def test_header_never_extends_the_deadline_and_bad_values_are_ignored(monkeypatch, clock):
    monkeypatch.setenv("REQUEST_DEADLINE_S", "15")
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    for value in ("60000", "abc", "-5", "nan"):
        cache.REPORTS.clear()
        assert post_report(headers={HEADER: value}).headers["x-report-mode"] == "gemini"
    assert timeouts_ms(fake) == [14000] * 4  # 15 s left, 1 s margin: never the header's 60 s


def test_reservation_time_counts_and_the_timeout_is_recomputed(monkeypatch, clock):
    slow = SlowReserve(clock, 3.0)
    protection.set_backend("memory", slow)
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    r = post_report(headers={HEADER: "20000"})
    assert r.headers["x-report-mode"] == "gemini"
    assert slow.waits == [20.0]  # the reservation may wait only what the request has left
    assert timeouts_ms(fake) == [16000]  # 20 s - 3 s reservation - 1 s margin (not 19 s computed before it)


def test_no_time_after_the_reservation_keeps_it_and_answers_from_drafts(monkeypatch, clock):
    slow = SlowReserve(clock, 3.0)
    protection.set_backend("memory", slow)
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    r = post_report(headers={HEADER: "12000"})  # 12 s at admission, 9 s after the reservation
    assert r.status_code == 200 and not fake.calls
    assert slow.control["event_calls"] == 1  # the reservation stays counted (never refunded)
    assert r.headers["x-report-mode"] == "drafts" and r.headers[REASON] == "deadline"
    assert r.json()["warnings"][-1] == protection.GENERATION_DEGRADED


def test_kb_read_counts_against_the_deadline(monkeypatch, clock):
    monkeypatch.setenv("REQUEST_DEADLINE_S", "20")
    real = kb.load
    seen = []

    def slow_load(ids, timeout=5.0):
        seen.append(timeout)
        clock.t += 4.0
        return real(ids, timeout)

    monkeypatch.setattr(kb, "load", slow_load)
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    assert post_report().headers["x-report-mode"] == "gemini"
    assert seen == [5.0] and timeouts_ms(fake) == [15000]  # 20 - 4 - 1


def test_follower_uses_what_is_left_not_a_fresh_budget(monkeypatch, clock):
    monkeypatch.setenv("REPORT_CACHE_SIZE", "100")
    waits = []

    def wait(flight, timeout):
        waits.append(timeout)
        clock.t += timeout  # the leader never finishes in time
        return None

    monkeypatch.setattr(cache.REPORTS, "join", lambda k: (False, cache._Flight()))
    monkeypatch.setattr(cache, "wait", wait)
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    r = post_report(headers={HEADER: "24000"})
    assert r.status_code == 200 and waits == [cache.FOLLOWER_WAIT_S]
    assert timeouts_ms(fake) == [11000]  # 24 - 12 waited - 1: the follower did not start a new 25 s budget
    monkeypatch.setattr(cache, "wait", wait)
    waits.clear()
    fake.calls.clear()
    r = post_report(headers={HEADER: "8000"})  # waits at most what is left, then has no time to call
    assert waits == [8.0] and not fake.calls and r.headers[REASON] == "deadline"


def test_followup_shares_the_route_deadline(monkeypatch, clock):
    fake = FakeGenai(followup_brain(cls(language="ja"))).install(monkeypatch)
    r = post_followup("洪水のリスクはありますか？", headers={HEADER: "3000"})
    assert r.status_code == 200 and not fake.calls
    out = r.json()
    assert out["language"] == "en" and r.headers[REASON] == "deadline"  # fixed English sentences


# ---- the allowance transaction's whole life is bounded ------------------------------------------------------
class FakeApi:
    def __init__(self, delay=0.0):
        self.calls, self.delay = [], delay

    def _rpc(self, name, **kw):
        self.calls.append((name, kw.get("timeout"), kw.get("retry", "unset")))
        time.sleep(self.delay)
        return type("R", (), {"transaction": b"t1", "write_results": [], "commit_time": None})()

    def begin_transaction(self, **kw):
        return self._rpc("begin", **kw)

    def commit(self, **kw):
        return self._rpc("commit", **kw)

    def rollback(self, **kw):
        return self._rpc("rollback", **kw)


class FakeClient:
    def __init__(self, api):
        self._firestore_api, self._database_string, self._rpc_metadata = api, "projects/p/databases/(default)", []


def test_begin_commit_and_rollback_get_only_the_time_left():
    api = FakeApi()
    end = time.monotonic() + 0.5
    txn = protection._bounded_transaction(FakeClient(api), end)
    txn._begin()
    txn._commit()
    txn._begin()
    txn._rollback()
    assert [c[0] for c in api.calls] == ["begin", "commit", "begin", "rollback"]
    assert all(0 < c[1] <= 0.5 for c in api.calls) and all(c[2] is None for c in api.calls)  # no RPC retry
    late = protection._bounded_transaction(FakeClient(api), time.monotonic() - 0.01)
    with pytest.raises(TimeoutError):
        late._begin()


class StuckOnce(protection.FirestoreBackend):
    """The first transaction hangs past every limit (a stuck Firestore call); later ones succeed at once."""

    def __init__(self, hang=1.0):
        super().__init__(client_factory=lambda: None)
        self.hang, self.n, self.store = hang, 0, protection.MemoryBackend()

    def _transaction(self, search, lim, day, end):
        self.n += 1
        if self.n == 1:
            time.sleep(self.hang)
        return self.store.reserve(search, lim, day)


def test_a_stuck_transaction_does_not_block_the_next_reservation(monkeypatch):
    monkeypatch.setenv("PROTECTION_TIMEOUT_S", "0.2")
    b = StuckOnce()
    lim = protection.Limits(10, 10, 10)
    t0 = time.monotonic()
    assert b.reserve(False, lim, "d") == protection.BACKEND  # abandoned at the limit
    assert b.reserve(False, lim, "d") == protection.OK       # not held up by the stuck one
    assert time.monotonic() - t0 < 0.8


def test_in_flight_transactions_are_capped(monkeypatch):
    monkeypatch.setenv("PROTECTION_TIMEOUT_S", "0.1")
    b = StuckOnce(hang=0.6)
    b._transaction = lambda *a: time.sleep(0.6) or protection.OK  # every transaction hangs
    lim = protection.Limits(10, 10, 10)
    assert [b.reserve(False, lim, "d") for _ in range(protection.MAX_IN_FLIGHT)] == [protection.BACKEND] * 2
    t0 = time.monotonic()
    assert b.reserve(False, lim, "d") == protection.BACKEND
    assert time.monotonic() - t0 < 0.05  # refused at once, no new thread
    assert b._in_flight == protection.MAX_IN_FLIGHT


def test_wait_is_never_longer_than_the_request_has_left(monkeypatch):
    monkeypatch.setenv("PROTECTION_TIMEOUT_S", "3")
    b = StuckOnce(hang=1.0)
    t0 = time.monotonic()
    assert b.reserve(False, protection.Limits(10, 10, 10), "d", wait_s=0.1) == protection.BACKEND
    assert time.monotonic() - t0 < 0.5
    assert b.reserve(False, protection.Limits(10, 10, 10), "d", wait_s=0.0) == protection.BACKEND


def test_two_instances_with_late_commits_never_exceed_the_limit(monkeypatch):
    """Two backends (two instances) on shared documents; some transactions commit after their caller gave up.
    A late commit counts a call that was not made, never the other way round, and the daily limit holds."""
    monkeypatch.setenv("PROTECTION_TIMEOUT_S", "0.15")
    shared, lock = protection.MemoryBackend(), threading.Lock()

    class Late(protection.FirestoreBackend):
        def __init__(self, delays):
            super().__init__(client_factory=lambda: None)
            self.delays = list(delays)

        def _transaction(self, search, lim, day, end):
            time.sleep(self.delays.pop(0) if self.delays else 0.0)
            with lock:
                return shared.reserve(search, lim, day)

    a, b = Late([0.0, 0.3, 0.0, 0.3]), Late([0.3, 0.0, 0.0, 0.0])
    lim = protection.Limits(daily=3, search=1, event=100)
    out = [x.reserve(False, lim, "d") for x in (a, b, a, b, a, b, a, b)]
    time.sleep(0.5)  # the abandoned transactions finish (and may commit)
    calls = shared.days["d"]["calls"]
    assert calls <= 3 and calls >= out.count(protection.OK)
    assert set(out) <= {protection.OK, protection.DAILY, protection.BACKEND}


# ---- X-Report-Degraded-Reason ----------------------------------------------------------------------------------
@pytest.mark.parametrize("setup,reason", [
    ({"GEMINI_DAILY_CALLS": "0"}, "allowance_daily"),
    ({"GEMINI_EVENT_CALLS": "0"}, "allowance_event"),
    ({"REPORT_DEGRADED": "true"}, "disabled"),
    ({"GEMINI_INPUT_MAX_BYTES": "100"}, "prompt_too_large"),
])
def test_degraded_reason_header(monkeypatch, setup, reason):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    for k, v in setup.items():
        monkeypatch.setenv(k, v)
    r = post_report()
    assert r.status_code == 200 and not fake.calls
    assert r.headers["x-report-mode"] == "drafts" and r.headers[REASON] == reason


def test_degraded_reason_disabled_flag_and_backend_error(monkeypatch):
    FakeGenai(report_brain(GOOD)).install(monkeypatch)
    protection.backend().control["disabled"] = True
    assert post_report().headers[REASON] == "disabled"
    monkeypatch.setenv("PROTECTION_BACKEND", "firestore")
    protection.set_backend("firestore", protection.FirestoreBackend(client_factory=lambda: 1 / 0))
    assert post_report().headers[REASON] == "backend_error"


class ProviderError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} provider error")
        self.code = code


def test_model_error_reason_and_no_header_on_success(monkeypatch):
    fake = FakeGenai(report_brain(GOOD)).install(monkeypatch)
    ok = post_report()
    assert ok.headers["x-report-mode"] == "gemini" and REASON not in ok.headers

    def broken(prompt, schema):
        raise ProviderError(503)

    fake.brain = broken
    cache.REPORTS.clear()
    r = post_report()
    assert r.headers["x-report-mode"] == "drafts" and r.headers[REASON] == "model_error"
    monkeypatch.delenv("GEMINI_API_KEY")  # a missing key is a configuration error: model_error too
    assert post_report().headers[REASON] == "model_error"


def test_followup_reason_only_when_it_fell_back(monkeypatch):
    FakeGenai(followup_brain(cls(out_of_scope=True, searchable=False))).install(monkeypatch)
    r = post_followup("Which school zone is this in?")  # the fixed refusal is drafts, but nothing failed
    assert r.headers["x-report-mode"] == "drafts" and REASON not in r.headers
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    r = post_followup("Is there a flood risk?")
    assert r.headers["x-report-mode"] == "drafts" and r.headers[REASON] == "allowance_daily"


# ---- model "code-draft" ----------------------------------------------------------------------------------------
def test_all_draft_summary_says_code_draft_and_mixed_keeps_the_model(monkeypatch):
    FakeGenai(report_brain(GOOD)).install(monkeypatch)
    mixed = post_report().json()
    assert mixed["model"] == gemini.model_name()
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    cache.REPORTS.clear()
    assert post_report().json()["model"] == CODE_DRAFT


# ---- one stop for the whole request ----------------------------------------------------------------------------
class StageGenai(FakeGenai):
    """Answers every task; raises a provider 429 at `fail` (classify, search, extract, answer, translate)."""

    def __init__(self, fail, classify, answers=(), search_text="Listing pages say when this home was built only vaguely."):
        super().__init__()
        self.fail, self.classify, self.answers, self.search_text, self.tasks = fail, classify, list(answers), \
            search_text, []

    def reply(self, prompt, config):
        from google.genai import types
        task = "search" if config.tools else next(
            t for t, m in (("classify", fp.TASK_CLASSIFY), ("answer", fp.TASK_ANSWER), ("translate", fp.TASK_TRANSLATE),
                           ("extract", fp.TASK_EXTRACT)) if prompt.startswith(m))
        self.tasks.append(task)
        if task == self.fail:
            raise ProviderError(429)
        if task == "search":
            chunk = types.GroundingChunk(web=types.GroundingChunkWeb(uri="https://redirect.invalid/1", title="x.nz"))
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text=self.search_text)]),
                grounding_metadata=types.GroundingMetadata(
                    grounding_chunks=[chunk], search_entry_point=types.SearchEntryPoint(rendered_content="")))])
        out = {"classify": self.classify, "extract": {"year_built": None, "cladding": None, "quotes": {}},
               "answer": self.answers.pop(0) if len(self.answers) > 1 else (self.answers or [{"answer": []}])[0],
               "translate": None}[task]
        if task == "translate":
            out = {"texts": json.loads(prompt.split("TEXTS:", 1)[1])}
        return types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(out))]))])


@pytest.mark.parametrize("fail,question,classify,answers,expected", [
    ("classify", "Is there a flood risk?", cls(), [ans(FLOOD_EN)], ["classify"]),
    ("search", "When was this home built? Is there a flood risk?", cls(), [ans(FLOOD_EN)], ["classify", "search"]),
    ("extract", "When was this home built? Is there a flood risk?", cls(), [ans(FLOOD_EN)],
     ["classify", "search", "extract"]),
    ("answer", "洪水のリスクはありますか？", cls(language="ja"), [ans(FLOOD_JA)], ["classify", "answer"]),
    # an English rewrite for a Japanese question is blocked: its fact's English draft needs a translation
    ("translate", "洪水のリスクはありますか？", cls(language="ja"), [ans(FLOOD_EN)],
     ["classify", "answer", "answer", "translate"]),
])
def test_quota_at_any_stage_stops_every_later_call(monkeypatch, fail, question, classify, answers, expected):
    fake = StageGenai(fail, classify, answers).install(monkeypatch)
    r = post_followup(question)
    assert r.status_code == 200, r.text
    assert fake.tasks == expected  # nothing after the 429: no retry, no answer, no translation
    assert counters()[0] == len(expected)  # and nothing more reserved


@pytest.mark.parametrize("daily,question,classify,answers,reserved", [
    (0, "Is there a flood risk?", cls(), [ans(FLOOD_EN)], 1),                    # classify refused
    (1, "When was this home built? Is there a flood risk?", cls(), [ans(FLOOD_EN)], 2),  # search refused
    (2, "When was this home built? Is there a flood risk?", cls(), [ans(FLOOD_EN)], 3),  # extraction refused
    (1, "洪水のリスクはありますか？", cls(language="ja"), [ans(FLOOD_JA)], 2),           # answer refused
    (3, "洪水のリスクはありますか？", cls(language="ja"), [ans(FLOOD_EN)], 4),  # translation refused
])
def test_allowance_refusal_at_any_stage_stops_every_later_reservation(monkeypatch, daily, question, classify,
                                                                      answers, reserved):
    tries = []
    mem = protection.backend()
    real = mem.reserve
    monkeypatch.setattr(mem, "reserve", lambda *a, **k: tries.append(1) or real(*a, **k))
    monkeypatch.setenv("GEMINI_DAILY_CALLS", str(daily))
    StageGenai(None, classify, answers).install(monkeypatch)
    r = post_followup(question)
    assert r.status_code == 200, r.text
    assert len(tries) == reserved  # the refused one is the last reservation tried
    assert r.json()["warnings"] == [protection.GENERATION_DEGRADED]


def test_search_allowance_alone_does_not_stop_the_answer(monkeypatch):
    fake = StageGenai(None, cls(), [ans(FLOOD_EN)]).install(monkeypatch)
    monkeypatch.setenv("SEARCH_DAILY_CALLS", "0")
    r = post_followup("When was this home built? Is there a flood risk?")
    assert r.status_code == 200 and fake.tasks == ["classify", "answer"]
    assert r.headers["x-report-mode"] == "gemini" and r.json()["warnings"] == [followup.SEARCH_UNAVAILABLE]


# ---- fixed sentences in the asker's language when written, else English ------------------------------------------
def test_chinese_question_after_the_allowance_gets_chinese_fixed_sentences(monkeypatch):
    fake = FakeGenai(followup_brain(cls(language="zh-Hant"))).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    r = post_followup("這裡有淹水風險嗎？")
    out = r.json()
    assert r.status_code == 200 and not fake.calls and out["language"] == "zh-Hant"
    assert out["answer"][0]["text"] in (followup.FALLBACK_LEAD["zh-Hant"], followup.FALLBACK_NONE["zh-Hant"])
    assert all(followup.script_matches(s["text"], "zh-Hant") for s in out["answer"])  # no English sentence
    assert out["links"]


def test_chinese_fixed_program_sentence_for_a_missing_field(monkeypatch):
    FakeGenai(followup_brain(cls(language="zh-Hant"))).install(monkeypatch)
    monkeypatch.setattr(followup, "call_rules", lambda *a: pytest.fail("rules-api"))
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    out = post_followup("外牆是什麼材質？", b=council_off()).json()
    assert out["language"] == "zh-Hant"
    texts = [s["text"] for s in out["answer"]]
    assert texts[0] == followup.FALLBACK_LEAD["zh-Hant"]
    assert "沒有免費的公開來源記錄外牆材質，因此無法確認；如果你知道，可以在表單中填寫。" in texts


def test_simplified_chinese_fixed_sentences(monkeypatch):
    FakeGenai(followup_brain(cls(language="zh-Hans"))).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    out = post_followup("这里有淹水风险吗？").json()
    assert out["language"] == "zh-Hans"
    assert out["answer"][0]["text"] in (followup.FALLBACK_LEAD["zh-Hans"], followup.FALLBACK_NONE["zh-Hans"])


def test_japanese_question_after_the_allowance_gets_english_and_says_so(monkeypatch):
    fake = FakeGenai(followup_brain(cls(language="ja"))).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    r = post_followup("洪水のリスクはありますか？")
    out = r.json()
    assert r.status_code == 200 and not fake.calls
    assert out["language"] == "en"
    assert out["answer"][0]["text"] in (followup.FALLBACK_LEAD["en"], followup.FALLBACK_NONE["en"])
    assert all(followup.script_matches(s["text"], "en") for s in out["answer"])  # no Japanese, no translation call


def test_japanese_refusal_without_a_translation_is_english(monkeypatch):
    fake = FakeGenai(followup_brain(cls(language="ja", out_of_scope=True, searchable=False))).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "1")  # classify passes; the refusal's translation is refused
    out = post_followup("この家を買うべきですか？").json()
    assert len(fake.calls) == 1
    assert out["language"] == "en" and out["answer"][0]["text"] == followup.REFUSAL_EN


def test_english_question_after_the_allowance_is_unchanged(monkeypatch):
    FakeGenai(followup_brain(cls())).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    out = post_followup("Is there a flood risk?").json()
    assert out["language"] == "en" and out["answer"]
    assert out["answer"][0]["text"] not in (followup.FALLBACK_LEAD["en"], followup.FALLBACK_NONE["en"])


def test_a_question_the_program_cannot_place_gets_the_fixed_sentence_after_the_allowance(monkeypatch):
    FakeGenai(followup_brain(cls())).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    out = post_followup("Which schools are nearby?").json()  # was answered with the flood, title and valuation facts
    assert [s["text"] for s in out["answer"]] == [followup.FALLBACK_NONE["en"]] and out["links"]


def test_fixed_chinese_sentences_pass_the_chinese_checks():
    from app.validator import Evidence
    for lang in ("zh-Hant", "zh-Hans"):
        for table in (followup.FALLBACK_LEAD, followup.FALLBACK_NONE):
            from b_common.report_schemas import Sentence
            fails = followup.check_answer(Sentence(text=table[lang], source_ids=[]), Evidence(), lang)
            assert set(fails) <= {"no_source_ids"}, (lang, fails)
        for en in (*followup.facts_prompt.NO_SOURCE_TEXT.values(), "The year built could not be checked from public records.",
                   "Council records could not be read from our server, so the cladding is unknown."):
            zh = followup.zh_fixed(en, lang)
            assert zh and followup.script_matches(zh, lang)
    assert followup.zh_fixed("Something else entirely.", "zh-Hant") is None


def test_completion_record_has_the_degraded_reason(monkeypatch, caplog):
    FakeGenai(report_brain(GOOD)).install(monkeypatch)
    monkeypatch.setenv("GEMINI_DAILY_CALLS", "0")
    caplog.set_level("INFO")
    post_report()
    rec = [x.json_fields for x in caplog.records if getattr(x, "json_fields", {}).get("event") == "request_complete"]
    assert rec[-1]["degraded_reason"] == "allowance_daily" and obs.current() is None
