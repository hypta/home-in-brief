"""Validate kb/seed/*.json: schema, quote length, source references, duplicate ids.

Usage: python kb/validate.py            (exit code 1 on any problem)
       python kb/validate.py --hash <id>  (print the content_hash to paste into verified_content_hash)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from content_hash import content_hash, verification_problems  # noqa: E402

SEED = Path(__file__).resolve().parent / "seed"

ITEM_FIELDS = [
    "id", "topic", "applies_when", "claim_en", "quote", "source_id", "source_url",
    "source_title", "source_type", "licence", "next_step_en", "verified_by",
    "verified_at", "verified_content_hash", "status", "last_check",
]
STATUSES = {"draft", "verified", "needs_review", "retired"}
SOURCE_FIELDS = ["id", "title", "url", "licence", "accessed_at", "source_type",
                 "attribution", "tos_status", "tos_url", "tos_checked_at"]
TOS_STATUSES = {"allowed", "unclear", "prohibited"}
TOPICS = {"build_era", "cladding", "title_type", "insurance", "lending", "auction", "hazard", "valuation"}
SOURCE_TYPES = {"official", "secondary"}
WHEN_KEYS = {
    "always": lambda v: v is True,
    "title_type": lambda v: isinstance(v, list) and v and all(isinstance(x, str) for x in v),
    "hazard_status": lambda v: v == "intersects",
    "fhl_eligible": lambda v: v is True,
    "lvr_over": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "dti_over": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "year_built_between": lambda v: isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v),
    "cladding_in": lambda v: isinstance(v, list) and v and all(isinstance(x, str) for x in v),
}
MAX_QUOTE_WORDS = 15


def validate(seed_dir: Path = SEED) -> list[str]:
    errors: list[str] = []
    src_path = seed_dir / "sources.json"
    sources = json.loads(src_path.read_text(encoding="utf-8"))
    source_ids: set[str] = set()
    for s in sources:
        if set(s) != set(SOURCE_FIELDS):
            errors.append(f"source {s.get('id')}: fields {sorted(s)} != {sorted(SOURCE_FIELDS)}")
        if s["id"] in source_ids:
            errors.append(f"duplicate source id {s['id']}")
        source_ids.add(s["id"])
        if s.get("source_type") not in SOURCE_TYPES:
            errors.append(f"source {s['id']}: bad source_type")
        for k in ("licence", "attribution", "tos_status", "tos_url", "tos_checked_at"):
            if not isinstance(s.get(k), str) or not s[k].strip():
                errors.append(f"source {s['id']}: {k} is missing or empty")
        if s.get("tos_status") not in TOS_STATUSES:
            errors.append(f"source {s['id']}: tos_status must be one of {sorted(TOS_STATUSES)}")

    seen: set[str] = set()
    count = 0
    for path in sorted(seed_dir.glob("*.json")):
        if path.name == "sources.json":
            continue
        for it in json.loads(path.read_text(encoding="utf-8")):
            count += 1
            iid = it.get("id")
            where = f"{path.name}:{iid}"
            if set(it) != set(ITEM_FIELDS):
                errors.append(f"{where}: field mismatch {sorted(set(it) ^ set(ITEM_FIELDS))}")
            if iid in seen:
                errors.append(f"{where}: duplicate id")
            seen.add(iid)
            if it.get("topic") not in TOPICS:
                errors.append(f"{where}: bad topic {it.get('topic')}")
            words = len(str(it.get("quote", "")).split())
            if not 1 <= words <= MAX_QUOTE_WORDS:
                errors.append(f"{where}: quote has {words} words")
            if it.get("source_id") not in source_ids:
                errors.append(f"{where}: unknown source_id {it.get('source_id')}")
            else:
                src = next(s for s in sources if s["id"] == it["source_id"])
                if src["url"] != it.get("source_url"):
                    errors.append(f"{where}: source_url differs from sources.json")
                if src["licence"] != it.get("licence"):
                    errors.append(f"{where}: licence differs from sources.json")
            if it.get("source_type") not in SOURCE_TYPES:
                errors.append(f"{where}: bad source_type")
            if it.get("status") not in STATUSES:
                errors.append(f"{where}: bad status {it.get('status')!r}")
            if it.get("status") == "draft" and any(
                    it.get(k) is not None for k in ("verified_by", "verified_at", "verified_content_hash")):
                errors.append(f"{where}: draft must have null verified_by/verified_at/verified_content_hash")
            for problem in verification_problems(it):
                errors.append(f"{where}: verified but {problem}")
            if it.get("last_check") is not None:
                errors.append(f"{where}: seed last_check must be null (written by kb-refresh only)")
            when = it.get("applies_when")
            if not isinstance(when, dict) or not when:
                errors.append(f"{where}: applies_when must be a non-empty object")
            else:
                for k, v in when.items():
                    if k not in WHEN_KEYS or not WHEN_KEYS[k](v):
                        errors.append(f"{where}: bad applies_when {k}={v!r}")
    print(f"items={count} sources={len(sources)} errors={len(errors)}")
    for e in errors:
        print("ERROR", e)
    return errors


def all_items(seed_dir: Path = SEED) -> list[dict]:
    items: list[dict] = []
    for path in sorted(seed_dir.glob("*.json")):
        if path.name != "sources.json":
            items.extend(json.loads(path.read_text(encoding="utf-8")))
    return items


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--hash":
        found = [i for i in all_items() if i["id"] == sys.argv[2]]
        if not found:
            sys.exit(f"no item with id {sys.argv[2]}")
        print(content_hash(found[0]))
        sys.exit(0)
    sys.exit(1 if validate() else 0)
