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

Recorded on 2026-10-10 from report-api at commit 4f1da41, served on localhost with
`GEMINI_MODEL=gemini-3.5-flash-lite`; its rules re-run went to rules-api on localhost (`DATA_BACKEND=local INCLUDE_DRAFT=true`). The request to
`POST /v1/followup` was the same three documents plus the question below and `history: []`.

| File | Question |
|---|---|
| `followup_year_supplied.json` | "It was built in 1997. What should I watch for?" (200) |
| `followup_error_empty_question.json` | "" (422, answered before any Gemini call) |

`property_api_error_short_query.json` is property-api's own answer to `GET /v1/addresses?q=ab` (422), recorded the
same day from the working tree, in-process. No upstream call is involved.
