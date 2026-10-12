# API codes

Every machine-readable flag and warning that `POST /v1/rules` can return, and the warnings
report-api adds, with what each means, when it fires, and how a front end or the Gemini layer
should present it. Codes ending in `:<x>` carry a suffix (a layer name, bank id or KB item id).

Error codes and header values of report-api are in `services/report-api/README.md`. web adds its
own error codes (`too_large`, `rate_limited`, `upstream_unavailable`, `upstream_auth`,
`upstream_error`; `services/web/app/main.py`).

Presentation rule of thumb: never turn "we could not check" into "no risk". Say what is
missing, and keep the wording neutral.

## Flags (`flags[]`, active flags only)

Each flag is `{id, source_ids}`. A flag is only listed when it applies. The three First Home Loan
flags are evaluated only when LVR > 80, a rates snapshot is available and an income was given.

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `lvr_over_80` | Deposit is under 20% of the price | Unrounded LVR > 80 | Say a low-equity margin or First Home Loan may apply; cite `rbnz_lvr`. |
| `dti_over_6` | Total debt exceeds 6x income | Unrounded DTI > 6 | Say lenders restrict high-DTI lending and the figure is above the RBNZ threshold; cite `rbnz_dti`. Show `high_dti_threshold`. |
| `fhl_deposit_below_5` | Deposit is below the 5% First Home Loan minimum | deposit / price < 5% while LVR > 80 | Say the First Home Loan scenario is not shown because the deposit is under 5%. |
| `fhl_income_over_cap` | Income is above the First Home Loan cap for this household | Income over $150,000, or over $95,000 for a single applicant | Say the First Home Loan scenario is not shown; quote the caps (`fhl_income_caps`). |
| `fhl_household_type_unknown` | Eligibility depends on household type, which was not given | Income over $95,000 and up to $150,000, and `household_type` is null | The scenario, when present (not when the deposit is under 5% or no lender is listed), has `eligibility: "conditional"`; show `eligibility_note` and ask for the household type. |

## Warnings (`warnings[]`)

### Missing or unusable inputs

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `price_missing` | No price to calculate with | No `price_override` and no usable CV | Ask the user for a price; show no loan figures. |
| `deposit_missing` | Price known, deposit not | `deposit` is null | Ask for a deposit. |
| `existing_debts_assumed_zero` | DTI and the high-DTI threshold were calculated with no other debts | `existing_debts` is null or not sent | Say the DTI assumes the user has no other debts, and ask for the figure (enter 0 to confirm none). |
| `income_missing` | DTI and First Home Loan cannot be checked | A price and a deposit are known and `household_income` is null | Ask for income if the user wants DTI or First Home Loan figures. |
| `cash_purchase_no_loan` | Deposit equals the price | deposit == price | Say no loan is needed, so no repayment scenarios are shown. `lvr_pct` and `dti` are null (not 0). |
| `deposit_exceeds_price` | Deposit is larger than the price used | deposit > price | Treated like a cash purchase: loan is 0, no repayment scenarios. Say the deposit covers the whole price and invite the user to check the amounts (price override or deposit). Not an error (HTTP 200). `lvr_pct` and `dti` are null (not 0). |
| `fhl_skipped_income_missing` | First Home Loan not evaluated | LVR > 80, a rates snapshot is available and income is null | Ask for income. |
| `fhl_no_lenders` | The snapshot has no First Home Loan lenders | Eligible but no bank has `fhl.lender` | Say lender information is unavailable. |

### Property data we could not use

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `cv_unavailable` | CV not used | `property.cv.status` is not `found` | Say the council valuation could not be retrieved. |
| `title_type_unavailable` | Title type not used | status is not `found` | Say the title type could not be confirmed. |
| `year_built_unavailable` | Year built not used | status is not `found` | Do not guess. The page says in the This home section that the year built was not found and that the user can enter it in the form, once more in the summary's "Could not be found" notes, and in the one-line digest of the Build era and title section; it does not ask again in each answer. |
| `cladding_unavailable` | Cladding not used | status is not `found` | Do not guess. The page says in the This home section that the cladding was not found and that the user can enter it in the form, and once more in the summary's "Could not be found" notes; it does not ask again in each answer. |
| `cv_invalid` | CV value present but unusable | CV `found` but not a finite number in (0, 1e10] | Treat as no CV. |
| `year_built_unparseable` | Year information is unusable | `year_built.value` is not a year (1000-2200) or a decade such as `1990s`, or `decade_range` is malformed | Treat as unknown. |
| `title_type_unresolved` | LINZ lists title types but none was chosen | status is `found` and `title_type.value` is null while `linz_types` is not empty | No title-type notes are selected. Say the title type needs confirming (see `linz_types`); do not guess. |
| `chat_year_built_unparseable` | Same, for the user-supplied year | The records give no usable year and `chat_supplied.year_built` is not a year or a decade | Ask again. |
| `title_type_unmapped` | The title type is known but no knowledge base note covers it | `title_type.value` is set and `found`, and no verified KB item lists it in `applies_when.title_type` (for example Leasehold) | No title-type note is shown. Say this tool has no note for this title type and that a lawyer can explain it; do not reuse a note for another title type. |
| `year_built_user_conflict` | The user's year disagrees with the property record | `year_built` is `found` and `chat_supplied.year_built` is set, and the user's year (or decade) does not overlap the property's `decade_range` or value | The property record is still used. Tell the user both figures and ask which is right; suggest checking the LIM or building consent. |
| `cladding_user_conflict` | The user's cladding disagrees with the property record | `cladding` is `found` and `chat_supplied.cladding` is set and is a different cladding | The property record is still used. Tell the user both values and ask which is right. |

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
| `kb_includes_draft` | Draft knowledge-base items may be included (development mode) | Not in rules-api `warnings[]`: rules-api only sets the boolean field `kb_includes_draft` (see Other fields). report-api copies it into the `/v1/report` response's `warnings[]` when the field is true | Development mode only (`INCLUDE_DRAFT`, which `.github/deploy/targets.json` does not set); the page then marks draft notes as not yet checked. |

### report-api (`warnings[]` of `/v1/report` and `/v1/followup`, not of rules-api)

| Code | Meaning | Fires when | Presentation |
|---|---|---|---|
| `generation_degraded` | A model call was refused before it was sent, so part or all of the answer is the program's own wording | Daily or total call allowance used up (`GEMINI_DAILY_CALLS`, `GEMINI_EVENT_CALLS`), the kill switch (`protection/control.disabled`), `REPORT_DEGRADED`, a prompt over the size limit, the allowance store unavailable or slow, or no time left after the call was reserved. Not raised when Gemini itself fails (timeout, quota, error): the answer is then also the program's drafts, with `X-Report-Degraded-Reason: model_error` and, for a summary, `model: "code-draft"` | Say the sentences are the program's own. A summary written entirely by the program has `model: "code-draft"` whatever the cause. |
| `search_unavailable` | A Google Search was called for and gave no answer (follow-up only) | The search was refused (daily Search allowance), failed, ran out of time or named no source. Not added once the request has been stopped: `generation_degraded` then applies, or, after a provider quota error or a missing key, neither warning | Say a web search gave no answer just now. The reply is made without it. |

In mock mode (`REPORT_MOCK`) a follow-up may also carry `rules_rerun_unavailable`: the rules could not be run
again, and the answer uses the RulesOutput that was sent.

## Other fields that explain absence

| Field | Values | Presentation |
|---|---|---|
| `kb_empty_reason` | `no_verified_items`, `no_matching_items`, `kb_unavailable` | Only set when `kb_items` is empty. Do not invent notes. |
| `kb_items_from_user_input` | item ids | Items chosen because of a year built or cladding that is not in the records: entered by the user, or found by a web search (`user_inputs.chat_supplied_source`). Say "Based on what you told us" for the first and "Based on a web search (not checked by this service)" for the second. |
| `kb_includes_draft` | boolean | True only in development mode (`INCLUDE_DRAFT`, which `.github/deploy/targets.json` does not set). The page then marks draft notes as not yet checked. |
| `hazard_gaps` | `{layer, status, reason}` | Why a primary layer was not checked. |
| `price_used.basis` | `cv`, `user`, `none` | Say whether the price is the council valuation or the user's own figure. |
| `first_home_loan.eligibility` | `eligible`, `conditional` | Show `eligibility_note` when conditional. |
| `first_home_loan.full_premium_assumed_for` | bank ids | Ranges for these banks assume the full 1.2% premium; say so. |
| `disclaimer` | text | A fixed information-only notice. The page shows its own footer notice instead; any other client should show this text verbatim with the results. |

## First Home Loan note ids (`first_home_loan.note_ids`)

Short codes for explanatory notes on the First Home Loan scenario. Show the suggested text next to the lender figures.

| Note id | Meaning | Suggested display text |
|---|---|---|
| `fhl_passthrough_varies` | Kāinga Ora charges lenders a 1.2% premium, and each lender passes on a different share to the borrower; some shares are unknown | "The First Home Loan premium is 1.2% of the loan, but each lender passes on a different share of it. Where a lender's share is unknown, we assume the full 1.2% is added to your loan, so your actual repayment may be lower." Use `lmi_note` for the exact wording and `full_premium_assumed_for` for the banks affected. |

## Fields that are always present

| Field | Meaning | Presentation |
|---|---|---|
| `loan_term_years` | Loan term used for every repayment figure (30) | Say "over 30 years" from this value instead of hard-coding it. |
| `stress_add_pp` | Percentage points added to the rate for the stress figures (2.0) | Say "if rates rose by 2 percentage points". |
| `rates_stale_after_days` | Days after which `rates_snapshot_date` counts as stale (30) | Use it to word the `rates_snapshot_stale` message. |
| `default_rate_pct` | Median 1-year standard rate in the snapshot, 2 decimals; null with no rates | Pre-fill as the starting rate; say it is the median of the snapshot and can be changed. |
| `fhl_income_caps` | First Home Loan income caps: `single` and `multiple_or_dependants` | Quote when `fhl_income_over_cap` is raised; the `first_home_loan` scenario is not returned then. Same values as `first_home_loan.income_caps`. |

## Error responses

All 4xx and 5xx responses use `{"error": {"code", "message", "fields": [{"field", "message"}]}}`.

| Code | Status | Meaning |
|---|---|---|
| `validation_error` | 422 | Bad input (for example a negative deposit); `fields` names each field and the reason. A deposit above the price is not an error: see `deposit_exceeds_price`. |
| `not_found` | 404 | Resource missing (for example no rates snapshot). |
| `method_not_allowed` | 405 | Wrong HTTP method for the path. |
| `http_error` | other 4xx | Any other HTTP error raised by the framework. |
| `internal_error` | 500 | Unexpected failure. No details are exposed. |

## Build decade matching

`year_built_between` rules match on the property's decade. When the property context carries
`year_built.decade_range` (`[from, to]`, inclusive), the rule matches if that range overlaps
the rule's range; `[null, 1913]` means 1913 or earlier. An explicit `decade_range: null`
(mixed eras) counts as no decade, so a year supplied in chat is used instead. Without a
`decade_range`, a single year (or `1990s`) is used. A year the user gives, or one found by a web
search, may be a single year (`1997`) or a decade (`1990s`, matched as 1990 to 1999).
