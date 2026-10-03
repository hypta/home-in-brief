"""Validate the hand-maintained rates snapshot.

Usage: python scripts/validate_rates_seed.py [path]   (default: data/rates_seed.json)
Exits 1 and lists every problem if the file is not usable. Extra fields are allowed.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "data" / "rates_seed.json"
TERMS = {"6m", "1y", "18m", "2y", "3y", "4y", "5y"}
MIN_RATE, MAX_RATE = 2.0, 12.0


def _ts_ok(v) -> bool:
    try:
        datetime.fromisoformat(v)
        return True
    except (TypeError, ValueError):
        return False


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rate_ok(v) -> bool:
    return _is_num(v) and MIN_RATE <= v <= MAX_RATE


def validate(snap: dict) -> list[str]:
    errs: list[str] = []
    if snap.get("schema_version") != "0.1":
        errs.append(f"schema_version must be '0.1', got {snap.get('schema_version')!r}")
    if not _ts_ok(snap.get("captured_at")):
        errs.append("top-level captured_at missing or not ISO 8601")
    banks = snap.get("banks")
    if not isinstance(banks, list) or not banks:
        return errs + ["banks must be a non-empty list"]
    seen = set()
    for i, b in enumerate(banks):
        bid = b.get("bank_id")
        tag = f"banks[{i}] ({bid})"
        if not bid or bid in seen:
            errs.append(f"{tag}: bank_id missing or duplicated")
        seen.add(bid)
        if not b.get("name"):
            errs.append(f"{tag}: name missing")
        if not str(b.get("source_url") or "").startswith("https://"):
            errs.append(f"{tag}: source_url missing or not https")
        if not _ts_ok(b.get("captured_at")):
            errs.append(f"{tag}: captured_at missing or not ISO 8601")
        special = b.get("special")
        if not isinstance(special, dict) or "1y" not in special:
            errs.append(f"{tag}: special must be an object with a 1y rate")
            special = special if isinstance(special, dict) else {}
        for group, rates in (("special", special), ("standard", b.get("standard"))):
            if rates is None and group == "standard":
                continue
            if not isinstance(rates, dict):
                errs.append(f"{tag}: {group} must be an object or null")
                continue
            for term, v in rates.items():
                if term not in TERMS:
                    errs.append(f"{tag}: {group}.{term} is not one of {sorted(TERMS)}")
                elif not _rate_ok(v):
                    errs.append(f"{tag}: {group}.{term}={v!r} outside {MIN_RATE}-{MAX_RATE}")
        fl = b.get("floating")
        if fl is not None and not _rate_ok(fl):
            errs.append(f"{tag}: floating={fl!r} outside {MIN_RATE}-{MAX_RATE}")
        le = b.get("low_equity") or {}
        if le.get("mechanism") not in ("lem", "standard_rate"):
            errs.append(f"{tag}: low_equity.mechanism must be 'lem' or 'standard_rate'")
        if le.get("lem_base") not in (None, "special", "unverified"):
            errs.append(f"{tag}: low_equity.lem_base invalid")
        for band in le.get("bands") or []:
            if not (_is_num(band.get("lvr_from")) and _is_num(band.get("lvr_to")) and _is_num(band.get("margin_pct"))
                    and 0 <= band["lvr_from"] <= band["lvr_to"] <= 100 and 0 <= band["margin_pct"] <= 5):
                errs.append(f"{tag}: implausible band {band}")
        fhl = b.get("fhl") or {}
        if not isinstance(fhl.get("lender"), bool):
            errs.append(f"{tag}: fhl.lender must be true/false")
        if fhl.get("special_applies") not in (True, False, None):
            errs.append(f"{tag}: fhl.special_applies must be true, false or null")
        if not isinstance(b.get("notes", []), list):
            errs.append(f"{tag}: notes must be a list")
    return errs


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"FAIL: cannot read {path}: {e}")
        return 1
    errs = validate(snap)
    if errs:
        print(f"FAIL: {path} has {len(errs)} problem(s):")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"OK: {path} ({len(snap['banks'])} banks)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
