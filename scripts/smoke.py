"""Smoke test for a running rules-api: /healthz and POST /v1/rules with the team spec example.

Usage: python scripts/smoke.py [base_url]   (default http://localhost:8080)
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
REQ = {
    "property_context": {"property": {"cv": {"value": 1_000_000, "valuation_date": "2024-05-01"}}},
    "user_inputs": {"deposit": 120000, "household_income": 150000, "existing_debts": 30000,
                    "household_type": "multiple"},
}
EXPECT = {"loan_amount": 880000, "lvr_pct": 88.0, "dti": 6.07}


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def main() -> int:
    print("healthz:", call("/healthz"))
    out = call("/v1/rules", REQ)
    bad = {k: (out.get(k), v) for k, v in EXPECT.items() if out.get(k) != v}
    print({k: out.get(k) for k in EXPECT}, "rates_snapshot_date=", out.get("rates_snapshot_date"))
    if bad:
        print("FAIL (got, expected):", bad)
        return 1
    print("smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
