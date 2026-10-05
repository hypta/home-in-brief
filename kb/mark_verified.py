"""Mark knowledge-base items as verified after a human review (see kb/MANUAL_CHECK.md).

Usage:
    python kb/mark_verified.py --by "Reviewer Name" kb_auction_unconditional kb_nhc_building_cap
    python kb/mark_verified.py --by "Reviewer Name" --dry-run kb_auction_unconditional

For each id it sets status="verified", verified_by, verified_at (current UTC time, full ISO timestamp)
and verified_content_hash (hash of the content as it is now), then runs kb/validate.py.
The author may verify their own item, but must open the source and check the quote word for word.
Only the listed ids are touched; the rest of each seed file is rewritten unchanged.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from content_hash import content_hash  # noqa: E402

SEED = HERE / "seed"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--by", required=True, help="name of the person who checked the source")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("ids", nargs="+", help="knowledge-base item ids")
    args = ap.parse_args(argv)
    if not args.by.strip():
        ap.error("--by must not be empty")

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    wanted = set(args.ids)
    found = set()
    for path in sorted(SEED.glob("*.json")):
        if path.name == "sources.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        touched = False
        for item in items:
            if item.get("id") not in wanted:
                continue
            found.add(item["id"])
            if item.get("status") == "retired":
                print(f"skip {item['id']}: retired")
                continue
            h = content_hash(item)
            print(f"{item['id']}: {item.get('status')} -> verified by {args.by} at {now} (hash {h[:12]}...)")
            item.update(status="verified", verified_by=args.by.strip(), verified_at=now, verified_content_hash=h)
            touched = True
        if touched and not args.dry_run:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    missing = wanted - found
    if missing:
        print("unknown ids: " + ", ".join(sorted(missing)), file=sys.stderr)
    if args.dry_run:
        print("dry run: nothing written")
        return 1 if missing else 0

    import validate  # noqa: E402  (kb/validate.py)
    problems = validate.validate()  # prints each problem as "ERROR ..."
    print(f"validate: errors={len(problems)}")
    return 1 if (missing or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
