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
"""
from __future__ import annotations

import argparse
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

from app import assemble, kb, validator as V  # noqa: E402
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
        ("S", r"debts?.*(none|zero|no )|no (other |existing )?debts", None), ("S", r"unit title", None),
        ("not", r"\b1998\b"), ("not", r"\bdiffer"), ("not", r"\b65\b"), ("not", r"\b2\.27\b"),
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
        ("not", r"\b1999\b"), ("not", r"\b80\b"),
        ("N", "kb_monolithic_cladding"),
    ],
}
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


def rubric(case: str, rep: dict, kb_items: dict) -> list[dict]:
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
    for s in model_sentences:
        fails = V.check_sentence(Sentence(**s), ev, V.MAX_WORDS if rep.get("model") != "mock" else None)
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
        for i in st["source_ids"]:
            if i not in selected or kb_items.get(i, {}).get("next_step_en") != st["text"]:
                bad.append(f"next step not verbatim from a selected item: {i}")
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
    ap.add_argument("--cases", nargs="*", default=sorted(CHECKS))
    ap.add_argument("--out", default=str(ROOT / "out" / "golden" / "eval"))
    args = ap.parse_args()

    out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for case in args.cases:
        d = json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))
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
                row["rubric"] = rubric(case, rep, data.items)
            else:
                row["error"] = rep.get("error", {}).get("code")
            if dbg:
                row["drop_reasons"] = [f for a in dbg["attempts"] for s in a["sentences"] for f in s["fails"]]
                row["attempts"] = len(dbg["attempts"])
            results.append(row)
            print(_line(row), flush=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    (out_dir / "summary.md").write_text(_markdown(results, args))
    print(f"\nwritten: {out_dir}/results.json, {out_dir}/summary.md")
    return 0


def _rates(row: dict) -> tuple[int, int, int, int]:
    r = row.get("rubric", [])
    must = [x for x in r if x["kind"] == "S"]
    other = [x for x in r if x["kind"] != "S"]
    return sum(x["ok"] for x in must), len(must), sum(x["ok"] for x in other), len(other)


def _line(row: dict) -> str:
    if row["status"] != 200:
        return f"{row['case']:6} run {row['run']}: HTTP {row['status']} {row.get('error')} ({row['seconds']}s)"
    mh, mn, oh, on = _rates(row)
    return (f"{row['case']:6} run {row['run']}: passed {row['passed']} dropped {row['dropped']} "
            f"hard {len(row['hard_violations'])} must {mh}/{mn} other {oh}/{on} "
            f"reasons {sorted(set(row.get('drop_reasons', [])))} ({row['seconds']}s)")


def _markdown(results: list[dict], args) -> str:
    lines = [f"# report-api eval {datetime.now().isoformat(timespec='seconds')}", "",
             f"Mode: {'http ' + args.url if args.url else 'direct'}; runs per case: {args.runs}; "
             f"REPORT_MOCK={os.environ.get('REPORT_MOCK', '')}; GEMINI_MODEL={os.environ.get('GEMINI_MODEL', '(default)')}",
             "", "| case | run | status | passed | dropped | hard | must mention | must not / next | drop reasons |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in results:
        if row["status"] != 200:
            lines.append(f"| {row['case']} | {row['run']} | {row['status']} {row.get('error')} | | | | | | "
                         f"{', '.join(sorted(set(row.get('drop_reasons', []))))} |")
            continue
        mh, mn, oh, on = _rates(row)
        lines.append(f"| {row['case']} | {row['run']} | 200 | {row['passed']} | {row['dropped']} | "
                     f"{len(row['hard_violations'])} | {mh}/{mn} | {oh}/{on} | "
                     f"{', '.join(sorted(set(row.get('drop_reasons', []))))} |")
    lines += ["", "## Failed checks and summaries", ""]
    for row in results:
        lines.append(f"### {row['case']} run {row['run']}")
        for x in row.get("rubric", []):
            if not x["ok"]:
                lines.append(f"- FAIL {x['check']}")
        for v in row.get("hard_violations", []):
            lines.append(f"- HARD {v}")
        for s in (row.get("report") or {}).get("summary", []):
            lines.append(f"  - S: {s['text']} `{', '.join(s['source_ids'])}`")
        for a in (row.get("debug") or {}).get("attempts", []):
            for s in a["sentences"]:
                if s["fails"]:
                    lines.append(f"  - DROPPED {s['fails']}: {s['text']} `{', '.join(s['source_ids'])}`")
            if a.get("error"):
                lines.append(f"  - ATTEMPT ERROR: {a['error']}")
        lines.append("")
    lines += ["## Manual checks (not automated)", ""] + [f"- {m}" for m in MANUAL] + [""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
