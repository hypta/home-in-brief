"""Write kb/seed/*.json and sources.json into Firestore (kb_items/{id}, sources/{id}).

Usage: python scripts/seed_firestore.py [--dry-run]

The git seed is the source of truth for content. What survives from Firestore:
  * last_check (written by kb-refresh);
  * a human verification in Firestore, only while it still matches the seed's exact content hash;
  * needs_review set by kb-refresh while the content is unchanged, unless the seed has a verified_at
    later than the demotion; retired stays retired unless the seed sets "reinstate": true.
Verified seed items are written as verified only if reviewer data and verified_content_hash match
the content (kb/validate.py enforces this and runs first). Ids that left the seed are marked retired.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "kb" / "seed"
sys.path.insert(0, str(ROOT / "kb"))
from content_hash import content_hash, parse_ts, verification_problems  # noqa: E402

KEEP_VERIFICATION = ("verified_by", "verified_at", "verified_content_hash")


def load():
    sources = json.loads((SEED / "sources.json").read_text(encoding="utf-8"))
    items = []
    for p in sorted(SEED.glob("*.json")):
        if p.name != "sources.json":
            items.extend(json.loads(p.read_text(encoding="utf-8")))
    return items, sources


def _later(a: str | None, b: str | None) -> bool:
    """True if timestamp a is strictly later than b (both must parse)."""
    ta, tb = parse_ts(a), parse_ts(b)
    return bool(ta and tb and ta > tb)


def merge_doc(seed: dict, existing: dict | None, now: str = "") -> dict:
    """The document to write for one seed item, given the current Firestore document (or None).

    Withdrawals are never undone by re-seeding:
      * needs_review set by kb-refresh (demoted_at/demoted_content_hash) stays while the content is the
        same, even if the seed says verified; only a seed verified_at later than demoted_at clears it.
      * retired stays retired unless the seed sets "reinstate": true or has a sound verified_at later
        than retired_at. A seed with status retired always retires.
    """
    seed = dict(seed)
    reinstate = bool(seed.pop("reinstate", False))
    doc = dict(seed)
    seed_hash = content_hash(seed)
    ex = existing or {}
    for k in ("last_check", "demoted_at", "demoted_content_hash", "retired_at"):
        if ex.get(k) is not None:
            doc[k] = ex[k]
    seed_sound_verified = seed["status"] == "verified" and not verification_problems(seed)

    if seed["status"] == "retired":
        doc["status"] = "retired"
        doc["retired_at"] = ex.get("retired_at") or now or None
        return doc
    if ex.get("status") == "retired":
        if reinstate or (seed_sound_verified and _later(seed.get("verified_at"), ex.get("retired_at"))):
            doc.pop("retired_at", None)
        else:
            doc["status"] = "retired"
            return doc

    if seed["status"] == "verified":
        if not seed_sound_verified:
            doc["status"] = "needs_review"
        elif ex.get("status") == "needs_review" and content_hash(ex) == seed_hash:
            # same content was withdrawn: only a review made after the withdrawal clears it
            withdrawn_at = ex.get("demoted_at") or (ex.get("last_check") or {}).get("at")
            if withdrawn_at and not _later(seed.get("verified_at"), withdrawn_at):
                doc["status"] = "needs_review"
        return doc

    if ex.get("status") == "verified":
        sound_and_same = not verification_problems(ex) and ex.get("verified_content_hash") == seed_hash
        for k in KEEP_VERIFICATION:  # keep the evidence either way
            doc[k] = ex.get(k)
        doc["status"] = "verified" if sound_and_same else "needs_review"
    elif ex.get("status") == "needs_review" and content_hash(ex) == seed_hash:
        doc["status"] = "needs_review"  # a person must re-verify
    return doc


def plan(items: list[dict], existing_docs: dict[str, dict], now: str = "") -> tuple[dict[str, dict], list[str]]:
    """(id -> doc to write, ids to mark retired)."""
    writes = {it["id"]: merge_doc(it, existing_docs.get(it["id"]), now) for it in items}
    retired = sorted(i for i, d in existing_docs.items() if i not in writes and d.get("status") != "retired")
    return writes, retired


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, do not connect")
    args = ap.parse_args()
    from validate import validate  # kb/validate.py

    if validate():
        sys.exit("seed is invalid; nothing written")
    items, sources = load()
    if args.dry_run:
        for s in sources:
            print(f"[dry-run] sources/{s['id']}  {s['url']}")
        for it in items:
            print(f"[dry-run] kb_items/{it['id']}  seed_status={it['status']}  source={it['source_id']}")
        print(f"[dry-run] {len(items)} items, {len(sources)} sources; nothing written "
              "(retirement of removed ids and merging with Firestore state need a connection)")
        return
    from google.cloud import firestore

    db = firestore.Client()
    for s in sources:
        db.collection("sources").document(s["id"]).set(s)
    existing = {d.id: d.to_dict() for d in db.collection("kb_items").stream()}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    writes, retired = plan(items, existing, now)
    for iid, doc in writes.items():
        db.collection("kb_items").document(iid).set(doc)
    for iid in retired:
        db.collection("kb_items").document(iid).update({"status": "retired", "retired_at": now})
    changed = [i for i, d in writes.items() if existing.get(i, {}).get("status") not in (None, d["status"])]
    print(f"wrote {len(writes)} items, {len(sources)} sources; retired {len(retired)}; "
          f"status changed on {len(changed)}: {changed}")


if __name__ == "__main__":
    main()
