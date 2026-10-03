"""Write kb/seed/*.json and sources.json into Firestore (kb_items/{id}, sources/{id}).

Usage: python scripts/seed_firestore.py [--dry-run]

The git seed is the source of truth for content. What survives from Firestore:
  * last_check (written by kb-refresh);
  * a human verification in Firestore, only while it still matches the seed's exact content hash;
  * needs_review set by kb-refresh, while the content is unchanged and the seed does not say verified.
Verified seed items are written as verified only if reviewer data and verified_content_hash match
the content (kb/validate.py enforces this and runs first). Ids that left the seed are marked retired.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "kb" / "seed"
sys.path.insert(0, str(ROOT / "kb"))
from content_hash import content_hash, verification_problems  # noqa: E402

KEEP_VERIFICATION = ("verified_by", "verified_at", "verified_content_hash")


def load():
    sources = json.loads((SEED / "sources.json").read_text(encoding="utf-8"))
    items = []
    for p in sorted(SEED.glob("*.json")):
        if p.name != "sources.json":
            items.extend(json.loads(p.read_text(encoding="utf-8")))
    return items, sources


def merge_doc(seed: dict, existing: dict | None) -> dict:
    """The document to write for one seed item, given the current Firestore document (or None)."""
    doc = dict(seed)
    if existing is None:
        doc["status"] = "needs_review" if seed["status"] == "verified" and verification_problems(seed) else seed["status"]
        return doc
    if existing.get("last_check") is not None:
        doc["last_check"] = existing["last_check"]
    seed_hash = content_hash(seed)
    if seed["status"] == "verified":
        if verification_problems(seed):
            doc["status"] = "needs_review"
        return doc
    ex_status = existing.get("status")
    if ex_status == "verified":
        sound_and_same = not verification_problems(existing) and existing.get("verified_content_hash") == seed_hash
        for k in KEEP_VERIFICATION:  # keep the evidence either way
            doc[k] = existing.get(k)
        doc["status"] = "verified" if sound_and_same else "needs_review"
    elif ex_status == "needs_review" and content_hash(existing) == seed_hash:
        doc["status"] = "needs_review"  # kb-refresh found the quote gone; a person must re-verify
    return doc


def plan(items: list[dict], existing_docs: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """(id -> doc to write, ids to mark retired)."""
    writes = {it["id"]: merge_doc(it, existing_docs.get(it["id"])) for it in items}
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
    writes, retired = plan(items, existing)
    for iid, doc in writes.items():
        db.collection("kb_items").document(iid).set(doc)
    for iid in retired:
        db.collection("kb_items").document(iid).update({"status": "retired"})
    changed = [i for i, d in writes.items() if existing.get(i, {}).get("status") not in (None, d["status"])]
    print(f"wrote {len(writes)} items, {len(sources)} sources; retired {len(retired)}; "
          f"status changed on {len(changed)}: {changed}")


if __name__ == "__main__":
    main()
