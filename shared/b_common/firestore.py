"""Small Firestore helpers (lazy import so local dev needs no GCP deps)."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional


@lru_cache(maxsize=1)
def get_client():
    from google.cloud import firestore

    return firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)


def list_docs(collection: str) -> list[dict[str, Any]]:
    return [d.to_dict() for d in get_client().collection(collection).stream()]


def get_doc(collection: str, doc_id: str) -> Optional[dict[str, Any]]:
    snap = get_client().collection(collection).document(doc_id).get()
    return snap.to_dict() if snap.exists else None
