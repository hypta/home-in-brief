"""Validation of extracted rates. A bank's new values are adopted only if this returns no problems."""
from .extract import TERMS, Extracted

MIN_RATE = 2.0
MAX_RATE = 12.0
MAX_JUMP_PP = 1.0
MAX_MARGIN_PCT = 3.0


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
    last_to = None
    for b in sorted(bands, key=lambda x: x["lvr_from"]):
        if not (0 <= b["lvr_from"] <= b["lvr_to"] <= 100) or not (0 <= b["margin_pct"] <= MAX_MARGIN_PCT):
            problems.append(f"implausible band {b}")
        if last_to is not None and b["lvr_from"] <= last_to:
            problems.append(f"overlapping band {b}")
        last_to = b["lvr_to"]
    if prev:
        for group, rates in (("special", special), ("standard", standard or {})):
            old = prev.get(group) or {}
            for term, v in rates.items():
                if term in old and isinstance(v, (int, float)) and abs(v - old[term]) > MAX_JUMP_PP:
                    problems.append(f"{group}.{term} moved {old[term]} -> {v} (>{MAX_JUMP_PP}pp)")
        old_bands = {(x["lvr_from"], x["lvr_to"]): x["margin_pct"]
                     for x in (prev.get("low_equity") or {}).get("bands") or []}
        for b in bands:
            old = old_bands.get((b["lvr_from"], b["lvr_to"]))
            if old is not None and abs(b["margin_pct"] - old) > MAX_JUMP_PP:
                problems.append(f"band {b['lvr_from']}-{b['lvr_to']} margin moved {old} -> {b['margin_pct']} (>{MAX_JUMP_PP}pp)")
        if floating is not None and prev.get("floating") is not None \
                and abs(floating - prev["floating"]) > MAX_JUMP_PP:
            problems.append(f"floating moved {prev['floating']} -> {floating} (>{MAX_JUMP_PP}pp)")
    return problems


def build_bank(bank: dict, ext: Extracted, page_sha256: str, prev: dict | None, captured_at: str | None = None,
               prev_snapshot_captured_at: str | None = None):
    """Return (record, problems). record is None when validation fails.

    Only machine-extracted fields are replaced. Everything curated by hand in the previous row (fhl.*, notes,
    name, mechanism ...) is kept. Terms, floating or LEM bands missing from the new extraction fall back to the old
    values; `carried_over_captured_at` maps each kept value to its original capture time, and a row that still
    contains carried values keeps its old `captured_at` (and stale_since) instead of looking freshly captured.
    """
    special = to_rate_map(ext.special)
    standard = to_rate_map(ext.standard) or None
    bands = [b.model_dump() for b in ext.bands]
    problems = validate(special, standard, ext.floating, bands, prev)
    if problems:
        return None, problems

    fhl_cfg = bank["fhl"]
    base = {
        "bank_id": bank["bank_id"],
        "name": bank["name"],
        "source_url": bank["source_url"],
        "special_requires_equity_pct": 20,
        "low_equity": {"mechanism": bank["mechanism"], "lem_base": bank.get("lem_base"), "bands": []},
        "fhl": {
            "lender": fhl_cfg["lender"],
            "lmi_passthrough_pct": fhl_cfg.get("lmi_passthrough_pct"),
            "special_applies": fhl_cfg.get("special_applies"),
        },
        "notes": list(bank.get("notes") or []),
    }
    rec = {**base, **(prev or {})}  # previous row wins, so hand-maintained fields and unknown extras survive
    old_le = (prev or {}).get("low_equity") or {}
    rec["low_equity"] = {**base["low_equity"], **old_le}
    rec["fhl"] = {**base["fhl"], **((prev or {}).get("fhl") or {})}

    prev_when = (prev or {}).get("captured_at") or prev_snapshot_captured_at or captured_at
    prev_prov = (prev or {}).get("carried_over_captured_at") or {}

    def when(label: str) -> str:  # a value carried again keeps the time it was first captured
        return prev_prov.get(label) or prev_when

    carried: dict[str, str] = {}
    for group, new_vals in (("special", special), ("standard", standard)):
        old_vals = (prev or {}).get(group) or {}
        merged = dict(new_vals or {})
        for term, v in old_vals.items():
            if term not in merged:
                merged[term] = v
                carried[f"{group}.{term}"] = when(f"{group}.{term}")
        rec[group] = merged if (merged or group == "special") else None
    floating = ext.floating
    if floating is None and (prev or {}).get("floating") is not None:
        floating = prev["floating"]
        carried["floating"] = when("floating")
    rec["floating"] = floating
    old_bands = old_le.get("bands") or []
    merged_bands = list(bands)
    for o in old_bands:
        key = (o["lvr_from"], o["lvr_to"])
        if any((n["lvr_from"], n["lvr_to"]) == key for n in bands):
            continue
        if any(n["lvr_from"] <= o["lvr_to"] and o["lvr_from"] <= n["lvr_to"] for n in bands):
            continue  # a differently-cut new band covers it: the new data supersedes
        merged_bands.append(o)
        carried[f"low_equity.bands[{o['lvr_from']}-{o['lvr_to']}]"] = when(f"low_equity.bands[{o['lvr_from']}-{o['lvr_to']}]")
    rec["low_equity"]["bands"] = sorted(merged_bands, key=lambda b: b["lvr_from"])

    notes = [n for n in rec["notes"] if not n.startswith("carried_over:")]
    if not carried:
        notes = [n for n in notes if not n.startswith("stale_since:")]
    rec["notes"] = notes
    rec["page_sha256"] = page_sha256
    rec["page_effective_date"] = ext.page_effective_date
    rec["last_fetched_at"] = captured_at
    if carried:
        rec["captured_at"] = prev_when
        rec["carried_over_captured_at"] = carried
        rec["notes"].append("carried_over: " + ", ".join(carried) + " kept from previous snapshot")
    else:
        rec["captured_at"] = captured_at
        rec.pop("carried_over_captured_at", None)
    return rec, []
