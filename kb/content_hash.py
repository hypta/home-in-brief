"""Content hash and verification rules shared by kb/validate.py, scripts/seed_firestore.py and kb-refresh.

An item may only be `verified` if a named person reviewed exactly this content: verified_by,
verified_at and verified_content_hash are set and verified_content_hash == content_hash(item).
Any edit to a hashed field therefore silently turns a verified item back into needs_review.
"""
import hashlib
import json
from datetime import datetime, timezone
import re
import unicodedata

HASHED_FIELDS = ("claim_en", "quote", "source_url", "applies_when", "next_step_en")


def _norm(value):
    if isinstance(value, str):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def content_hash(item: dict) -> str:
    payload = {f: _norm(item.get(f)) for f in HASHED_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verification_problems(item: dict) -> list[str]:
    """Why a `verified` item is not really verified; empty for any other status or a sound one."""
    if item.get("status") != "verified":
        return []
    problems = [f"{k} is missing" for k in ("verified_by", "verified_at", "verified_content_hash")
                if not item.get(k)]
    if item.get("verified_content_hash") and item["verified_content_hash"] != content_hash(item):
        problems.append("verified_content_hash does not match the current content (content changed after review)")
    return problems


def effective_status(item: dict) -> str:
    """The status to trust: a verified item with unsound verification counts as needs_review."""
    status = item.get("status", "draft")
    return "needs_review" if status == "verified" and verification_problems(item) else status


def parse_ts(value) -> datetime | None:
    """ISO 8601 date or datetime -> aware UTC datetime. A date-only value means the START of that day
    (UTC), so a same-day date-only verified_at never counts as later than a demotion on that day.
    Returns None if absent or unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
