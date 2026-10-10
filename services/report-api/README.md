# report-api

The Gemini layer. web sends the three documents it fetched server-side (PropertyContext from
property-api, the user's inputs, RulesOutput from rules-api); report-api asks Gemini for a short
plain-language summary, checks every sentence against the sources, and builds the rest of the
report by code. Contract: `out/gemini_layer/INTERFACE_DRAFT.md` (team notes, not in git).

Private Cloud Run service; only web calls it.

## Endpoints

| Method | Path | Result |
|---|---|---|
| POST | `/v1/report` | Report (below) |
| POST | `/v1/followup?refusal_mode=exempt\|strict` | Follow-up answer (below) |
| GET | `/health` | `{"status": "ok"}`; calls nothing |

Request body: `{"property_context": {...}, "user_inputs": {...}, "rules_output": {...}}`. All three keys
are required JSON objects and are kept whole (`b_common.report_schemas.ReportRequest`).

Response 200:

| Field | Produced by |
|---|---|
| `schema_version` | `"0.1"` |
| `generated_at` | ISO 8601, NZ time |
| `model` | `GEMINI_MODEL` in use, or `mock` |
| `summary` | `[{text, source_ids}]`. Gemini writes 3-5 sentences from a topic briefing built by code; only sentences that pass the validator are returned (at most 5). With no loan (`price_missing`, `deposit_exceeds_price`, `cash_purchase_no_loan`) code appends one fixed sentence with `source_ids: []`. Sentences that use chat-supplied facts get the prefix "Based on what you told us, " from code |
| `next_steps` | `[{text, source_ids, kb_item}]`: `next_step_en` of selected KB items, verbatim. Items cited in the summary first, then a fixed program order (flood insurance whenever a primary flood layer intersects, other intersecting-hazard items, build era/cladding, RBNZ LVR/DTI, LIM, title, First Home Loan, the rest). Identical texts are merged (`kb_item` = first, `source_ids` = all). At most 6. No lending items when there is no loan |
| `not_found_notes` | `[{field, text, reason}]`: property fields and primary hazard layers with status `not_found` or `unavailable`. Fields that share the same wording and reason are merged into one note; `field` is then a comma-separated list (for example `cv,year_built,...`, `hazards.flood_plain,hazards.flood_prone_area`). Hazard notes end with "This does not mean there is no hazard." |
| `warnings` | `rules_output.warnings` without panel-level and internal codes (`lem_base_unverified:*`, `bank_skipped:*`, `kb_hash_mismatch:*`, `kb_verification_incomplete:*`, `hazard_status_*`, `hazard_priority_unknown:*`), plus `kb_includes_draft` when `rules_output.kb_includes_draft` is true. report-api adds no codes of its own; meanings are in CODES.md |
| `dropped` | Number of model sentences removed by the validator |

## Errors

Same shape as rules-api: `{"error": {"code", "message", "fields": [{"field", "message"}]}}`.

| Code | HTTP | Meaning |
|---|---|---|
| `validation_error` | 422 | Body missing a key, or a key is not a JSON object |
| `model_unavailable` | 503 | Gemini timed out, hit quota, returned an error, or `GEMINI_API_KEY` is not set |
| `model_output_invalid` | 502 | Gemini's answer did not match the response schema (after the retry) |
| `validation_failed` | 502 | No summary sentence passed the validator (after the retry) |
| `kb_unavailable` | 503 | KB items or sources could not be read from rules-api |
| `rules_unavailable` | 503 | Follow-up only: the rules-api re-run with a supplied year or cladding failed |
| `deadline_exceeded` | 504 | Follow-up only: `REQUEST_DEADLINE_S` ran out between calls |
| `not_found`, `method_not_allowed` | 404, 405 | Unknown path or method |
| `internal_error` | 500 | Anything else; no details |

For `/v1/followup`, `validation_error` also covers a missing `question`, a bad `history` entry and an unknown
`refusal_mode`, and `validation_failed` means no answer sentence passed.

These codes are deliberately not in CODES.md (web's tests require page text for every code listed there).

## Follow-up (`app/followup.py`, `app/followup_prompt.py`)

Request: the three documents plus `question` (1-1000 characters) and `history` (at most 6 entries of
`{role: "user"|"assistant", text}`, text at most 2000 characters; web sends the last 3 turns). No server state.
Query `refusal_mode`: `exempt` (default) or `strict`.

Response 200: `{language, answer: [{text, source_ids}], out_of_scope, user_supplied, links, dropped}`.
1 or more passing sentences gives 200 (at most 3); 0 gives 502 `validation_failed`.

Steps (INTERFACE_DRAFT 5 and 6.1):

1. Gemini call 1 (classify): language, out of scope, a stated year built or cladding, suggested links. The
   language is corrected by script (a question with Han characters is never `en`).
2. `user_supplied`, checked by code: `year_built` is an integer 1800..this year or a decade such as `1990s`
   that appears in the question as written; `cladding` is one of `monolithic`, `weatherboard`, `brick`,
   `concrete`, `other` and a matching word must appear in the question. A question with a conditional or
   negation word (`if`, `not`, `如果`, `假如`, `不是`, ...) gives no value. Logged as `user_supplied_ignored`.
3. Out of scope: the answer is one fixed English refusal chosen by code, translated by a Gemini call that
   sees only that sentence (English is kept if the translation has digits, URLs or is empty). `links` are
   the suggested PropertyContext link ids, or all of them. `exempt`: the refusal has `source_ids: []` and skips
   the validator; `strict`: it goes through the validator, is dropped, and the response is 502.
4. Merge: values only add to `user_inputs.chat_supplied`. A field PropertyContext has with status `found`
   wins: the value is not used, it is still returned in `user_supplied`, and the answer says the council
   data differs (code adds a fixed sentence citing the council source if the model's answer does not).
   When a used value is new, report-api calls `POST {RULES_API_URL}/v1/rules` (ID token for `https://`) and
   uses the new RulesOutput; its `kb_items_from_user_input` are the items that depend on the user's value.
5. Gemini call 2 (answer) from a case-data prompt; `question` and `history` (including assistant turns) are
   quoted as untrusted data and never enter the evidence. The one exception: a used `year_built` is an allowed
   number for the `kb_items_from_user_input` items only.
6. Every sentence goes through `validator.validate`, with these language-dependent additions: for CJK text
   (Chinese/Japanese/Korean language or script) the 25-word limit is replaced by 60 units (CJK characters
   plus Latin words and numbers), Chinese verdict and approximation words (安全, 建議, 應該, 必須, 可能, 大約, ...)
   and English verdict words glued to CJK text are dropped, `兩` and `百分之` count as number words, and `一` is
   masked before the validator's numeral check (it is part of ordinary words). In every language the
   publisher names Settled.govt.nz and unittitles.govt.nz are not treated as links; any other domain is.
   Digits next to CJK characters get a space (`1997年` -> `1997 年`) so the number is read whole. One retry
   when nothing passes and time allows.
7. Sentences citing a `kb_items_from_user_input` item get a prefix from code: `依你提供的資訊，` (zh-Hant),
   `根据你提供的信息，` (zh-Hans), "Based on what you told us, " (English); other languages are translated by
   Gemini, falling back to English.

Time: one budget, `REQUEST_DEADLINE_S`; each Gemini call gets at most `GEMINI_TIMEOUT_S` and what is left;
the rules re-run at most 5 s. Mock mode does not cover the follow-up yet.

```
curl -s 'localhost:8090/v1/followup?refusal_mode=exempt' -H 'content-type: application/json' \
  -d "$(jq '{property_context, user_inputs, rules_output} + {question: "這間是 1997 年蓋的，要注意什麼？", history: []}' \
        services/report-api/fixtures/inputs/case1.json)"
```

## Validator (`app/validator.py`)

A summary sentence is dropped when any rule fails (rule code in parentheses, logged server-side):

- no `source_ids` (`no_source_ids`); an id that is not a PropertyContext source, a rules-api source
  (`/v1/sources` or any `source_ids` in RulesOutput) or a selected KB item (`unknown_source_id`); a `kb_`
  id outside `kb_items` ∪ `kb_items_from_user_input` (`kb_not_selected`);
- a number that does not equal a field mapped to a cited id (`number_not_in_cited_sources`; map in
  INTERFACE_DRAFT 3.3: hazard `overlap_pct` and data year, property fields by `source_id`, valuation
  year, published bank rates, First Home Loan income caps, numbers inside a cited KB `claim_en`).
  Exact after removing `$`, `NZ$`, thousands separators and `%`; R1 rounding to the field's display
  precision (percent 1 dp, money whole dollars, rates 2 dp); R2 year of an ISO date; "1990s" reads as 1990;
- a computed value (loan amount, LVR, DTI, high-DTI threshold, repayments, stress figures, LMI amounts,
  deposit share) or 0, unless the number comes from a cited KB claim (`computed_value`);
- `k`/`m` abbreviations (`abbreviated_number`), number words (`number_word`);
- verdict, advice, prediction or approximation words (`banned_word`; RUBRIC 7; "will", "must", "should",
  "likely" are allowed when the cited KB claim contains them);
- URLs or HTML (`url_or_html`), field names or internal terms (`internal_jargon`);
- more than 25 words, not counting the code-added prefix (`too_long`);
- landslide content without a High (or Very High) class or without `kb_landslide_map_context` (`landslide_rule`);
- lending content when there is no loan (`lending_without_loan`).

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | none | Gemini API key (Secret Manager on Cloud Run). Never logged |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Same default as jobs/rates-refresh |
| `RULES_API_URL` | `http://localhost:8080` | KB items and sources; an ID token is added for `https://` |
| `KB_SOURCE` | `rules-api` | `local` reads `kb/seed` instead (tests, local runs) |
| `KB_DIR` | `kb/seed` | Seed directory for `KB_SOURCE=local` |
| `REPORT_MOCK` | false | `true`: no Gemini call (below) |
| `GEMINI_TIMEOUT_S` | 20 | Limit for one Gemini call |
| `REQUEST_DEADLINE_S` | 25 | Total budget; one retry (invalid JSON, 0 passing sentences, model error) only if the remaining time covers another attempt |

## Mock mode

`REPORT_MOCK=true` returns the Claude golden summary for the nine fixtures in `fixtures/inputs/`
(matched on `property_context.address.matched` and `user_inputs`), through the real validator (without
the 25-word limit, which postdates the goldens) and the real code-built parts. Other inputs get one
fixed mock sentence plus the code-built parts.

## Run and test

```
make test-report-api
REPORT_MOCK=true make local-report          # :8090, KB from kb/seed
curl -s localhost:8090/v1/report -H 'content-type: application/json' \
  -d "$(jq '{property_context, user_inputs, rules_output}' services/report-api/fixtures/inputs/case1.json)"
```

Evaluation against the golden cases and RUBRIC section 8: `scripts/eval_report_api.py` (writes to
`out/golden/eval/`).

## Fixtures

`fixtures/inputs/*.json`: the nine golden cases (Chinese description fields removed).
`fixtures/golden/{claude,codex}/*.report.json`: the two reference reports, used by the validator tests and mock mode.
