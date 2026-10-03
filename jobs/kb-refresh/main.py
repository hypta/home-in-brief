"""kb-refresh: re-fetch the source page of every KB item and check its quote is still there.

Only sources with tos_status == "allowed" are fetched. Every other item is recorded as
manual_check_required without any request. See kb/MANUAL_CHECK.md.

Only last_check (and verified -> needs_review) ever changes. Item text is never rewritten.
"""
import os
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checker import Fetcher, check_item, manual_run, next_status  # noqa: E402
from store import FirestoreStore, LocalStore  # noqa: E402


RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"  # base of every run id; a random -xxxx suffix keeps ids unique
RUN_ID_RE = r"\d{8}T\d{6}Z-[0-9a-f]{4}"


def make_run_id(clock, rand=lambda: secrets.token_hex(2)) -> str:
    return f"{clock()}-{rand()}"


def run_id_to_iso(run_id: str) -> str:
    """UTC run id -> ISO 8601 with offset; anything unparseable is returned unchanged."""
    try:
        return datetime.strptime(run_id[:16], RUN_ID_FORMAT).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return run_id


def run_job(store, fetch=None, clock=None, rand=None) -> tuple[dict, int]:
    """clock() returns UTC YYYYMMDDTHHMMSSZ; ids are clock + "-xxxx" (4 hex), used as Firestore doc ids.
    The stored `at` is ISO 8601."""
    base_clock = clock or (lambda: datetime.now(timezone.utc).strftime(RUN_ID_FORMAT))
    clock = (lambda: make_run_id(base_clock, rand)) if rand else (lambda: make_run_id(base_clock))
    own_fetcher = fetch is None
    fetch = fetch or Fetcher()
    sources = store.load_sources()
    checked = manual = changed = 0
    errors: list[dict] = []
    hard_errors = 0
    try:
        for item in store.load_items():
            src = sources.get(item.get("source_id"))
            tos = src.get("tos_status") if src else None
            try:
                run_id = clock()
                if tos != "allowed":
                    run = manual_run(run_id, f"tos_status={tos or 'source_not_found'}", item)
                elif item.get("source_url") != src.get("url"):
                    # terms were approved for the source's URL only, never for a different one
                    run = manual_run(run_id, "source_url_differs_from_approved_source_url", item)
                else:
                    run = check_item(item, fetch=fetch, now=run_id)
                run["run_id"], run["at"] = run_id, run_id_to_iso(run_id)
                status = next_status(item, run)
                store.record(item, run, status)
            except Exception as e:  # non-network failure (parse, store write, bad item)
                errors.append({"id": item.get("id"), "error": f"{type(e).__name__}: {e}", "network": False})
                hard_errors += 1
                continue
            if run["manual_check_required"]:
                manual += 1
                continue
            checked += 1
            if status != item.get("status"):
                changed += 1
            if run["error"]:
                errors.append({"id": item["id"], "error": run["error"], "network": run["network_error"]})
                hard_errors += 0 if run["network_error"] else 1
    finally:
        if own_fetcher:
            fetch.close()
    job_id = clock()
    job_run = {"job": "kb-refresh", "run_id": job_id, "at": run_id_to_iso(job_id), "checked": checked, "manual_check_required": manual,
               "changed": changed, "errors": errors}
    store.finish(job_run)
    return job_run, (1 if hard_errors else 0)


def main() -> int:
    backend = os.environ.get("DATA_BACKEND", "local")
    store = FirestoreStore() if backend == "firestore" else LocalStore()
    job_run, code = run_job(store)
    print(f"kb-refresh ({backend}): auto_checked={job_run['checked']} "
          f"manual_check_required={job_run['manual_check_required']} changed={job_run['changed']} "
          f"errors={len(job_run['errors'])}")
    if backend == "local":
        for r in store.report["runs"]:
            tag = "MANUAL " + r["manual_reason"] if r["manual_check_required"] else f"http={r['http_status']}"
            print(f"  {r['id']}: quote_found={r['quote_found']} {tag} err={r['error']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
