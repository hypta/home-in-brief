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
| `summary` | `[{text, source_ids}]`. Gemini rewrites a fact list built by code into 3-5 sentences (see "Summary: fact list and rewrite"); rewrites that fail the checks are replaced by the facts' draft sentences, and every must-mention fact is always present (at most 5). With no loan (`price_missing`, `deposit_exceeds_price`, `cash_purchase_no_loan`) code appends one fixed sentence with `source_ids: []`. Sentences that use chat-supplied facts get the prefix "Based on what you told us, " from code |
| `next_steps` | `[{text, source_ids, kb_item}]`: `next_step_en` of selected KB items, verbatim. Items cited in the summary first, then a fixed program order (flood insurance whenever a primary flood layer intersects, other intersecting-hazard items, build era/cladding, RBNZ LVR/DTI, LIM, title, First Home Loan, the rest). Identical texts are merged (`kb_item` = first, `source_ids` = all). At most 6. No lending items when there is no loan |
| `not_found_notes` | `[{field, text, reason}]`: property fields and primary hazard layers with status `not_found` or `unavailable`. Fields that share the same wording and reason are merged into one note; `field` is then a comma-separated list (for example `cv,year_built,...`, `hazards.flood_plain,hazards.flood_prone_area`). Hazard notes end with "This does not mean there is no hazard." |
| `warnings` | `rules_output.warnings` without panel-level and internal codes (`lem_base_unverified:*`, `bank_skipped:*`, `kb_hash_mismatch:*`, `kb_verification_incomplete:*`, `hazard_status_*`, `hazard_priority_unknown:*`), plus `kb_includes_draft` when `rules_output.kb_includes_draft` is true. report-api adds no codes of its own; meanings are in CODES.md |
| `dropped` | Number of Gemini sentences blocked by the checks (their facts fall back to drafts) |

## Errors

Same shape as rules-api: `{"error": {"code", "message", "fields": [{"field", "message"}]}}`.

| Code | HTTP | Meaning |
|---|---|---|
| `validation_error` | 422 | Body missing a key, or a key is not a JSON object |
| `model_unavailable` | 503 | Gemini timed out, hit quota, returned an error, or `GEMINI_API_KEY` is not set |
| `model_output_invalid` | 502 | Gemini's answer did not match the response schema (after the retry) |
| `validation_failed` | 502 | Report: no sentence at all could be shown (cannot happen while the drafts pass); follow-up: see below |
| `kb_unavailable` | 503 | KB items or sources could not be read from rules-api |
| `rules_unavailable` | 503 | Follow-up only: the rules-api re-run with a supplied year or cladding failed |
| `deadline_exceeded` | 504 | Follow-up only: `REQUEST_DEADLINE_S` ran out between calls |
| `not_found`, `method_not_allowed` | 404, 405 | Unknown path or method |
| `internal_error` | 500 | Anything else; no details |

For `/v1/followup`, `validation_error` also covers a missing `question`, a bad `history` entry and an unknown
`refusal_mode`, and `validation_failed` means no answer sentence passed.

These codes are deliberately not in CODES.md (web's tests require page text for every code listed there).

## Summary: fact list and rewrite (`app/prompt.py`, `app/facts.py`, `app/validator.py`, `app/rewrite.py`)

1. **Fact list** (`prompt.fact_list`): code turns the three documents into facts `F1, F2, ...` in RUBRIC 1 order.
   Each fact has `text` (the draft sentence, values as placeholders such as `{{F1.flood_plain_pct}}`),
   `source_ids`, `slots` (name, value, kind, source, optional label / before / after context, strict),
   `topic`, `priority`, `user` (relies on chat-supplied facts) and `must` (a RUBRIC 8 must-mention; at most 5).
   Optional facts add lending and user-input meaning without any calculated value (household type used for the
   First Home Loan cap, a self-entered rate) and topics beyond the 5-sentence cap.
   A user fact that cites a leaky-building item the user's own input triggered (`kb_leaky_build_era`,
   `kb_monolithic_cladding` in `kb_items_from_user_input`) ranks right after the title, so it is always a
   must-mention with the prefix; with the council year unknown, a 1998 + monolithic input becomes one sentence
   naming both ("the year you gave and the monolithic cladding are both discussed in Settled.govt.nz's guidance
   on leaky buildings; public data could not confirm them"). The non-residential use sentence it pushes out is
   merged into the title sentence (without the title's KB clause when needed to stay within 25 words), so the
   summary stays at 5 sentences. `next_steps` keeps those triggered items within the 6 shown (`assemble.py`).
2. **Rewrite**: Gemini returns `{"summary": [{"text", "fact_ids"}]}`, 3-5 sentences, and may merge facts. It never
   sees a value: digits, years, amounts, title names and the council's land-use words are written only as
   placeholders. Code fills them in (`51.2%`, `$95,000`, `1990s`, `Freehold`).
3. **Checks** (`validator.check_rewrite`): fact ids must exist; `source_ids` come from the facts, never from
   Gemini; a placeholder must belong to a fact the sentence names; no digit outside a placeholder; no strict
   string value (title names, council land-use words) outside a placeholder unless the fact text itself says it;
   a placeholder must sit next to its words (its label is the nearest label; LVR threshold after "over", DTI
   multiple before "times"); user facts are not merged with other facts. Semantic guard (simple): the fact's
   qualifiers (`may`, `can`, `could not`, `only`, `limited`, `at least`, `generally`, `exempt`, `unknown`,
   `on individual properties`, `differs`, `same decade`, `assumes`, `within`, `above`, `if`) and keep phrases
   (e.g. "because your income", "it is exempt", "your loan") must be kept in the fact's order; terms (LVR, DTI,
   First Home Loan, body corporate, leaky) and publishers (Auckland Council, Settled.govt.nz, Reserve Bank,
   Kāinga Ora, LINZ, ...) kept; the number of not/no/never words unchanged; no added "says"/"states", causal
   link or publisher. Then the filled sentence runs through `check_sentence` as before (banned words, 25 words,
   URLs, numbers against the cited sources).
4. **Fallback** (`rewrite.compose`): a blocked sentence is replaced by the drafts of its facts (one sentence each,
   unless a passing rewrite already covers the fact); a must fact no sentence used is added as its draft; over 5,
   optional sentences go first. Code adds "Based on what you told us, " to sentences with a user fact.
   One retry when no rewrite passes. The debug record (eval only) counts rewrites, fallback drafts and added
   must facts; the response contract is unchanged.

**Limits of the semantic guard.** It is lexical. It cannot tell whether a rewrite that keeps every listed word
still means the same thing (a changed relation between kept words can pass), and it does not detect added
content beyond extra "says", causes and publishers. Follow-up answers are English only, so the validator's
Chinese-equivalent branches are not used there. Mock mode still returns the golden summaries.

## Follow-up (`app/followup.py`, `app/followup_prompt.py`)

The validated answer is English only (2026-10-10 decision). A question may be in any language and is answered, in
English; an answer from a Google Search (step 3) is the one exception and follows the language of the question.
Request: the three documents plus `question` (1-1000 characters) and `history` (at most 6 entries of
`{role: "user"|"assistant", text}`, text at most 2000 characters; web sends the last 3 turns). No server state.
Query `refusal_mode`: `exempt` (default) or `strict`.

Response 200: `{language, answer: [{text, source_ids}], out_of_scope, user_supplied, links, dropped, warnings}`,
plus `web` when an out-of-scope question was answered from a Google Search (step 3; `answer` is then `[]`).
`language` describes `answer` and is always `"en"`; the field is kept only for compatibility with the existing
contract (A reads it).
`warnings` is `[]` except `rules_rerun_unavailable` (mock mode answered from the input RulesOutput).
1 or more passing sentences gives 200 (at most 3); 0 gives 502 `validation_failed`.

Steps (INTERFACE_DRAFT 5 and 6.1):

1. Gemini call 1 (classify): out of scope, whether public web pages can answer it (`searchable`), a stated year built
   or cladding, suggested links. The question is understood in whatever language it is written in; there is no
   language detection.
2. `user_supplied`, checked by code: `year_built` is an integer 1800..this year or a decade such as `1990s`
   that appears in the question as written; `cladding` is one of `monolithic`, `weatherboard`, `brick`,
   `concrete`, `other` and a matching word must appear in the question. A question with a conditional or
   negation word (`if`, `not`, `如果`, `假如`, `不是`, ...) gives no value. Logged as `user_supplied_ignored`.
3. Out of scope, and classify says web pages can answer it (schools, commute, services nearby, published crime
   statistics; never advice, verdicts or predictions): a Gemini call with the Google Search tool
   (`followup.gemini_search`, prompt `followup_prompt.web_prompt` with the matched address, the question and the
   history), made once more, time allowing, when the first answer names no source. The response carries `web: {text, sources: [{title, url}], search_suggestions}`: the answer in the
   language of the question, the pages the search grounded it on (`title` is the site's domain, `url` is
   Google's redirect) and Google's search-suggestions HTML. This text is **not** checked by the validator and is
   not edited by code: the Gemini API terms for Grounding with Google Search require it to be shown as returned,
   together with its search suggestions, and do not allow storing it (so the tests use a hand-written answer).
   When neither answer names a source, the call fails, the mode is `strict` or `REPORT_MOCK` is on, the refusal
   below is returned instead. Only the route searches: `answer_followup` called directly (the eval script) does
   not, because an eval run stores what it gets.

   Otherwise the answer is one fixed English refusal chosen by code (never translated). `links` are
   the suggested PropertyContext link ids; if Gemini suggests none, code picks at most 2 by question type
   (valuation or sale: `homes`, `oneroof`, `council_valuation`; flooding: `council_flood_viewer`,
   `council_geomaps`; other hazards: `council_geomaps`, `council_flood_viewer`; anything else: `council_geomaps`),
   keeping only ids PropertyContext has (else its first link). `exempt`: the refusal has `source_ids: []` and skips
   the validator; `strict`: it goes through the validator, is dropped, and the response is 502.
4. Merge: values only add to `user_inputs.chat_supplied`. A field PropertyContext has with status `found`
   wins: the value is not used and is still returned in `user_supplied`. Code writes the first answer
   sentence comparing both values and cites the field's `source_id`, e.g. "You gave the year built as 1985; the council records the 1990s
   (1990–1999), and this answer uses the council's record." A user year inside the council decade is not a
   conflict: "The year you gave, 1998, falls within the council's recorded decade, the 1990s (1990–1999)." Cladding is handled the same way when
   the property has a value. **Program-sentence rule:** this sentence runs through the same checks as any
   answer sentence, except that the user's year is an allowed number for the cited field in this sentence
   only. Gemini is told the comparison is already stated and never to write the user's value.
   When a used value is new, report-api calls `POST {RULES_API_URL}/v1/rules` (ID token for `https://`) and
   uses the new RulesOutput; its `kb_items_from_user_input` are the items that depend on the user's value.
5. Gemini call 2 (answer) from the fact list (`prompt.followup_facts`: the summary facts with the same ids,
   plus each selected KB claim, found property values, fields and hazard layers that could not be checked, and
   the value the user just gave, cited only to the KB items it selected). After a rules re-run the list is
   rebuilt from the new RulesOutput. Gemini returns `{"answer": [{"text", "fact_ids"}], "links"}` with values as
   placeholders; each sentence goes through `validator.check_rewrite` (step 6 is its final check) and a blocked
   one falls back to its facts' English drafts (model order kept, at most 3). `question` and `history`
   (including assistant turns) are quoted as untrusted data and never enter the evidence. The one exception: a
   used `year_built` is an allowed number for the `kb_items_from_user_input` items only.
6. Every sentence goes through `validator.validate` with the English 25-word limit (`MAX_WORDS`), plus one rule:
   a sentence containing any Chinese, Japanese or Korean character (or CJK punctuation) is blocked
   (`non_english`) and takes the same path as any other blocked sentence (draft fallback). Domains follow the
   validator: only the host of a source the sentence cites passes ("Settled.govt.nz says ..." citing a Settled KB
   item). One retry when nothing passes and time allows.
7. Sentences citing a `kb_items_from_user_input` item get the prefix "Based on what you told us, " from code.
8. **Program facts** (`prompt._program_topics`, `followup.program_sentences`): a property field no public source
   gives us (cladding: no free source; last sale: no licensed source) has no `source_id` to cite, and a year or
   cladding the user entered earlier is not a property value. For these the fact list has program facts: "No free
   public source records the cladding, so it could not be confirmed; you can enter it in the form if you know it.",
   "... the cladding you gave is monolithic, which Settled.govt.nz discusses in its guidance on leaky buildings;
   public data could not confirm it." (cites `kb_monolithic_cladding`), "... the cladding you gave is brick; ..."
   (no source), "... the year built you gave is 1998, ...". Gemini only picks them by id; the answer shows the
   draft, never a rewrite. A safety net adds the program fact of a field the question names (cladding, year built,
   last sale) when Gemini picks none. Checks: `prompt.check_program` = `check_sentence` with the user's year
   allowed under the cited ids, and a sentence with no source exempt from the source-id rule only (as the
   `not_found_notes` and no-loan sentences are). Fields the council page could not be read for (floor area,
   local board, building site coverage) get ordinary cited facts ("Council records could not be read from our
   server, so the floor area is unknown.").

Time: one budget, `REQUEST_DEADLINE_S`; each Gemini call gets at most `GEMINI_TIMEOUT_S` and what is left;
the rules re-run at most 5 s.

Mock (`REPORT_MOCK=true`, no Gemini call, for front-end work without a key): code classifies the question.
Out-of-scope keywords (school, zone, commute, crime, neighbour, traffic, 學區, 通勤, 治安, ...; not when the
question names a hazard) give the English refusal and the link fallback (the Chinese keywords are only used to understand the question). A year
or cladding goes through the same checks, merge and rules re-run; if rules-api is unavailable the input
RulesOutput is used and `warnings` has `rules_rerun_unavailable`. The answer is the comparison sentence (if
any) plus the `claim_en` of each item the user's value selected, verbatim, validated and prefixed. Any other
question gets one fixed mock sentence citing a PropertyContext source.

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
