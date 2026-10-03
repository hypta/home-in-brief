import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[3] / "shared")]


def hashed(item: dict) -> dict:
    """Mark a test KB item as reviewed against its current content."""
    from b_common.content_hash import content_hash

    return {**item, "verified_content_hash": content_hash(item)}
