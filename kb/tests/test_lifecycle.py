import copy
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kb"))

import validate as V  # noqa: E402
from content_hash import content_hash, effective_status, verification_problems  # noqa: E402

spec = importlib.util.spec_from_file_location("seed_firestore", ROOT / "scripts" / "seed_firestore.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


def item(**kw):
    base = {"id": "kb_t", "topic": "insurance", "applies_when": {"always": True}, "claim_en": "Claim.",
            "quote": "a short quote", "source_id": "s", "source_url": "https://e.test/p", "source_title": "T",
            "source_type": "official", "licence": "L", "next_step_en": "Step.", "verified_by": None,
            "verified_at": None, "verified_content_hash": None, "status": "draft", "last_check": None}
    base.update(kw)
    return base


def verified(**kw):
    it = item(**kw)
    it.update(status="verified", verified_by="Sam", verified_at="2026-10-04")
    it["verified_content_hash"] = content_hash(it)
    return it


def test_hash_covers_exactly_the_content_fields_and_ignores_whitespace():
    base = item()
    h = content_hash(base)
    for field, value in [("claim_en", "Other."), ("quote", "other quote"), ("source_url", "https://e.test/q"),
                         ("applies_when", {"always": False}), ("next_step_en", "Other step.")]:
        assert content_hash(item(**{field: value})) != h, field
    for field, value in [("status", "verified"), ("verified_by", "x"), ("last_check", {"a": 1}), ("licence", "Z"),
                         ("source_title", "Z"), ("topic", "lending"), ("claim_en", " Claim.  ")]:
        assert content_hash(item(**{field: value})) == h, field


def test_verification_problems_and_effective_status():
    assert verification_problems(item()) == [] and effective_status(item()) == "draft"
    assert verification_problems(verified()) == [] and effective_status(verified()) == "verified"
    edited = dict(verified(), claim_en="Changed.")
    assert "content changed" in " ".join(verification_problems(edited)) and effective_status(edited) == "needs_review"
    for k in ("verified_by", "verified_at", "verified_content_hash"):
        assert effective_status(dict(verified(), **{k: None})) == "needs_review"


def seed_dir(tmp_path, items, sources=None):
    d = tmp_path / "seed"
    d.mkdir()
    (d / "sources.json").write_text(json.dumps(sources or [{
        "id": "s", "title": "T", "url": "https://e.test/p", "licence": "L", "accessed_at": "2026-10-04",
        "source_type": "official", "attribution": "A", "tos_status": "allowed", "tos_url": "u",
        "tos_checked_at": "2026-10-04"}]))
    (d / "a.json").write_text(json.dumps(items))
    return d


def errs(tmp_path, items, capsys):
    out = V.validate(seed_dir(tmp_path, items))
    capsys.readouterr()
    return out


def test_validator_accepts_verified_seed_and_rejects_unsound_ones(tmp_path, capsys):
    assert errs(tmp_path, [verified()], capsys) == []


def test_validator_rejections(tmp_path, capsys):
    for i, bad in enumerate([dict(verified(), claim_en="Edited after review."),
                             dict(verified(), verified_by=None),
                             dict(verified(), verified_at=None),
                             dict(verified(), verified_content_hash=None),
                             dict(item(), verified_by="Sam"),
                             dict(item(), status="bogus"),
                             dict(item(), last_check={"x": 1})]):
        sub = tmp_path / str(i)
        sub.mkdir()
        assert errs(sub, [bad], capsys), bad


def test_validator_requires_source_terms_fields(tmp_path, capsys):
    d = seed_dir(tmp_path, [item()])
    s = json.loads((d / "sources.json").read_text())
    s[0]["tos_status"] = "maybe"
    s[0]["attribution"] = ""
    (d / "sources.json").write_text(json.dumps(s))
    out = V.validate(d)
    capsys.readouterr()
    assert any("tos_status" in e for e in out) and any("attribution" in e for e in out)


def test_real_seed_is_valid_and_hash_cli(capsys, monkeypatch):
    assert V.validate() == []
    capsys.readouterr()
    first = V.all_items()[0]
    monkeypatch.setattr(sys, "argv", ["validate.py", "--hash", first["id"]])
    import runpy
    try:
        runpy.run_path(str(ROOT / "kb" / "validate.py"), run_name="__main__")
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == content_hash(first)


# ---- seeding ---------------------------------------------------------------------------------

def test_new_verified_seed_is_written_verified_and_unsound_one_is_not():
    assert seed.merge_doc(verified(), None)["status"] == "verified"
    assert seed.merge_doc(dict(verified(), claim_en="Edited."), None)["status"] == "needs_review"


def test_seed_promotes_existing_draft_or_needs_review_to_verified_and_keeps_last_check():
    for old_status in ("draft", "needs_review"):
        existing = item(status=old_status, last_check={"quote_found": True})
        doc = seed.merge_doc(verified(), existing)
        assert doc["status"] == "verified" and doc["last_check"] == {"quote_found": True}


def test_changed_content_cannot_ride_on_an_old_firestore_verification():
    existing = verified()  # verified in Firestore for the old text
    edited_seed = item(claim_en="A different number.")
    doc = seed.merge_doc(edited_seed, existing)
    assert doc["status"] == "needs_review" and doc["claim_en"] == "A different number."
    assert doc["verified_by"] == "Sam"  # evidence is kept for audit, but does not make it verified


def test_unchanged_content_keeps_firestore_verification_and_needs_review():
    assert seed.merge_doc(item(), verified())["status"] == "verified"
    existing = item(status="needs_review", last_check={"quote_found": False})
    assert seed.merge_doc(item(), existing)["status"] == "needs_review"
    changed = dict(existing)
    assert seed.merge_doc(item(claim_en="New."), changed)["status"] == "draft"  # new content, fresh draft


def test_removed_ids_are_retired_not_served():
    writes, retired = seed.plan([item(id="keep")], {"keep": item(id="keep"), "gone": verified(id="gone"),
                                                     "old": item(id="old", status="retired")})
    assert list(writes) == ["keep"] and retired == ["gone"]


def test_seed_does_not_mutate_inputs():
    s, e = verified(), item(last_check={"a": 1})
    s0, e0 = copy.deepcopy(s), copy.deepcopy(e)
    seed.merge_doc(s, e)
    assert (s, e) == (s0, e0)
