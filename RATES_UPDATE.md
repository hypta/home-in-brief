# Updating bank rates by hand

The automatic `rates-refresh` job only fetches banks whose website terms allow it (`tos_status: allowed` in
`jobs/rates-refresh/config/banks.yaml`). None of the 11 lenders is `allowed` at the moment (NZHL is `prohibited`,
the others `unclear`), so `data/rates_seed.json` is maintained by hand. The job and its schedule stay in the repo
but the schedule is not enabled (see `DEPLOY.md`).

## Steps

1. Open the bank's rates page in a normal browser: the `source_url` of its row in `data/rates_seed.json`
   (the same URL is in `banks.yaml`). Expand any collapsed sections.
2. Edit that bank's row in `data/rates_seed.json`:
   - `special` and `standard`: owner-occupier fixed rates per term, using only the keys `6m`, `1y`, `18m`, `2y`,
     `3y`, `4y`, `5y` (omit terms the bank does not list). `special` is the rate for borrowers with 20% or more
     equity; `standard` is the rate below that (`null` if the bank shows none). `1y` under `special` is required.
   - `floating`: the owner-occupier floating or variable rate, or `null`.
   - `low_equity.bands`: low equity margin bands as `lvr_from`, `lvr_to`, `margin_pct` (empty if not published).
   - `page_effective_date`: the date the page states, as written, or `null`.
   - Ignore first-home-buyer-only promotions, revolving credit, investor and reverse-equity rates.
3. Set that row's `captured_at` to the time you read the page (ISO 8601, e.g. `2026-10-04T09:30:00+13:00`).
   Leave other banks' `captured_at` alone: each row carries its own date. Update the top-level `captured_at`
   to the same time if you touched any row.
4. Validate: `.venv/bin/python scripts/validate_rates_seed.py` (checks the format, rates between 2% and 12%, a `1y`
   special rate, `captured_at` and `source_url` on every bank). Fix anything it lists.
5. Run the tests: `make test`.
6. Commit `data/rates_seed.json` with a message such as `rates: update ASB and BNZ (read 2026-10-04)`.
   If the app runs from Firestore, load the file with `scripts/seed_rates.py` (see `LOCAL.md` / `DEPLOY.md`).

## Turning automation on for a bank

Only after its terms (or written permission from the bank) clearly allow automated access: set
`tos_status: allowed`, fill `tos_url`, `tos_checked_at` and `tos_note`, add `render: browser` only if the rates
need JavaScript, run `make rates-fetch`, then enable the Cloud Scheduler job described in `DEPLOY.md`.
