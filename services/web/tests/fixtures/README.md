# Recorded upstream responses

Real responses, not hand-written. Recorded on 2026-10-04 from rules-api at commit 6442c73, run in-process with
`DATA_BACKEND=local INCLUDE_DRAFT=true`. The property context sent was
`services/property-api/fixtures/property_context_740_sandringham_road_extension.json` (a public reserve).

| File | Request to `POST /v1/rules` |
|---|---|
| `rules_output_low_deposit.json` | deposit 60000, household_income 120000, existing_debts 10000 (200) |
| `rules_error_negative_deposit.json` | deposit -5, household_income 120000 (422) |

Recorded on 2026-10-10 from report-api at commit f44b549. The request to `POST /v1/report` was the property context
above, `user_inputs` of deposit 60000, household_income 120000, existing_debts 10000, and `rules_output_low_deposit.json`.

| File | How |
|---|---|
| `report_low_deposit.json` | Served on localhost with `GEMINI_MODEL=gemini-3.5-flash-lite`; KB from rules-api run with `DATA_BACKEND=local INCLUDE_DRAFT=true` (200) |
| `report_error_model_unavailable.json` | In-process with `KB_SOURCE=local` and no `GEMINI_API_KEY` (503) |

`property_api_error_short_query.json` is property-api's own answer to `GET /v1/addresses?q=ab` (422), recorded the
same day from the working tree, in-process. No upstream call is involved.
