# Recorded upstream responses

Real responses, not hand-written. Recorded on 2026-10-04 from rules-api at commit a7501d6, run in-process with
`DATA_BACKEND=local INCLUDE_DRAFT=true`. The property context sent was
`services/property-api/fixtures/property_context_740_sandringham_road_extension.json` (a public reserve).

| File | Request to `POST /v1/rules` |
|---|---|
| `rules_output_low_deposit.json` | deposit 60000, household_income 120000, existing_debts 10000, everything else null (200) |
| `rules_error_deposit_over_price.json` | same, with deposit 900000 (422) |

`property_api_error_short_query.json` is property-api's own answer to `GET /v1/addresses?q=ab` (422), recorded the
same day from the working tree, in-process. No upstream call is involved.
