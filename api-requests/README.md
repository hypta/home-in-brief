# API request collections

Ready-made requests for rules-api and property-api run locally (report-api and web have none).

| File | Use with |
|---|---|
| `rules-api.http`, `property-api.http` | VS Code [REST Client](https://marketplace.visualstudio.com/items?itemName=humao.rest-client) extension: open the file and click "Send Request" above a block |
| `home-in-brief.postman_collection.json` | Postman: Import → File. Collection variables `rules` (http://localhost:8080) and `property` (http://localhost:8081) |
| `bodies/*.json` | `POST /v1/rules` bodies recorded on 2026-10-04 from property-api output for 740 Sandringham Road Extension and 44 Lorne Street (their two listing-site links and four layer data dates differ from property-api's current fixtures) |
| `examples/scenario_first_home_couple.response.json` | The answer to the "first-home couple" request in `rules-api.http`, recorded on 2026-10-04 (bank rates and dates have changed since) |

## Start the services

From the repo root, after `make setup`:

```bash
make local-api
```

`make local-api` serves rules-api on port 8080 from the local seed files. All knowledge-base items are verified and
appear in responses. `INCLUDE_DRAFT=true` matters only when you add a draft item:

```bash
DATA_BACKEND=local INCLUDE_DRAFT=true PYTHONPATH=shared:services/rules-api .venv/bin/python -m uvicorn app.main:app --port 8080
```

property-api on port 8081 (needs `LINZ_API_KEY` in `.env`; without it only `/health` is useful:
`/v1/property-context` answers 200 with every field `unavailable`, and address search and the map answer 502):

```bash
cd services/property-api && set -a && . ../../.env && set +a && PYTHONPATH=../../shared ../../.venv/bin/python -m uvicorn app.main:app --port 8081
```

## What each rules-api example shows

| # | Case | Look for |
|---|---|---|
| 1 | CV $1,000,000, deposit $120,000, income $150,000, debts $30,000 | `loan_amount` 880000, `lvr_pct` 88.0, `dti` 6.07, `high_dti_threshold` 870000 |
| 2 | Real 740 Sandringham Road Extension context | `kb_overland_flow_path`, FHL `eligibility: "conditional"` |
| 3 | Real 44 Lorne Street context, LVR below 80 | no low-deposit items, no FHL scenario |
| 4 | Single applicant over the $95k cap | flag `fhl_income_over_cap` |
| 5 | User rate and chat-supplied build year/cladding | `user_rate` scenario, `kb_items_from_user_input` |
| 6 | Primary hazard layers unavailable | warnings `hazards_partial:*`, `hazard_gaps[]` |
| 7 | CV unavailable, price entered by user | `price_used.basis: "user"` |
| 8 | Deposit equals price | warning `cash_purchase_no_loan` |
| 9 | Negative deposit | 422 `{"error": {"code": "validation_error", ...}}` |
| 10 | `GET /v1/rules` | 405 `method_not_allowed` |

Codes are explained in [`CODES.md`](../CODES.md); the full contract is [`openapi.json`](../openapi.json).
