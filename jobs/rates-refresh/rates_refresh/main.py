"""rates-refresh job: fetch -> extract -> validate -> write. Never overwrite good data with bad."""
import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .extract import gemini_extractor
from .fetch import fetch_bank
from .store import make_store
from .validate import build_bank

SCHEMA_VERSION = "0.1"
NZ = ZoneInfo("Pacific/Auckland")
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "banks.yaml"


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
    date = now.date().isoformat()
    prev = store.load_previous() or {}
    prev_by_id = {b["bank_id"]: b for b in prev.get("banks", [])}

    out_banks, updated, failed = [], [], []
    for bank in banks:
        bid = bank["bank_id"]
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
    store.write_job_run(now.strftime("%Y-%m-%dT%H-%M-%S%z"), job_run)
    return snapshot, job_run, (0 if updated else 1)


def fetch_only(banks) -> int:
    bad = 0
    for bank in banks:
        try:
            r = fetch_bank(bank)
            print(f"{bank['bank_id']:9} configured={bank['fetch']:8} used={r.method:8} "
                  f"fell_back={r.fell_back} status={r.status} len={len(r.text)} "
                  f"blocked={r.blocked} reason={r.block_reason}")
            bad += r.blocked
        except Exception as e:
            bad += 1
            print(f"{bank['bank_id']:9} ERROR {type(e).__name__}: {e}")
    return 1 if bad == len(banks) else 0


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
    _, job_run, code = run(banks, make_store())
    print(f"updated={job_run['updated']} failed={job_run['failed']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
