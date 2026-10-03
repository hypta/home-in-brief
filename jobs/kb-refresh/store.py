"""Backends: local (kb/seed/*.json, report file) and firestore."""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parents[2])
SEED_DIR = Path(os.environ.get("KB_SEED_DIR") or ROOT / "kb" / "seed")
OUT_DIR = Path(os.environ.get("OUT_DIR") or ROOT / "out")


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


class FirestoreStore:
    def __init__(self, client=None):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client()
        self.db = client

    def load_items(self) -> list[dict]:
        return [d.to_dict() | {"id": d.id} for d in self.db.collection("kb_items").stream()]

    def load_sources(self) -> dict[str, dict]:
        return {d.id: d.to_dict() for d in self.db.collection("sources").stream()}

    def record(self, item: dict, run: dict, new_status: str) -> None:
        ts = run["at"]
        self.db.collection("kb_source_checks").document(item["id"]).collection("runs").document(ts).set(run)
        update = {"last_check": run}
        if new_status != item.get("status"):
            update["status"] = new_status
        self.db.collection("kb_items").document(item["id"]).update(update)

    def finish(self, job_run: dict) -> None:
        self.db.collection("job_runs").document(job_run["at"]).set(job_run)
