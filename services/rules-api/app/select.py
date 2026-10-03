"""Evaluator for KbItem.applies_when (all keys AND-ed; unknown key never matches)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("rules-api.select")


@dataclass
class SelectContext:
    title_type: Optional[str] = None
    hazards: list[dict] = field(default_factory=list)  # {layer, priority, status}
    lvr_pct: Optional[float] = None
    dti: Optional[float] = None
    year_range: Optional[tuple[int, int]] = None  # (from, to); resolved property, else chat
    cladding: Optional[str] = None
    fhl_eligible: bool = False  # first_home_loan scenario was produced


def _check(key: str, arg: Any, ctx: SelectContext) -> Optional[bool]:
    """True/False for a known key, None for an unknown one."""
    if key == "always":
        return bool(arg)
    if key == "title_type":
        return ctx.title_type is not None and ctx.title_type in arg
    if key == "hazard_status":
        return any(
            h["priority"] == "primary" and h["status"] == arg for h in ctx.hazards
        )
    if key == "fhl_eligible":
        return ctx.fhl_eligible == bool(arg)
    if key == "lvr_over":
        return ctx.lvr_pct is not None and ctx.lvr_pct > arg
    if key == "dti_over":
        return ctx.dti is not None and ctx.dti > arg
    if key == "year_built_between":
        lo, hi = arg
        return ctx.year_range is not None and ctx.year_range[0] <= hi and lo <= ctx.year_range[1]
    if key == "cladding_in":
        return ctx.cladding is not None and ctx.cladding.lower() in {
            str(c).lower() for c in arg
        }
    return None


def matches(applies_when: dict[str, Any], ctx: SelectContext) -> bool:
    if not applies_when:
        return False
    for key, arg in applies_when.items():
        try:
            res = _check(key, arg, ctx)
        except (TypeError, ValueError):
            log.warning("malformed applies_when value for key %r: %r", key, arg)
            return False
        if res is None:
            log.warning("unknown applies_when key %r", key)
            return False
        if not res:
            return False
    return True
