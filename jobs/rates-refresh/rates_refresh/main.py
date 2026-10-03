"""rates-refresh job: fetch -> extract -> validate -> write. Never overwrite good data with bad."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .extract import gemini_extractor
from .fetch import fetch_bank
from .store import make_store
from .validate import build_bank

SCHEMA_VERSION = "0.1"
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
NZ = ZoneInfo("Pacific/Auckland")
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "banks.yaml"


def load_banks(path: Path = CONFIG_PATH) -> list[dict]:
    return yaml.safe_load(path.read_text())["banks"]


def _mark_stale(prev_bank: dict, date: str, prev_captured_at: str | None = None) -> dict:
    rec = dict(prev_bank)
    # keep the bank's own capture time; older snapshots only have the top-level one
    rec["captured_at"] = prev_bank.get("captured_at") or prev_captured_at
    notes = list(rec.get("notes") or [])
    if not any(n.startswith("stale_since:") for n in notes):  # keep the earliest date
        notes.append(f"stale_since: {date}")
    rec["notes"] = notes
    return rec


def run(banks, store, extractor=gemini_extractor, fetcher=fetch_bank, now: datetime | None = None):
    """Returns (snapshot | None, job_run, exit_code). Writes via store."""
    now = now or datetime.now(NZ)
    date = now.date().isoformat()  # snapshot key: NZ calendar day, same as captured_at[:10] (captured_at keeps its +12/+13 offset)
    prev = store.load_previous() or {}
    prev_by_id = {b["bank_id"]: b for b in prev.get("banks", [])}

    out_banks, updated, failed, skipped = [], [], [], []
    for bank in banks:
        bid = bank["bank_id"]
        if bank.get("tos_status") != "allowed":
            # terms do not clearly allow automated access: no request at all, keep the manually maintained data
            skipped.append(bid)
            if bid in prev_by_id:
                out_banks.append(prev_by_id[bid])
            continue
        record, reason = None, None
        try:
            page = fetcher(bank)
            if page.blocked:
                reason = f"fetch blocked via {page.method}: {page.block_reason}"
            else:
                ext = extractor(page.text, bank)
                record, problems = build_bank(bank, ext, page.sha256, prev_by_id.get(bid), now.isoformat())
                if problems:
                    reason = "validation: " + "; ".join(problems)
        except Exception as e:  # one bank must never take down the others
            reason = f"error: {type(e).__name__}: {e}"[:500]

        if record is not None:
            out_banks.append(record)
            updated.append(bid)
        else:
            failed.append({"bank_id": bid, "reason": reason})
            if bid in prev_by_id:
                out_banks.append(_mark_stale(prev_by_id[bid], date, prev.get("captured_at")))

    job_run = {
        "job": "rates-refresh",
        "updated": updated,
        "failed": failed,
        "skipped_by_policy": skipped,
        "finished_at": now.isoformat(),
    }
    snapshot = None
    if updated:
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "captured_at": now.isoformat(),
            "banks": out_banks,
        }
        store.write_snapshot(date, snapshot)
    run_id = now.astimezone(timezone.utc).strftime(RUN_ID_FORMAT)  # UTC, sortable, same as kb-refresh
    job_run["run_id"] = run_id
    store.write_job_run(run_id, job_run)
    attempted = len(banks) - len(skipped)
    return snapshot, job_run, (1 if attempted and not updated else 0)


NO_ALLOWED_MSG = (
    "No bank has tos_status=allowed in config/banks.yaml, so nothing was fetched (skipped_by_policy). "
    "Rates are maintained by hand: see RATES_UPDATE.md."
)


def fetch_only(banks) -> int:
    allowed = [b for b in banks if b.get("tos_status") == "allowed"]
    for b in banks:
        if b not in allowed:
            print(f"{b['bank_id']:9} skipped_by_policy tos_status={b.get('tos_status')}")
    if not allowed:
        print(NO_ALLOWED_MSG)
        return 0
    bad = 0
    for bank in allowed:
        try:
            r = fetch_bank(bank)
            print(f"{bank['bank_id']:9} render={bank.get('render', 'http'):8} used={r.method:8} "
                  f"status={r.status} len={len(r.text)} blocked={r.blocked} reason={r.block_reason}")
            bad += r.blocked
        except Exception as e:
            bad += 1
            print(f"{bank['bank_id']:9} ERROR {type(e).__name__}: {e}")
    return 1 if bad == len(allowed) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true", help="fetch pages and report; no Gemini, no writes")
    ap.add_argument("--bank", action="append", help="limit to bank_id (repeatable)")
    args = ap.parse_args(argv)
    banks = load_banks()
    if args.bank:
        banks = [b for b in banks if b["bank_id"] in args.bank]
    if args.fetch_only:
        return fetch_only(banks)
    if not any(b.get("tos_status") == "allowed" for b in banks):
        print(NO_ALLOWED_MSG)
    _, job_run, code = run(banks, make_store())
    print(f"updated={job_run['updated']} failed={job_run['failed']} skipped_by_policy={job_run['skipped_by_policy']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
