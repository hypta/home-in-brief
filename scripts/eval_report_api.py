"""Evaluate report-api against the golden cases (out/golden/RUBRIC.md section 8).

Runs each golden case N times, either through the service function (default, needs no server) or a
running report-api (--url), and records per run: status, passed sentences, dropped (with reasons in
direct mode), hard-rule violations in the returned report, and the RUBRIC must / must-not / next-step
assertions. Assertions that cannot be automated are listed as manual.

  set -a; . ./.env; set +a
  .venv/bin/python scripts/eval_report_api.py --runs 3                 # real Gemini, direct
  REPORT_MOCK=true .venv/bin/python scripts/eval_report_api.py --runs 1
  .venv/bin/python scripts/eval_report_api.py --url http://localhost:8090

Results go to out/golden/eval/<timestamp>/ (results.json, summary.md); out/ is not in git.

Rewrite metrics (direct mode, from the service debug record): per run, how many summary sentences are Gemini
rewrites that passed, how many are fact drafts (a blocked rewrite's fallback, or a must fact no sentence used),
and the text similarity (difflib ratio) of each rewrite to the drafts of the facts it uses.

  .venv/bin/python scripts/eval_report_api.py --followup     # real follow-up questions for case1 and case5
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "report-api"
sys.path[:0] = [str(SERVICE), str(ROOT / "shared")]
os.environ.setdefault("KB_SOURCE", "local")

from b_common.report_schemas import Report, ReportRequest, Sentence  # noqa: E402

from app import assemble, kb, prompt, validator as V  # noqa: E402
from app.service import Debug, ReportError, build_report, debug_dict  # noqa: E402

FIXTURES = SERVICE / "fixtures" / "inputs"
PRIMARY4 = ["ac_flood_plains", "ac_flood_prone", "ac_overland_flow", "ac_coastal"]
LVR = ("rbnz_lvr", "kb_rbnz_lvr")
DTI = ("rbnz_dti", "kb_rbnz_dti")
PREFIX = r"^Based on what you told us, "

# RUBRIC.md section 8 as checks. ("S", regex|None, [ids all cited] | tuple(any of ids)): a summary sentence
# matching regex (case-insensitive) that cites the ids. ("not", regex): no summary sentence matches.
# ("N", kb_id): a next step carries kb_id. ("N_no_lending",): no lending KB item in next steps.
# ("manual", text): needs a human reader.
CHECKS = {
    "case1": [
        ("S", r"51\.2", ["ac_flood_plains"]), ("S", r"50\.8", ["ac_flood_prone"]), ("S", None, ["ac_overland_flow"]),
        ("S", None, ["kb_rbnz_lvr"]), ("S", None, ["kainga_ora_fhl"]), ("S", r"freehold", ["linz_titles"]),
        ("S", r"reserve land|recreational", ["ac_valuation"]),
        ("not", r"\b90\b"), ("not", r"\b5\.6\b"), ("not", r"720,?000"), ("not", r"772,?000"), ("not", r"\bsafe\b"),
        ("N", "kb_flood_insurance_limit"), ("N", "kb_overland_flow_path"), ("N", "kb_rbnz_lvr"),
    ],
    "case2": [
        ("S", r"0\.5", ["ac_coastal"]), ("S", r"intersect", ["ac_flood_plains"]), ("S", None, ["kb_rbnz_lvr"]),
        ("S", None, ["kb_rbnz_dti"]), ("S", None, ["kainga_ora_fhl"]),
        ("S", r"not be confirmed|could not confirm|unconfirmed|not confirmed", ["linz_titles"]),
        ("S", r"match|single property|could not|not found|not confirmed|unconfirmed", ("ac_valuation", "ac_property_page")),
        ("not", r"\b0\.0\b"), ("not", r"\b85\b"), ("not", r"\b7\.46\b"),
        ("N", "kb_coastal_inundation_meaning"), ("N", "kb_flood_insurance_limit"),
    ],
    "case3": [
        ("S", None, ["ac_overland_flow"]), ("S", r"\bhigh\b", ["ac_landslide", "kb_landslide_map_context"]),
        ("S", r"LVR|loan-to-value", LVR), ("S", r"DTI|debt-to-income", DTI),
        ("S", r"first home loan.*(not|above|over)|(not|above|over).*first home loan", ["kainga_ora_fhl"]),
        ("S", r"cross lease", None),
        ("not", r"\b85\b"), ("not", r"\b7\.44\b"),
        ("N", "kb_overland_flow_path"), ("N", "kb_title_cross_lease"),
    ],
    "case4": [
        ("S", r"not show|do not|does not|show no", PRIMARY4), ("S", r"LVR|loan-to-value", LVR), ("S", r"DTI|debt-to-income", DTI),
        ("S", None, ["kainga_ora_fhl"]), ("S", r"unit title", None), ("S", r"restaurant|retail", ["ac_valuation"]),
        ("S", r"(year|built|build).*(not|could not|no )|(not|could not|no ).*(year|built|build)", None),
        ("not", r"\b95\b"), ("not", r"\b7\.44\b"), ("not", r"no risk"),
        ("N", "kb_rbnz_lvr"), ("N", "kb_title_unit_title"),
    ],
    "case5": [
        ("S", r"not show|do not|does not|show no", PRIMARY4), ("S", r"1990s", ["kb_leaky_build_era"]),
        ("S", PREFIX + r".*monolithic", None),
        ("S", r"same decade|consistent|falls (with)?in|match", None),
        ("not", r"no (other |existing )?debts"), ("S", r"unit title", None),  # the page says it on the DTI line
        ("year", r"1998"), ("not", r"\bdiffer"), ("not", r"\b65\b"), ("not", r"\b2\.27\b"),
        ("N", "kb_leaky_build_era"), ("N", "kb_monolithic_cladding"),
    ],
    "edge1": [
        ("S", r"could not be checked|not be checked|unknown|could not|unavailable", ("ac_flood_plains", "ac_flood_prone",
                                                                          "ac_overland_flow", "ac_coastal")),
        ("S", r"no loan scenario", None),
        ("S", r"(title|year).*(not|could not)", None),
        ("not", r"no hazard"), ("not", r"no flood"), ("not", r"\bsafe\b"),
        ("N", "kb_lim_next_step"), ("N_no_lending",),
    ],
    "edge2": [
        ("S", r"deposit is larger|deposit exceeds", None), ("S", r"cross lease", None), ("S", r"1960s", None),
        ("not", r"\bLVR\b"), ("not", r"\bDTI\b"), ("not", r"first home loan"), ("not", r"\b0\b"),
        ("N_no_lending",),
    ],
    "edge3": [
        ("S", None, ["ac_overland_flow"]), ("S", r"LVR|loan-to-value", LVR), ("S", r"DTI|debt-to-income", DTI),
        ("S", None, ["kainga_ora_fhl"]), ("S", r"cross lease", None),
        ("not", r"\b95\b"), ("not", r"\b15\.5\b"),
        ("N", "kb_rbnz_lvr"), ("N", "kb_rbnz_dti"),
    ],
    "edge4": [
        ("S", r"leasehold", ["linz_titles"]), ("S", PREFIX + r".*1975", None), ("S", PREFIX + r".*monolithic", None),
        ("year", r"1999"), ("not", r"\b80\b"),
        ("N", "kb_monolithic_cladding"),
    ],
}
# case1 with the user's 1998 + monolithic (inputs_council_off/case1_filled): case1's checks plus the leaky-building
# user fact (must, prefixed) and both triggered items in next steps
CHECKS["case1_filled"] = CHECKS["case1"] + [
    ("S", PREFIX + r".*leaky", ["kb_monolithic_cladding"]), ("N", "kb_monolithic_cladding"), ("N", "kb_leaky_build_era"),
]
MANUAL = [
    "Topic order follows RUBRIC 1 (hazards, lending, FHL, title, non-residential use, build era, not checked).",
    "Plain language; LVR and DTI explained once; one topic per sentence.",
    "User-supplied facts are not presented as verified; property data wins on conflict.",
]


def _cites(s: dict, ids) -> bool:
    if ids is None:
        return True
    if isinstance(ids, tuple):
        return any(i in s["source_ids"] for i in ids)
    return all(i in s["source_ids"] for i in ids)


def rubric(case: str, rep: dict, kb_items: dict, year_source: str = "user") -> list[dict]:
    """year_source: where the chat-supplied year built came from ("user" or "web_search"). A ("year", n) check wants
    the year the user typed, shown next to "you gave" with the prefix; a year a web search found must not appear."""
    out = []
    summary = rep.get("summary", [])
    steps = rep.get("next_steps", [])
    for chk in CHECKS.get(case, []):
        kind = chk[0]
        if kind == "S":
            rx, ids = chk[1], chk[2]
            ok = any((rx is None or re.search(rx, s["text"], re.I)) and _cites(s, ids) for s in summary)
            label = f"must: {rx or ''} {ids or ''}".strip()
        elif kind == "not":
            ok = not any(re.search(chk[1], s["text"], re.I) for s in summary)
            label = f"must not: {chk[1]}"
        elif kind == "year":
            if year_source == "web_search":
                ok = not any(re.search(rf"\b{chk[1]}\b", s["text"]) for s in summary)
                label = f"must not: {chk[1]} (year found by a web search)"
            else:
                ok = any(re.search(PREFIX + rf".*you gave, (?:the )?{chk[1]}\b", s["text"], re.I) for s in summary)
                label = f"must: you gave, {chk[1]} (year the user typed)"
        elif kind == "N":
            ok = any(chk[1] in st["source_ids"] for st in steps)
            label = f"next includes: {chk[1]}"
        else:
            ok = not any(kb_items.get(i, {}).get("topic") == "lending" for st in steps for i in st["source_ids"])
            label = "next has no lending items"
        out.append({"kind": kind, "check": label, "ok": ok})
    return out


def hard_checks(req: ReportRequest, rep: dict, kb_items: dict, ev) -> list[str]:
    """Rules any returned report must satisfy; each string is one violation."""
    bad = []
    fixed = set(assemble.NO_LOAN_SENTENCES.values()) | {assemble.MOCK_SENTENCE}
    summary = rep.get("summary", [])
    model_sentences = [s for s in summary if s["text"] not in fixed]
    year_facts = prompt.fact_list(req, ev, kb_items)
    for s in model_sentences:
        fails = V.check_sentence(Sentence(**s), V.with_user_year(ev, year_facts, s["text"]), V.MAX_WORDS if rep.get("model") != "mock" else None)
        if fails:
            bad.append(f"summary sentence fails {fails}: {s['text'][:80]}")
        if V.relies_on_user_input(Sentence(**s), ev) and not s["text"].startswith(V.PREFIX):
            bad.append(f"missing user-input prefix: {s['text'][:80]}")
    if not 1 <= len(model_sentences) <= 5 and rep.get("model") != "mock":
        bad.append(f"{len(model_sentences)} model sentences")
    steps = rep.get("next_steps", [])
    if len(steps) > assemble.MAX_NEXT_STEPS:
        bad.append(f"{len(steps)} next steps")
    selected = set(req.selected_kb_ids())
    for st in steps:
        # assemble merges steps with the same or nearly the same text (identical texts; similar First Home Loan steps,
        # assemble._merge_similar): the text is one source's next_step_en verbatim and every source is selected
        for i in st["source_ids"]:
            if i not in selected:
                bad.append(f"next step cites an item that is not selected: {i}")
        if not any(kb_items.get(i, {}).get("next_step_en") == st["text"] for i in st["source_ids"]):
            bad.append(f"next step not verbatim from a selected item: {st['source_ids']}")
    if len({st["text"] for st in steps}) != len(steps):
        bad.append("duplicate next step text")
    if not set(rep.get("warnings", [])) <= set(req.warnings()) | {"kb_includes_draft"}:
        bad.append("warning not from rules_output")
    if any(w.startswith(("lem_base_unverified:", "bank_skipped:")) for w in rep.get("warnings", [])):
        bad.append("panel-level warning kept")
    expected_nf = {n.field for n in assemble.not_found_notes(req)}
    if {n["field"] for n in rep.get("not_found_notes", [])} != expected_nf:
        bad.append("not_found_notes fields differ from the predicate")
    return bad


def run_once(case: str, body: dict, url: str | None) -> tuple[int, dict, dict | None, float]:
    t0 = time.monotonic()
    if url:
        import requests

        r = requests.post(url.rstrip("/") + "/v1/report", json=body, timeout=60)
        return r.status_code, r.json(), None, time.monotonic() - t0
    dbg = Debug()
    try:
        rep = build_report(ReportRequest.model_validate(body), debug=dbg).model_dump()
        status = 200
    except ReportError as e:
        rep, status = {"error": {"code": e.code, "message": e.message}}, e.status
    return status, rep, debug_dict(dbg), time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--url", help="call a running report-api instead of the service function")
    ap.add_argument("--cases", nargs="*", help="case names (default: every json in the inputs directory that has CHECKS)")
    ap.add_argument("--inputs", default=str(FIXTURES), help="directory of <case>.json inputs; a name like case1_filled "
                    "uses the checks of case1")
    ap.add_argument("--out", default=str(ROOT / "out" / "golden" / "eval"))
    ap.add_argument("--followup", action="store_true", help="real follow-up questions for case1 and case5")
    ap.add_argument("--questions", nargs="*", help="with --followup: these questions instead of the default ones; "
                    "--inputs and --cases pick the cases")
    args = ap.parse_args()
    if args.followup:
        questions = [(f"q{n}", q) for n, q in enumerate(args.questions, 1)] if args.questions else FOLLOWUP_QUESTIONS
        inputs = Path(args.inputs) if args.questions else FIXTURES
        return followup_eval(Path(args.out), inputs, args.cases or ["case1", "case5"], questions)

    inputs = Path(args.inputs)
    cases = args.cases or sorted(p.stem for p in inputs.glob("*.json") if p.stem.split("_")[0] in CHECKS)
    out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        d = json.loads((inputs / f"{case}.json").read_text(encoding="utf-8"))
        body = {k: d[k] for k in ("property_context", "user_inputs", "rules_output")}
        req = ReportRequest.model_validate(body)
        data = kb.load(req.selected_kb_ids())
        ev = V.build_evidence(req, data.items, data.sources.keys())
        for n in range(args.runs):
            status, rep, dbg, secs = run_once(case, body, args.url)
            row = {"case": case, "run": n + 1, "status": status, "seconds": round(secs, 2), "report": rep, "debug": dbg}
            if status == 200:
                Report.model_validate(rep)
                fixed = set(assemble.NO_LOAN_SENTENCES.values()) | {assemble.MOCK_SENTENCE}
                row["passed"] = sum(s["text"] not in fixed for s in rep["summary"])
                row["dropped"] = rep["dropped"]
                row["hard_violations"] = hard_checks(req, rep, data.items, ev)
                row["rubric"] = rubric(case if case in CHECKS else case.split("_")[0], rep, data.items,
                                            prompt.chat_sources(req).get("year_built", "user"))
            else:
                row["error"] = rep.get("error", {}).get("code")
            if dbg:
                row["drop_reasons"] = [f for a in dbg["attempts"] for s in a["sentences"] for f in s["fails"]]
                row["attempts"] = len(dbg["attempts"])
                row["rewrite"] = rewrite_metrics(dbg)
            results.append(row)
            print(_line(row), flush=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    (out_dir / "summary.md").write_text(_markdown(results, args))
    print(f"\nwritten: {out_dir}/results.json, {out_dir}/summary.md")
    return 0


def rewrite_metrics(dbg: dict) -> dict:
    """Rewritten vs draft sentences in the final summary, and similarity of each rewrite to its facts' drafts."""
    drafts = {f["id"]: f["draft"] for f in dbg.get("facts", [])}
    comp = dbg.get("composed", [])
    sims, copies = [], 0
    for c in comp:
        if c["kind"] != "rewrite":
            continue
        ref = " ".join(drafts.get(i, "") for i in c["fact_ids"])
        sims.append(round(difflib.SequenceMatcher(None, c["text"], ref).ratio(), 3))
        copies += c["text"] == ref
    st = dbg.get("rewrite", {})
    return {"rewritten": st.get("rewritten", 0), "fallback_drafts": st.get("fallback_drafts", 0),
            "must_added": st.get("must_added", 0), "blocked": st.get("blocked", 0),
            "model_sentences": st.get("model_sentences", 0), "similarity": sims, "identical_to_draft": copies}


def _rates(row: dict) -> tuple[int, int, int, int]:
    r = row.get("rubric", [])
    must = [x for x in r if x["kind"] == "S"]
    other = [x for x in r if x["kind"] != "S"]
    return sum(x["ok"] for x in must), len(must), sum(x["ok"] for x in other), len(other)


def _line(row: dict) -> str:
    if row["status"] != 200:
        return f"{row['case']:6} run {row['run']}: HTTP {row['status']} {row.get('error')} ({row['seconds']}s)"
    mh, mn, oh, on = _rates(row)
    rw = row.get("rewrite") or {}
    return (f"{row['case']:6} run {row['run']}: passed {row['passed']} dropped {row['dropped']} "
            f"hard {len(row['hard_violations'])} must {mh}/{mn} other {oh}/{on} "
            f"rewritten {rw.get('rewritten', '-')} drafts {rw.get('fallback_drafts', 0) + rw.get('must_added', 0)} "
            f"reasons {sorted(set(row.get('drop_reasons', [])))} ({row['seconds']}s)")


def _markdown(results: list[dict], args) -> str:
    lines = [f"# report-api eval {datetime.now().isoformat(timespec='seconds')}", "",
             f"Mode: {'http ' + args.url if args.url else 'direct'}; runs per case: {args.runs}; "
             f"REPORT_MOCK={os.environ.get('REPORT_MOCK', '')}; GEMINI_MODEL={os.environ.get('GEMINI_MODEL', '(default)')}",
             ""] + _totals(results) + [
             "", "| case | run | status | passed | dropped | hard | must mention | must not / next | rewritten | drafts "
             "(fallback+must) | mean similarity | drop reasons |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in results:
        if row["status"] != 200:
            lines.append(f"| {row['case']} | {row['run']} | {row['status']} {row.get('error')} | | | | | | "
                         f"{', '.join(sorted(set(row.get('drop_reasons', []))))} |")
            continue
        mh, mn, oh, on = _rates(row)
        rw = row.get("rewrite") or {}
        sims = rw.get("similarity") or []
        lines.append(f"| {row['case']} | {row['run']} | 200 | {row['passed']} | {row['dropped']} | "
                     f"{len(row['hard_violations'])} | {mh}/{mn} | {oh}/{on} | {rw.get('rewritten', '')} | "
                     f"{rw.get('fallback_drafts', 0)}+{rw.get('must_added', 0)} | "
                     f"{(sum(sims) / len(sims)) if sims else 0:.2f} | "
                     f"{', '.join(sorted(set(row.get('drop_reasons', []))))} |")
    lines += ["", "## Failed checks and summaries", ""]
    for row in results:
        lines.append(f"### {row['case']} run {row['run']}")
        for x in row.get("rubric", []):
            if not x["ok"]:
                lines.append(f"- FAIL {x['check']}")
        for v in row.get("hard_violations", []):
            lines.append(f"- HARD {v}")
        kinds = [c["kind"] for c in (row.get("debug") or {}).get("composed", [])]
        for n, s in enumerate((row.get("report") or {}).get("summary", [])):
            kind = kinds[n] if n < len(kinds) else "fixed"
            lines.append(f"  - S [{kind}]: {s['text']} `{', '.join(s['source_ids'])}`")
        for a in (row.get("debug") or {}).get("attempts", []):
            for s in a["sentences"]:
                if s["fails"]:
                    lines.append(f"  - DROPPED {s['fails']}: {s['text']} `{', '.join(s['source_ids'])}`")
            if a.get("error"):
                lines.append(f"  - ATTEMPT ERROR: {a['error']}")
        lines.append("")
    lines += ["## Manual checks (not automated)", ""] + [f"- {m}" for m in MANUAL] + [""]
    return "\n".join(lines)


FOLLOWUP_QUESTIONS = [
    ("en_general", "What do the hazard maps and the title type mean for this property?"),
    ("zh_year_cladding", "這間是 1997 年蓋的，外牆是 monolithic，要注意什麼？"),
    ("out_of_scope", "Is this house in a good school zone?"),
    ("definition", "What is a LIM report?"),
    ("advice", "Should I buy this house?"),
    ("my_lvr", "What is my LVR?"),
    ("mixed_advice", "Should I buy this house? It's in a flood plain."),
    ("diligence", "Should I worry about flooding here?"),
]
# What each default question should do, scored apart from the answer's wording. `route`: the route prefix expected
# (debug["route"]); `search`: whether the question is one the program would send to a Google Search. This eval never
# searches (answer_followup's web_search is off, and the Gemini API terms do not allow storing real results), so a
# search is "wanted" when the route reached the search step with an eligible question (debug["search_wanted"]).
FOLLOWUP_EXPECT = {
    "en_general": {"route": "facts", "search": False},
    "zh_year_cladding": {"route": "facts", "search": False},
    "out_of_scope": {"route": "refusal", "search": True},
    "definition": {"route": "insufficient", "search": True},
    "advice": {"route": "refusal:advice", "search": False},
    "my_lvr": {"route": "facts", "search": False},
    "mixed_advice": {"route": "mixed", "search": False},  # the flood facts' drafts, after the fixed decline sentence
    "diligence": {"route": "facts", "search": False},
}


def route_row(qid: str, dbg: dict) -> dict:
    """How a follow-up was routed, apart from how well it was answered: the route taken, why it said it could not answer,
    whether the question may be searched, whether the program would have searched, and (for a default question) whether
    that matches what is expected. The search-enabled routes themselves are tested with a fake SDK in
    services/report-api/tests/test_followup_search_routes.py, not here."""
    route = dbg.get("route") or "facts"
    wanted = bool(dbg.get("search_wanted"))
    row = {"route": route, "insufficient": dbg.get("insufficient"), "search_eligible": dbg.get("search_eligible"),
           "search_wanted": wanted, "search_failed": dbg.get("search_failed")}
    exp = FOLLOWUP_EXPECT.get(qid)
    row["expected"] = exp
    row["route_ok"] = None if exp is None else (route.startswith(exp["route"]) and wanted == exp["search"])
    return row


def followup_eval(out: Path, inputs: Path = FIXTURES, cases=("case1", "case5"), questions=None) -> int:
    """Each case and question (default: case1 and case5, the FOLLOWUP_QUESTIONS) through followup.answer_followup (real
    Gemini; the rules-api re-run goes to RULES_API_URL). Writes out/golden/eval/followup_rewrite_<ts>.json."""
    from app import followup

    rows = []
    for case in cases:
        d = json.loads((inputs / f"{case}.json").read_text(encoding="utf-8"))
        base = {k: d[k] for k in ("property_context", "user_inputs", "rules_output")}
        for qid, q in questions or FOLLOWUP_QUESTIONS:
            dbg: dict = {}
            t0 = time.monotonic()
            try:
                resp = followup.answer_followup(followup.FollowupRequest.model_validate(base | {"question": q}),
                                                debug=dbg).model_dump()
                status = 200
            except ReportError as e:
                resp, status = {"error": {"code": e.code, "message": e.message}}, e.status
                dbg["route"] = "error:" + e.code
            row = {"case": case, "question_id": qid, "question": q, "status": status,
                   "seconds": round(time.monotonic() - t0, 2), "response": resp,
                   "rewrite": dbg.get("rewrite"), "composed": dbg.get("composed"), "attempts": dbg.get("attempts"),
                   "program": dbg.get("program"), **route_row(qid, dbg)}
            if dbg.get("composed"):
                drafts = {f["id"]: f["draft"] for f in dbg.get("facts", [])}
                row["similarity"] = [round(difflib.SequenceMatcher(
                    None, c["text"], " ".join(drafts.get(i, "") for i in c["fact_ids"])).ratio(), 3)
                    for c in dbg["composed"] if c["kind"] == "rewrite"]
            rows.append(row)
            texts = [a["text"] for a in resp.get("answer", [])]
            print(f"{case} {qid}: HTTP {status} route {row['route']} (insufficient {row['insufficient']}, search "
                  f"{'wanted' if row['search_wanted'] else 'not wanted'}, route_ok {row['route_ok']}) "
                  f"rewrite {dbg.get('rewrite')} {texts}", flush=True)
    scored = [r for r in rows if r["route_ok"] is not None]
    if scored:  # routing is scored here and nowhere else: the rewrite columns above say nothing about it
        print(f"routing: {sum(r['route_ok'] for r in scored)}/{len(scored)} as expected "
              f"({', '.join(r['case'] + ':' + r['question_id'] for r in scored if not r['route_ok']) or 'none off'})")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"followup_rewrite_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(f"written: {path}")
    return 0


def _totals(results: list[dict]) -> list[str]:
    ok = [r for r in results if r["status"] == 200]
    must_hit = sum(_rates(r)[0] for r in ok)
    must_n = sum(_rates(r)[1] for r in ok)
    other_hit = sum(_rates(r)[2] for r in ok)
    other_n = sum(_rates(r)[3] for r in ok)
    hard = sum(len(r["hard_violations"]) for r in ok)
    rw = [r.get("rewrite") or {} for r in ok]
    rewritten = sum(x.get("rewritten", 0) for x in rw)
    drafts = sum(x.get("fallback_drafts", 0) + x.get("must_added", 0) for x in rw)
    blocked = sum(x.get("blocked", 0) for x in rw)
    model = sum(x.get("model_sentences", 0) for x in rw)
    sims = [v for x in rw for v in x.get("similarity", [])]
    copies = sum(x.get("identical_to_draft", 0) for x in rw)
    pct = lambda a, b: f"{100 * a / b:.1f}%" if b else "n/a"  # noqa: E731
    return ["## Totals", "",
            f"- runs: {len(results)} ({len(ok)} HTTP 200)",
            f"- must mention: {must_hit}/{must_n} ({pct(must_hit, must_n)}); must not / next steps: "
            f"{other_hit}/{other_n} ({pct(other_hit, other_n)})",
            f"- hard violations: {hard}",
            f"- summary model sentences: {rewritten + drafts}; Gemini rewrites kept: {rewritten} "
            f"({pct(rewritten, rewritten + drafts)}); drafts: {drafts} ({pct(drafts, rewritten + drafts)})",
            f"- Gemini sentences returned: {model}; blocked: {blocked} ({pct(blocked, model)})",
            f"- rewrite similarity to drafts: mean {(sum(sims) / len(sims)) if sims else 0:.3f}, "
            f"min {min(sims) if sims else 0:.3f}, max {max(sims) if sims else 0:.3f}; identical to draft: {copies}"]


if __name__ == "__main__":
    sys.exit(main())
