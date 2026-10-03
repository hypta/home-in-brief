"""Backends: local (kb/seed/*.json, report file) and firestore."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parents[2])
SEED_DIR = Path(os.environ.get("KB_SEED_DIR") or ROOT / "kb" / "seed")
OUT_DIR = Path(os.environ.get("OUT_DIR") or ROOT / "out")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "kb"))
from content_hash import content_hash, parse_ts  # noqa: E402


def load_seed_items(seed_dir: Path = SEED_DIR) -> list[dict]:
    items: list[dict] = []
    for p in sorted(seed_dir.glob("*.json")):
        if p.name != "sources.json":
            items.extend(json.loads(p.read_text(encoding="utf-8")))
    return items


def load_seed_sources(seed_dir: Path = SEED_DIR) -> dict[str, dict]:
    return {s["id"]: s for s in json.loads((seed_dir / "sources.json").read_text(encoding="utf-8"))}


class LocalStore:
    def __init__(self, seed_dir: Path = SEED_DIR, out_dir: Path = OUT_DIR):
        self.seed_dir, self.out_dir = seed_dir, out_dir
        self.report: dict = {"runs": [], "job": None}

    def load_items(self) -> list[dict]:
        return load_seed_items(self.seed_dir)

    def load_sources(self) -> dict[str, dict]:
        return load_seed_sources(self.seed_dir)

    def record(self, item: dict, run: dict, new_status: str) -> None:
        self.report["runs"].append({"id": item["id"], "source_url": item["source_url"],
                                    "status_before": item.get("status"), "status_after": new_status, **run})

    def finish(self, job_run: dict) -> None:
        self.report["job"] = job_run
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "kb_refresh_report.json").write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _review_is_newer(current: dict, checked_at: str | None) -> bool:
    reviewed, checked = parse_ts(current.get("verified_at")), parse_ts(checked_at)
    return bool(reviewed and checked and reviewed >= checked)


class FirestoreStore:
    def __init__(self, client=None, transactional=None):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client()
            transactional = firestore.transactional
        self.db = client
        self._transactional = transactional or (lambda fn: fn)  # tests inject a client without transactions

    def load_items(self) -> list[dict]:
        return [d.to_dict() | {"id": d.id} for d in self.db.collection("kb_items").stream()]

    def load_sources(self) -> dict[str, dict]:
        return {d.id: d.to_dict() for d in self.db.collection("sources").stream()}

    def record(self, item: dict, run: dict, new_status: str) -> None:
        """Write the run, and update the item only if its content is still the version that was checked.

        If a person edited the item while the job was running, the run is kept for audit with
        stale_content=True and the item is left alone. If the same content was re-verified at or after
        the time of this check, the check cannot demote it (superseded_by_review=True). The status is
        changed only if it is still what the job read; a demotion records demoted_at and
        demoted_content_hash.
        """
        run_id = run.get("run_id") or run["at"]
        item_ref = self.db.collection("kb_items").document(item["id"])
        run_ref = self.db.collection("kb_source_checks").document(item["id"]).collection("runs").document(run_id)

        @self._transactional
        def apply(tx):
            snap = item_ref.get(transaction=tx)
            current = snap.to_dict() if snap.exists else None
            stale = current is None or content_hash(current) != run.get("content_hash")
            # A review of the same content made after this check started is newer evidence than the check.
            superseded = (not stale and new_status != item.get("status")
                          and _review_is_newer(current, run.get("at")))
            rec = dict(run, stale_content=stale, superseded_by_review=superseded)
            tx.set(run_ref, rec)
            if stale:
                return
            update = {"last_check": rec}
            if (new_status != item.get("status") and current.get("status") == item.get("status")
                    and not superseded):
                update["status"] = new_status
                if new_status == "needs_review":
                    # lets kb/seed re-seeding tell a withdrawal from a review
                    update["demoted_at"] = run.get("at")
                    update["demoted_content_hash"] = run.get("content_hash")
            tx.update(item_ref, update)

        apply(self.db.transaction())

    def finish(self, job_run: dict) -> None:
        self.db.collection("job_runs").document(job_run.get("run_id") or job_run["at"]).set(job_run)
