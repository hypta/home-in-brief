# Home Buyer Copilot

A free pre-offer report for first-time home buyers in Auckland, New Zealand. Before making an offer, a buyer gets a
plain-English view of what a property could really cost and where the risks are: affordability under current lending
rules (LVR and DTI limits, deposit, repayments at current bank rates), the property's title type, age and cladding,
natural-hazard and insurance exposure, and the Council valuation (CV), which is not a market price.

This is general information, not financial, legal or insurance advice. Verify everything with a licensed adviser,
lawyer and insurer before you make an offer.

License: TBD by the team.

## Status

This repo is a monorepo shared by two people. **What works today** is the rules side (owner B): a deterministic rules
and calculation API, a knowledge base with source quotes, and jobs that keep rates and knowledge-base data fresh.
The property-data API (owner A, `services/property-api/`, port 8081) is built. The Gemini layer (summary, validator,
follow-up questions) is owned by B and the frontend (`web`) by A; both are in progress.

## Repo layout

| Path | Owner | What it is |
|---|---|---|
| `services/property-api/` | A | Collects property data (CV, title, hazards) and calls rules-api |
| `services/rules-api/` | B | FastAPI service: loan, LVR, DTI, repayment scenarios, which knowledge-base items apply |
| `jobs/rates-refresh/` | B | Job that refreshes bank rates (Gemini extraction), gated by each bank's website terms |
| `jobs/kb-refresh/` | B | Job that re-checks knowledge-base quotes against their source pages |
| `shared/b_common/` | shared | Pydantic schemas and the data-access layer (local files or Firestore) |
| `kb/` | shared | Knowledge-base seed items and sources, validator, `MANUAL_CHECK.md` |
| `data/` | shared | `rates_seed.json`, the hand-maintained rates snapshot |
| `scripts/` | B | `seed_firestore.py`, `seed_rates.py`, `smoke.py`, `validate_rates_seed.py` |
| `openapi.json` | B | OpenAPI description of rules-api |

Docs: `LOCAL.md` (run locally), `DEPLOY.md` (Cloud Run + Firestore, deployed by GitHub Actions), `RATES_UPDATE.md` (update rates by hand), `CODES.md` (codes used by the API),
`kb/MANUAL_CHECK.md` (verify knowledge-base items by hand).

## Architecture

```
                 Browser (frontend, in progress)
                           |
                           v
                  property-api (A)  ---> property data sources
                           |
                           |  POST /v1/rules   (property_context + user_inputs)
                           v
        +--------------- rules-api (B, private Cloud Run) ---------------+
        |  calc: loan, LVR, DTI, repayment scenarios, First Home Loan     |
        |  rules: select applicable knowledge-base items                  |
        +-----------------------------+----------------------------------+
                                      | reads
                                      v
                        Firestore  (rates_latest, rates_snapshots,
                                    kb_items, sources, job_runs)
                                      ^ writes
              +-----------------------+------------------------+
              |                                                |
     rates-refresh (Cloud Run job)                   kb-refresh (Cloud Run job, weekly)
     fetch rates page -> Gemini extract              fetch source page -> check quote
     (only banks whose terms allow it)               (only sources whose terms allow it)

   Gemini report-writing layer (in progress): rules output + knowledge-base quotes -> written report
```

rules-api never calls a language model. Numbers come from code; knowledge-base claims come from human-verified quotes
with their source and attribution.

## Quickstart (about 5 minutes, no Docker, no cloud account)

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/). Run from the repo root.

```bash
git clone <this repo> && cd <repo directory>
make setup          # uv venv --python 3.12 .venv + pip-installs each service's requirements.txt and pytest
make test           # all unit tests
make local-api      # rules-api on http://localhost:8080 (DATA_BACKEND=local, reads kb/seed and data/rates_seed.json)
make smoke          # in a second shell: checks /healthz and /v1/rules (expects loan 880000, LVR 88.0, DTI 6.07)
```

Without `uv`: `python3.12 -m venv .venv`, then `.venv/bin/pip install -r services/rules-api/requirements.txt -r jobs/rates-refresh/requirements.txt -r jobs/kb-refresh/requirements.txt pytest`.
Or install one service only, for example `.venv/bin/pip install -r services/rules-api/requirements.txt pytest`.

### Docker plus Firestore emulator

Needs a running Docker daemon.

```bash
make up       # build and start the Firestore emulator (127.0.0.1:8085) and rules-api (127.0.0.1:8080)
make seed     # load kb/seed and data/rates_seed.json into the emulator (emulator data is not persisted)
make smoke    # same check, now served from Firestore
make kb       # run the kb-refresh job against the emulator
make down
```

Ports are published on `127.0.0.1` only, because the emulator and the API have no authentication. More detail in
`LOCAL.md`. Production runs on Cloud Run: a GitHub Actions workflow (`.github/workflows/deploy.yml`) tests every push to
`main` and deploys the changed services; the one-time GCP setup (Firestore, Workload Identity Federation, Scheduler, IAM) is in `DEPLOY.md`.

## API (rules-api)

| Method and path | Purpose |
|---|---|
| `GET /health` | Liveness check (use this when deployed: Cloud Run reserves some paths ending in `z`) |
| `GET /healthz` | Same response, for local use |
| `POST /v1/rules` | Loan, LVR, DTI, repayment scenarios and the ids of applicable knowledge-base items |
| `GET /v1/kb/items?ids=a,b` | Knowledge-base items (quote, source URL, `source_id`, licence); all visible items if `ids` is empty |
| `GET /v1/rates/latest` | Latest bank rates snapshot (404 if none loaded) |
| `GET /v1/sources` | Sources behind the knowledge base, with `attribution`, `tos_status`, `tos_url` and `tos_checked_at` (join on an item's `source_id`) |

Only `verified` knowledge-base items are returned. Set `INCLUDE_DRAFT=true` (done in `docker-compose.yml`) to include
`draft` items for development; the response flags this with `kb_includes_draft`. 
The `first_home_loan` scenario has a `lenders[]` list, one entry per partner lender. A value that is not known is `null`
with `rate_basis: "unknown"`, and a note says when eligibility is conditional. The income caps used are $95,000 for a
single applicant and $150,000 for two or more applicants or a single applicant with dependants.
`openapi.json` is the contract for all request and response fields.

Errors use one format for every 4xx and 5xx response: `{"error": {"code": "...", "message": "...", "fields": [{"field": "...", "message": "..."}]}}`.
Response codes, warnings and flags are explained in `CODES.md`.

Example (a $1,000,000 property by CV, $120,000 deposit, $150,000 household income, $30,000 other debts):

```bash
curl -s http://localhost:8080/v1/rules -H 'Content-Type: application/json' -d '{
  "property_context": {"property": {"cv": {"status": "found", "value": 1000000, "valuation_date": "2024-05-01"}}},
  "user_inputs": {"deposit": 120000, "household_income": 150000, "existing_debts": 30000,
                  "household_type": "multiple"}
}'
```

The CV is used only when `cv.status` is `"found"`. The response includes `loan_amount` (880000), `lvr_pct` (88.0), `dti` (6.07), `scenarios`, `kb_items`,
`rates_snapshot_date` and `warnings`.

In production rules-api is a private Cloud Run service; callers authenticate with a Google ID token (`DEPLOY.md`, section 11).

## Data policy

- **Website terms gate automation.** Every bank in `jobs/rates-refresh/config/banks.yaml` and every source in
  `kb/seed/sources.json` carries a `tos_status` (`allowed`, `unclear` or `prohibited`) with the terms URL and review
  date (reviewed 2026-10-03, not legal advice). Jobs fetch only `allowed` entries, send an honest `User-Agent`, honour
  `robots.txt`, and treat a block or bot challenge as final: no retry through a browser, no bypass.
- **No scraping of sites that disallow it.** As of the review no bank is `allowed`, so no rates page is fetched automatically.
- **Rates are a manual snapshot.** `data/rates_seed.json` is updated by hand from each bank's public rates page
  (`RATES_UPDATE.md`), with a `captured_at` per bank. Rates move; the report shows the snapshot date.
- **Knowledge-base items need human verification.** Each item holds a word-for-word quote from an official source. Items
  start as `draft`; a person checks the quote against the source and marks it `verified` (`kb/MANUAL_CHECK.md`). The job
  never edits item text and downgrades an item whose quote disappears.
- **Attribution.** Quotes are shown with the source's name, URL and licence/attribution text from `kb/seed/sources.json`.
- The CV is an Auckland Council rating valuation (date shown), not a market price.

## Limitations

- The Gemini report layer and the frontend are not finished yet.
- Most seeded knowledge-base items (20+) are `draft` until a person verifies them, so a production rules-api (no
  `INCLUDE_DRAFT`) returns none.
- Rates cover 11 lenders from a manual snapshot and can be out of date. Non-bank lenders are not covered.
- Calculations are simplified (for example fixed-rate owner-occupier scenarios only) and are not a lender's assessment.
- Auckland only. Not financial, legal or insurance advice.
