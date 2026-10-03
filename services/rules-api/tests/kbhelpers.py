"""Test helpers (not named conftest: several suites run in one pytest session)."""


def hashed(item: dict) -> dict:
    """Mark a test KB item as reviewed against its current content."""
    from b_common.content_hash import content_hash

    return {**item, "verified_by": "tester", "verified_at": "2026-10-04",
            "verified_content_hash": content_hash(item)}
