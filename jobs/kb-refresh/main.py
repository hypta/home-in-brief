"""kb-refresh: re-fetch every KB item's source page and check its quote is still there.

Only last_check (and verified -> needs_review) ever changes. Item text is never rewritten.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checker import check_item, next_status  # noqa: E402
from store import FirestoreStore, LocalStore  # noqa: E402


def run_job(store, fetch=None, clock=None) -> tuple[dict, int]:
    clock = clock or (lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    checked = changed = 0
    errors: list[dict] = []
    hard_errors = 0
    for item in store.load_items():
        kwargs = {"fetch": fetch} if fetch else {}
        try:
            run = check_item(item, now=clock(), **kwargs)
            status = next_status(item, run)
            store.record(item, run, status)
        except Exception as e:  # non-network failure (parse, store write, bad item)
            errors.append({"id": item.get("id"), "error": f"{type(e).__name__}: {e}", "network": False})
            hard_errors += 1
            continue
        checked += 1
        if status != item.get("status"):
            changed += 1
        if run["error"]:
            errors.append({"id": item["id"], "error": run["error"], "network": run["network_error"]})
            hard_errors += 0 if run["network_error"] else 1
    job_run = {"job": "kb-refresh", "at": clock(), "checked": checked, "changed": changed, "errors": errors}
    store.finish(job_run)
    return job_run, (1 if hard_errors else 0)


def main() -> int:
    backend = os.environ.get("DATA_BACKEND", "local")
    store = FirestoreStore() if backend == "firestore" else LocalStore()
    job_run, code = run_job(store)
    print(f"kb-refresh ({backend}): checked={job_run['checked']} changed={job_run['changed']} "
          f"errors={len(job_run['errors'])}")
    if backend == "local":
        for r in store.report["runs"]:
            print(f"  {r['id']}: quote_found={r['quote_found']} http={r['http_status']} err={r['error']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
