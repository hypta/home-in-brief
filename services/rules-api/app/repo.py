"""Data sources: LocalRepo (seed files) and FirestoreRepo."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Protocol

from b_common.schemas import KbItem, RatesSnapshot, Source

log = logging.getLogger("rules-api.repo")

# services/rules-api/app/repo.py -> repo root (also /app inside the image)
ROOT = Path(os.environ.get("DATA_ROOT") or Path(__file__).resolve().parents[3])


class Repo(Protocol):
    def kb_items(self) -> list[KbItem]: ...
    def sources(self) -> list[Source]: ...
    def rates(self) -> Optional[RatesSnapshot]: ...


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


class LocalRepo:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def kb_items(self) -> list[KbItem]:
        seed = self.root / "kb" / "seed"
        out: list[KbItem] = []
        if not seed.is_dir():
            return out
        for p in sorted(seed.glob("*.json")):
            if p.name == "sources.json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for raw in data if isinstance(data, list) else [data]:
                    out.append(KbItem.model_validate(raw))
            except Exception as e:  # skip a bad file, keep serving the rest
                log.warning("skipping %s: %s", p.name, e)
        return out

    def sources(self) -> list[Source]:
        data = _read_json(self.root / "kb" / "seed" / "sources.json")
        if not data:
            return []
        if isinstance(data, dict):
            data = data.get("sources", list(data.values()))
        return [Source.model_validate(s) for s in data]

    def rates(self) -> Optional[RatesSnapshot]:
        data = _read_json(self.root / "data" / "rates_seed.json")
        return RatesSnapshot.model_validate(data) if data else None


class FirestoreRepo:
    def kb_items(self) -> list[KbItem]:
        from b_common import firestore as fs

        return [KbItem.model_validate(d) for d in fs.list_docs("kb_items")]

    def sources(self) -> list[Source]:
        from b_common import firestore as fs

        return [Source.model_validate(d) for d in fs.list_docs("sources")]

    def rates(self) -> Optional[RatesSnapshot]:
        from b_common import firestore as fs

        d = fs.get_doc("rates_latest", "current")
        return RatesSnapshot.model_validate(d) if d else None


def get_repo() -> Repo:
    if os.environ.get("DATA_BACKEND", "local").lower() == "firestore":
        return FirestoreRepo()
    return LocalRepo()
