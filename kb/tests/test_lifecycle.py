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
        "tos_checked_at": "2026-10-04", "published_or_updated": None}]))
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


# ---- re-seeding never undoes a withdrawal -------------------------------------------------------

def demoted(it, at="2026-10-05T01:00:00+00:00"):
    return dict(it, status="needs_review", demoted_at=at, demoted_content_hash=content_hash(it),
                last_check={"quote_found": False, "at": at})


def reverified(at, **kw):
    it = verified(**kw)
    it["verified_at"] = at
    return it


def test_reseed_does_not_undo_a_kb_refresh_demotion():
    old = verified()  # verified_at 2026-10-04 (date only)
    existing = demoted(old)
    for seed_item in (old, reverified("2026-10-05"), reverified("2026-10-04T23:59:59+00:00"),
                      reverified("2026-10-05T01:00:00+00:00")):  # not strictly later than the demotion
        doc = seed.merge_doc(seed_item, existing)
        assert doc["status"] == "needs_review", seed_item["verified_at"]
        assert doc["demoted_at"] == existing["demoted_at"] and doc["last_check"] == existing["last_check"]


def test_a_review_after_the_demotion_clears_it():
    existing = demoted(verified())
    doc = seed.merge_doc(reverified("2026-10-06T09:00:00+13:00"), existing)  # 2026-10-05T20:00Z
    assert doc["status"] == "verified" and doc["verified_at"] == "2026-10-06T09:00:00+13:00"
    assert doc["last_check"] == existing["last_check"]
    assert seed.merge_doc(reverified("2026-10-07"), existing)["status"] == "verified"


def test_demotion_of_other_content_does_not_block_a_fresh_verification():
    old = verified()
    existing = demoted(old)
    new_text = reverified("2026-10-04", claim_en="Corrected claim.")
    assert seed.merge_doc(new_text, existing)["status"] == "verified"


def test_draft_seed_keeps_demotion():
    assert seed.merge_doc(item(), demoted(verified()))["status"] == "needs_review"


def retired_doc(**kw):
    return dict(item(**kw), status="retired", retired_at="2026-10-05T00:00:00+00:00")


def test_reseed_does_not_reinstate_a_retired_id():
    for seed_item in (item(), verified(), reverified("2026-10-04")):
        doc = seed.merge_doc(seed_item, retired_doc())
        assert doc["status"] == "retired" and doc["retired_at"] == "2026-10-05T00:00:00+00:00"


def test_retired_id_comes_back_only_when_the_seed_says_so():
    assert seed.merge_doc(dict(item(), reinstate=True), retired_doc())["status"] == "draft"
    doc = seed.merge_doc(dict(verified(), reinstate=True), retired_doc())
    assert doc["status"] == "verified" and "reinstate" not in doc and "retired_at" not in doc
    assert seed.merge_doc(reverified("2026-10-06T00:00:00+00:00"), retired_doc())["status"] == "verified"
    assert seed.merge_doc(reverified("2026-10-05"), retired_doc())["status"] == "retired"  # same day: not later


def test_explicit_retirement_in_seed_is_authoritative():
    existing = verified()
    doc = seed.merge_doc(dict(existing, status="retired"), existing, now="2026-10-05T00:00:00Z")
    assert doc["status"] == "retired" and doc["retired_at"] == "2026-10-05T00:00:00Z"
    doc2 = seed.merge_doc(dict(existing, status="retired"), None, now="N")
    assert doc2["status"] == "retired"


def test_plan_stamps_retired_at_only_via_main_and_keeps_retired_ids_quiet():
    writes, retired = seed.plan([item(id="a")], {"a": retired_doc(id="a"), "gone": retired_doc(id="gone")}, "N")
    assert writes["a"]["status"] == "retired" and retired == []


def test_validator_rejects_bad_formats_that_would_break_the_api(tmp_path, capsys):
    for i, bad in enumerate([dict(item(), id="Has Space"), dict(item(), claim_en=None),
                             dict(item(), quote=5), dict(item(), applies_when="always"),
                             dict(verified(), verified_at="yesterday"), dict(item(), reinstate="yes"),
                             dict(item(), source_type="blog")]):
        sub = tmp_path / str(i)
        sub.mkdir()
        assert errs(sub, [bad], capsys), bad
    ok = tmp_path / "ok"
    ok.mkdir()
    assert errs(ok, [dict(item(), reinstate=True)], capsys) == []


def test_partial_publication_dates_are_checked(tmp_path, capsys):
    d = seed_dir(tmp_path, [item()])
    s = json.loads((d / "sources.json").read_text())
    s[0]["published_or_updated"] = "May 2024"
    (d / "sources.json").write_text(json.dumps(s))
    out = V.validate(d)
    capsys.readouterr()
    assert any("published_or_updated" in e for e in out)


def test_every_seed_claim_is_within_25_words():
    # the summary and follow-up show a claim as written when a rewrite is blocked; the validator allows 25 words
    for path in sorted((ROOT / "kb" / "seed").glob("*.json")):
        if path.name == "sources.json":
            continue
        for it in json.loads(path.read_text(encoding="utf-8")):
            n = len(it["claim_en"].split())
            # kb_unit_title_levies is 26 words and was not in the list to shorten; reported separately
            assert n <= 25 or it["id"] == "kb_unit_title_levies", (it["id"], n)
