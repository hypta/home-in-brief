"""Write kb/seed/*.json and sources.json into Firestore (kb_items/{id}, sources/{id}).

Usage: python scripts/seed_firestore.py [--dry-run]
Re-seeding overwrites seed fields but keeps human-set verification state of existing docs.
"""
import argparse
import json
from pathlib import Path

SEED = Path(__file__).resolve().parents[1] / "kb" / "seed"
PRESERVE = ("status", "verified_by", "verified_at", "last_check")


def load():
    sources = json.loads((SEED / "sources.json").read_text(encoding="utf-8"))
    items = []
    for p in sorted(SEED.glob("*.json")):
        if p.name != "sources.json":
            items.extend(json.loads(p.read_text(encoding="utf-8")))
    return items, sources


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, do not connect")
    args = ap.parse_args()
    items, sources = load()
    if args.dry_run:
        for s in sources:
            print(f"[dry-run] sources/{s['id']}  {s['url']}")
        for it in items:
            print(f"[dry-run] kb_items/{it['id']}  status={it['status']}  source={it['source_id']}")
        print(f"[dry-run] {len(items)} items, {len(sources)} sources; nothing written")
        return
    from google.cloud import firestore

    db = firestore.Client()
    for s in sources:
        db.collection("sources").document(s["id"]).set(s)
    for it in items:
        ref = db.collection("kb_items").document(it["id"])
        existing = ref.get()
        data = dict(it)
        if existing.exists:  # never reset human verification or job state
            old = existing.to_dict()
            for k in PRESERVE:
                if k in old:
                    data[k] = old[k]
        ref.set(data)
    print(f"wrote {len(items)} items, {len(sources)} sources")


if __name__ == "__main__":
    main()
