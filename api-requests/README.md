# API request collections

Ready-made requests for both local services.

| File | Use with |
|---|---|
| `rules-api.http`, `property-api.http` | VS Code [REST Client](https://marketplace.visualstudio.com/items?itemName=humao.rest-client) extension: open the file and click "Send Request" above a block |
| `home-buyer-copilot.postman_collection.json` | Postman: Import → File. Collection variables `rules` (http://localhost:8080) and `property` (http://localhost:8081) |
| `bodies/*.json` | `POST /v1/rules` bodies built from property-api's real fixtures (740 Sandringham Road Extension, 44 Lorne Street) |

## Start the services

From the repo root, after `make setup`:

```bash
make local-api
```

`make local-api` serves rules-api on port 8080 from the local seed files. Knowledge-base items are still drafts, so add
`INCLUDE_DRAFT=true` to see them in responses:

```bash
DATA_BACKEND=local INCLUDE_DRAFT=true PYTHONPATH=shared:services/rules-api .venv/bin/python -m uvicorn app.main:app --port 8080
```

property-api on port 8081 (needs `LINZ_API_KEY` in `.env` for address search and the map; `/healthz` and
`/v1/property-context` partly work without it):

```bash
cd services/property-api && set -a && . ../../.env && set +a && PYTHONPATH=../../shared ../../.venv/bin/python -m uvicorn app.main:app --port 8081
```

## What each rules-api example shows

| # | Case | Look for |
|---|---|---|
| 1 | Spec sample (§6.2/6.3) | `loan_amount` 880000, `lvr_pct` 88.0, `dti` 6.07, `high_dti_threshold` 870000 |
| 2 | Real 740 Sandringham Road Extension context | flood KB items, FHL `eligibility: "conditional"` |
| 3 | Real 44 Lorne Street context, LVR below 80 | no low-deposit items, no FHL scenario |
| 4 | Single applicant over the $95k cap | flag `fhl_income_over_cap` |
| 5 | User rate and chat-supplied build year/cladding | `user_rate` scenario, `kb_items_from_user_input` |
| 6 | Primary hazard layers unavailable | warnings `hazards_partial:*`, `hazard_gaps[]` |
| 7 | CV unavailable, price entered by user | `price_used.basis: "user"` |
| 8 | Deposit equals price | warning `cash_purchase_no_loan` |
| 9 | Negative deposit | 422 `{"error": {"code": "validation_error", ...}}` |
| 10 | `GET /v1/rules` | 405 `method_not_allowed` |

Codes are explained in [`CODES.md`](../CODES.md); the full contract is [`openapi.json`](../openapi.json).
