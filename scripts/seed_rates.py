"""Write data/rates_seed.json into Firestore as rates_latest/current (+ rates_snapshots/{date}).

Usage: python scripts/seed_rates.py [--dry-run]
Uses FIRESTORE_EMULATOR_HOST / GOOGLE_CLOUD_PROJECT from the environment.
"""
import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

SEED = Path(__file__).resolve().parents[1] / "data" / "rates_seed.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, do not connect")
    args = ap.parse_args()
    snap = json.loads(SEED.read_text(encoding="utf-8"))
    date = snap["captured_at"][:10]
    # one immutable snapshot doc per load (date + UTC run id), never overwrite an earlier snapshot of the same day
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)
    snap_id = f"{date}_{run_id}"
    if args.dry_run:
        print(f"[dry-run] rates_latest/current + rates_snapshots/{snap_id}: {len(snap['banks'])} banks; nothing written")
        return
    from google.cloud import firestore

    db = firestore.Client()
    db.collection("rates_snapshots").document(snap_id).set(snap)
    db.collection("rates_latest").document("current").set(snap)
    print(f"wrote rates_latest/current ({len(snap['banks'])} banks)")


if __name__ == "__main__":
    main()
