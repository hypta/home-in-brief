"""Persistence backends: Firestore (production) and local files (dev)."""
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


class LocalStore:
    def __init__(self, seed_path: Path | None = None, out_dir: Path | None = None):
        self.seed_path = Path(os.environ.get("RATES_SEED_PATH", seed_path or REPO_ROOT / "data" / "rates_seed.json"))
        self.out_dir = Path(os.environ.get("OUT_DIR", out_dir or Path(__file__).resolve().parents[1] / "out"))

    def load_previous(self) -> dict | None:
        try:
            return json.loads(self.seed_path.read_text())
        except (OSError, ValueError):
            return None

    def write_snapshot(self, date: str, snapshot: dict):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "rates_snapshot.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))

    def write_job_run(self, run_id: str, run: dict):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "job_run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False))


class FirestoreStore:
    def __init__(self, client=None):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client()
        self.db = client

    def load_previous(self) -> dict | None:
        doc = self.db.collection("rates_latest").document("current").get()
        return doc.to_dict() if doc.exists else None

    def write_snapshot(self, date: str, snapshot: dict):
        self.db.collection("rates_snapshots").document(date).set(snapshot)
        self.db.collection("rates_latest").document("current").set(snapshot)

    def write_job_run(self, run_id: str, run: dict):
        self.db.collection("job_runs").document(run_id).set(run)


def make_store():
    backend = os.environ.get("DATA_BACKEND", "local")
    if backend == "firestore":
        return FirestoreStore()
    if backend == "local":
        return LocalStore()
    raise SystemExit(f"unknown DATA_BACKEND={backend!r}")
