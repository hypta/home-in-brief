# rules-api codes

Every machine-readable code that `POST /v1/rules` can return, with what it means, when it
fires, and how a front end or the Gemini layer should present it. Codes ending in `:<x>`
carry a suffix (a layer name, bank id or KB item id).

Presentation rule of thumb: never turn "we could not check" into "no risk". Say what is
missing, and keep the wording neutral.

## Flags (`flags[]`, active flags only)

Each flag is `{id, source_ids}`. A flag is only listed when it applies.

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `lvr_over_80` | Deposit is under 20% of the price | Unrounded LVR > 80 | Say a low-equity margin or First Home Loan may apply; cite `rbnz_lvr`. |
| `dti_over_6` | Total debt exceeds 6x income | Unrounded DTI > 6 | Say lenders restrict high-DTI lending and the figure is above the RBNZ threshold; cite `rbnz_dti`. Show `high_dti_threshold`. |
| `fhl_deposit_below_5` | Deposit is below the 5% First Home Loan minimum | deposit / price < 5% while LVR > 80 | Say the First Home Loan scenario is not shown because the deposit is under 5%. |
| `fhl_income_over_cap` | Income is above the First Home Loan cap for this household | Income over $150,000, or over $95,000 for a single applicant | Say the First Home Loan scenario is not shown; quote the caps (`income_caps`). |
| `fhl_household_type_unknown` | Eligibility depends on household type, which was not given | Income between $95,000 and $150,000 and `household_type` is null | The scenario is present with `eligibility: "conditional"`; show `eligibility_note` and ask for the household type. |

## Warnings (`warnings[]`)

### Missing or unusable inputs

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `price_missing` | No price to calculate with | No `price_override` and no usable CV | Ask the user for a price; show no loan figures. |
| `deposit_missing` | Price known, deposit not | `deposit` is null | Ask for a deposit. |
| `income_missing` | DTI and First Home Loan cannot be checked | `household_income` is null | Ask for income if the user wants DTI or First Home Loan figures. |
| `cash_purchase_no_loan` | Deposit equals the price | deposit == price | Say no loan is needed, so no repayment scenarios are shown. |
| `fhl_skipped_income_missing` | First Home Loan not evaluated | LVR > 80 but income is null | Ask for income. |
| `fhl_no_lenders` | The snapshot has no First Home Loan lenders | Eligible but no bank has `fhl.lender` | Say lender information is unavailable. |

### Property data we could not use

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `cv_unavailable` | CV not used | `property.cv.status` is not `found` | Say the council valuation could not be retrieved. |
| `title_type_unavailable` | Title type not used | status is not `found` | Say the title type could not be confirmed. |
| `year_built_unavailable` | Year built not used | status is not `found` | Ask the user for the year built; do not guess. |
| `cladding_unavailable` | Cladding not used | status is not `found` | Ask the user for the cladding; do not guess. |
| `cv_invalid` | CV value present but not a positive finite number | CV `found` with a bad value | Treat as no CV. |
| `year_built_unparseable` | Year is neither a year nor a decade like `1990s` | Unparseable `year_built.value` | Treat as unknown. |
| `chat_year_built_unparseable` | Same, for the user-supplied year | Unparseable `chat_supplied.year_built` | Ask again. |

### Hazards

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `hazards_unavailable` | No primary hazard layer could be checked | No primary layers, or all are `unavailable` | Say hazard checks were not possible. Never say "no hazards found". |
| `hazards_partial:<layer>` | One primary layer could not be checked | Primary layer is `unavailable` or `not_found` | Name the layer as unchecked. The original reason is in `hazard_gaps[]`. |
| `hazard_status_missing:<layer>` | Layer had a null status | `status` is null | Treated as `unavailable`. |
| `hazard_status_unknown:<layer>` | Layer had an unrecognised status | Status not one of intersects, none, unavailable, not_found | Treated as `unavailable`. |
| `hazard_priority_unknown:<layer>` | Layer had a null or unknown priority | Priority not primary or secondary | Treated as `secondary`. |

### Rates

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `rates_unavailable` | Rates snapshot could not be read | No snapshot or a read error | Bank and First Home Loan scenarios are missing; `user_rate` still works if a rate was given. |
| `rates_snapshot_stale` | Oldest bank rate is more than 30 days old (NZ date) | Oldest `captured_at` older than 30 days | Show `rates_snapshot_date` prominently and tell the user to check current rates. |
| `bank_skipped:<bank_id>` | A bank has no applicable rate for this LVR | Missing 1-year rate or no low-equity band | Omit that bank; do not imply it has no rate. |
| `lem_base_unverified:<bank_id>` | The base rate for the bank's low-equity margin is unverified | LVR > 80 and `lem_base` is `unverified` | Mark that bank's rate as approximate. |

### Knowledge base

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `kb_unavailable` | Knowledge base could not be read | Repository error | Say background notes are unavailable; see `kb_empty_reason`. |
| `kb_hash_mismatch:<id>` | A verified item changed after review | Recomputed content hash differs from `verified_content_hash`, or the hash is missing | Item is withheld. Not user-facing. |
| `kb_verification_incomplete:<id>` | A verified item lacks `verified_by` or `verified_at` | Either field empty | Item is withheld. Not user-facing. |

## Other fields that explain absence

| Field | Values | Presentation |
|---|---|---|
| `kb_empty_reason` | `no_verified_items`, `no_matching_items`, `kb_unavailable` | Only set when `kb_items` is empty. Do not invent notes. |
| `kb_items_from_user_input` | item ids | Items chosen because of chat-supplied year or cladding. Say "based on what you told us". |
| `kb_includes_draft` | boolean | True means draft items may appear. Do not show to end users. |
| `hazard_gaps` | `{layer, status, reason}` | Why a primary layer was not checked. |
| `price_used.basis` | `cv`, `user`, `none` | Say whether the price is the council valuation or the user's own figure. |
| `first_home_loan.eligibility` | `eligible`, `conditional` | Show `eligibility_note` when conditional. |
| `first_home_loan.full_premium_assumed_for` | bank ids | Ranges for these banks assume the full 1.2% premium; say so. |
| `disclaimer` | text | Show verbatim with the report. |

## Error responses

All 4xx and 5xx responses use `{"error": {"code", "message", "fields": [{"field", "message"}]}}`.

| Code | Status | Meaning |
|---|---|---|
| `validation_error` | 422 | Bad input; `fields` names each field and the reason. |
| `not_found` | 404 | Resource missing (for example no rates snapshot). |
| `internal_error` | 500 | Unexpected failure. No details are exposed. |
