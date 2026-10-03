"""Validation of extracted rates. A bank's new values are adopted only if this returns no problems."""
from .extract import TERMS, Extracted

MIN_RATE = 2.0
MAX_RATE = 12.0
MAX_JUMP_PP = 1.0


def to_rate_map(items) -> dict[str, float]:
    return {i.term: i.rate_pct for i in items}


def _check_rate(label: str, v, problems: list[str]):
    if not isinstance(v, (int, float)) or not (MIN_RATE <= v <= MAX_RATE):
        problems.append(f"{label}={v} outside {MIN_RATE}-{MAX_RATE}")


def validate(special: dict, standard: dict | None, floating: float | None,
             bands: list[dict], prev: dict | None) -> list[str]:
    problems: list[str] = []
    if "1y" not in special:
        problems.append("missing special.1y")
    for group, rates in (("special", special), ("standard", standard or {})):
        for term, v in rates.items():
            if term not in TERMS:
                problems.append(f"{group}.{term} unknown term")
            else:
                _check_rate(f"{group}.{term}", v, problems)
    if floating is not None:
        _check_rate("floating", floating, problems)
    for b in bands:
        if not (0 <= b["lvr_from"] <= b["lvr_to"] <= 100) or not (0 <= b["margin_pct"] <= 5):
            problems.append(f"implausible band {b}")
    if prev:
        for group, rates in (("special", special), ("standard", standard or {})):
            old = prev.get(group) or {}
            for term, v in rates.items():
                if term in old and isinstance(v, (int, float)) and abs(v - old[term]) > MAX_JUMP_PP:
                    problems.append(f"{group}.{term} moved {old[term]} -> {v} (>{MAX_JUMP_PP}pp)")
        if floating is not None and prev.get("floating") is not None \
                and abs(floating - prev["floating"]) > MAX_JUMP_PP:
            problems.append(f"floating moved {prev['floating']} -> {floating} (>{MAX_JUMP_PP}pp)")
    return problems


def build_bank(bank: dict, ext: Extracted, page_sha256: str, prev: dict | None, captured_at: str | None = None):
    """Return (record, problems). record is None when validation fails."""
    special = to_rate_map(ext.special)
    standard = to_rate_map(ext.standard) or None
    bands = [b.model_dump() for b in ext.bands]
    problems = validate(special, standard, ext.floating, bands, prev)
    if problems:
        return None, problems
    rec = {
        "bank_id": bank["bank_id"],
        "name": bank["name"],
        "source_url": bank["source_url"],
        "captured_at": captured_at,
        "page_sha256": page_sha256,
        "page_effective_date": ext.page_effective_date,
        "special": special,
        "standard": standard,
        "floating": ext.floating,
        "special_requires_equity_pct": 20,
        "low_equity": {
            "mechanism": bank["mechanism"],
            "lem_base": bank.get("lem_base"),
            "bands": bands,
        },
        "fhl": {
            "lender": bank["fhl"]["lender"],
            "lmi_passthrough_pct": bank["fhl"].get("lmi_passthrough_pct"),
        },
        "notes": list(bank.get("notes") or []),
    }
    return rec, []
