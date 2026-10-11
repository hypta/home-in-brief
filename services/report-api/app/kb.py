"""Knowledge-base items and the rules-api source registry, read from rules-api or from kb/seed.

KB_SOURCE=rules-api (default): GET {RULES_API_URL}/v1/kb/items?ids=... and /v1/sources, with an ID
token for https URLs (as web does). KB_SOURCE=local: read kb/seed/*.json directly, for tests and
local runs without network.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import KeysView
from typing import Optional

import requests

log = logging.getLogger("report-api.kb")

REPO_ROOT = Path(__file__).resolve().parents[3]
METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
CACHE_TTL_S = 600
_tokens: dict[str, tuple[float, str]] = {}
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


class KbUnavailable(Exception):
    pass


class SourceTitles(dict):
    """rules-api source id -> title; `.keys()` also carries `.urls` (id -> url) so build_evidence can read them."""

    urls: dict[str, str]

    def __init__(self, titles: Optional[dict[str, str]] = None, urls: Optional[dict[str, str]] = None):
        super().__init__(titles or {})
        self.urls = dict(urls or {})

    def keys(self):  # type: ignore[override]
        ks = _Keys(self)
        ks.urls = self.urls
        return ks


class _Keys(KeysView):
    urls: dict[str, str] = {}


@dataclass
class KbData:
    items: dict[str, dict] = field(default_factory=dict)  # id -> KB item (claim_en, next_step_en, source_id, topic)
    sources: dict[str, str] = field(default_factory=SourceTitles)  # rules-api source id -> title (.urls: id -> url)


def _kb_dir() -> Path:
    return Path(os.environ.get("KB_DIR") or REPO_ROOT / "kb" / "seed")


def _load_local(ids: list[str]) -> KbData:
    d = _kb_dir()
    if not d.is_dir():
        raise KbUnavailable(f"kb directory not found: {d}")
    items: dict[str, dict] = {}
    sources = SourceTitles()
    for p in sorted(d.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("sources", []) if p.name == "sources.json" else [data]
        for row in rows:
            if not isinstance(row, dict) or "id" not in row:
                continue
            if p.name == "sources.json":
                sources[row["id"]] = row.get("title") or row["id"]
                if row.get("url"):
                    sources.urls[row["id"]] = row["url"]
            else:
                items[row["id"]] = row
    wanted = set(ids)
    return KbData(items={k: v for k, v in items.items() if k in wanted}, sources=sources)


def _auth(base: str, timeout: float) -> dict:
    """ID token for a private Cloud Run service; plain-http local services need none."""
    if not base.startswith("https://"):
        return {}
    expires, token = _tokens.get(base, (0.0, ""))
    if time.time() >= expires:
        r = requests.get(METADATA, params={"audience": base}, headers={"Metadata-Flavor": "Google"},
                         timeout=min(5.0, timeout))
        r.raise_for_status()
        token = r.text
        _tokens[base] = (time.time() + 50 * 60, token)
    return {"Authorization": f"Bearer {token}"}


def _get_json(base: str, path: str, params: Optional[dict], timeout: float):
    t0 = time.monotonic()
    headers = _auth(base, timeout)
    timeout = max(0.1, timeout - (time.monotonic() - t0))  # the ID token fetch counts against the same limit
    r = requests.get(base + path, params=params, headers=headers, timeout=timeout)
    if r.status_code in (401, 403):
        _tokens.pop(base, None)
    r.raise_for_status()
    return r.json()


def _cached(key: str, fetch):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = fetch()
    with _lock:
        _cache[key] = (now + CACHE_TTL_S, value)
    return value


def _load_remote(ids: list[str], timeout: float) -> KbData:
    base = os.environ.get("RULES_API_URL", "http://localhost:8080").rstrip("/")
    end = time.monotonic() + max(0.5, min(5.0, timeout))  # every request below shares this limit (Codex R1)

    def left() -> float:
        t = end - time.monotonic()
        if t <= 0:
            raise TimeoutError("kb load out of time")
        return t

    def _fetch_sources() -> SourceTitles:
        rows = [s for s in _get_json(base, "/v1/sources", None, left()) if isinstance(s, dict) and "id" in s]
        return SourceTitles({s["id"]: s.get("title") or s["id"] for s in rows},
                            {s["id"]: s["url"] for s in rows if s.get("url")})

    sources = _cached(f"sources:{base}", _fetch_sources)
    items: dict[str, dict] = {}
    missing = []
    for i in ids:
        hit = _cache.get(f"kb:{base}:{i}")
        if hit and hit[0] > time.time():
            items[i] = hit[1]  # type: ignore[assignment]
        else:
            missing.append(i)
    if missing:
        rows = _get_json(base, "/v1/kb/items", {"ids": ",".join(missing)}, left())
        now = time.time()
        for row in rows:
            if isinstance(row, dict) and "id" in row:
                items[row["id"]] = row
                with _lock:
                    _cache[f"kb:{base}:{row['id']}"] = (now + CACHE_TTL_S, row)
    return KbData(items=items, sources=sources)


def load(ids: list[str], timeout: float = 5.0) -> KbData:
    """KB items for `ids` and the rules-api source registry. Raises KbUnavailable."""
    mode = os.environ.get("KB_SOURCE", "rules-api").lower()
    try:
        if mode == "local":
            return _load_local(ids)
        return _load_remote(ids, timeout)
    except KbUnavailable:
        raise
    except Exception as e:  # network, HTTP or JSON errors
        log.warning("kb load failed (%s): %s", mode, type(e).__name__)
        raise KbUnavailable(str(e)) from e


def clear_cache() -> None:
    with _lock:
        _cache.clear()
