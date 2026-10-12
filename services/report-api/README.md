# report-api

The Gemini layer. web sends the three documents it fetched server-side (PropertyContext from
property-api, the user's inputs, RulesOutput from rules-api); report-api asks Gemini for a short
plain-language summary, checks every sentence against the sources, and builds the rest of the
report by code. Contract: the request and response models (`shared/b_common/report_schemas.py`,
`app/followup.py`) and the endpoint sections below.

Private Cloud Run service; only web calls it.

## Endpoints

| Method | Path | Result |
|---|---|---|
| POST | `/v1/report` | Report (below) |
| POST | `/v1/followup?refusal_mode=exempt\|strict` | Follow-up answer (below) |
| GET | `/health` | `{"status": "ok"}`; calls nothing |

Headers (both POST routes):

| Header | Direction | Meaning |
|---|---|---|
| `X-Report-Mode` | response | `gemini`: the response has Gemini content that passed the checks (a rewrite or a Search answer). It may come from the report cache, and it may be mixed with program drafts; it does not mean a fresh, healthy call was made in this request. `drafts`: only program drafts and fixed sentences. `mock`: `REPORT_MOCK` |
| `X-Report-Degraded-Reason` | response | Only when the response fell back to drafts after a refused or failed model call or for lack of time: `allowance_daily`, `allowance_event`, `disabled` (`protection/control.disabled` or `REPORT_DEGRADED`), `backend_error` (allowance backend unavailable, slow or denied), `prompt_too_large`, `model_error` (Gemini timeout, quota 429, 4xx/5xx, invalid JSON, no rewrite passing the checks, or no `GEMINI_API_KEY`), `deadline` (no time left for a call, before it or after its reservation). Absent on a normal drafts answer (e.g. the follow-up's fixed refusal) |
| `X-Request-Deadline-Ms` | request (from web) | Milliseconds web has left for this request. The request's one deadline is the smaller of this and `REQUEST_DEADLINE_S` (a larger or malformed value is ignored). web does not send it yet (A); without it the deadline is 25 s |

Logs: one JSON completion record per request (`app/obs.py`: request id, status, latency, model, call and Search counts,
error category, sentence counts, mode, `degraded_reason`; never the question, history, prompts or amounts). A failed
Gemini call logs its exception class, category, HTTP status and the provider's request id, never the exception's
message; tracebacks (ours and uvicorn's "Exception in ASGI application") keep only exception classes and
file:line:function (`obs.safe_traceback`).

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
| `not_found_notes` | `[{field, text, reason, fields, labels}]` (`fields` and `labels` added later, additive: `fields` is `field` split into codes, `labels` the display name of each, same order, for example `["Title type", "Year built"]`): property fields and primary hazard layers with status `not_found` or `unavailable`. Fields that share the same wording and reason are merged into one note; `field` is then a comma-separated list (for example `cv,year_built,...`, `hazards.flood_plain,hazards.flood_prone_area`). Hazard notes end with "This does not mean there is no hazard." |
| `warnings` | `rules_output.warnings` without panel-level and internal codes (`lem_base_unverified:*`, `bank_skipped:*`, `kb_hash_mismatch:*`, `kb_verification_incomplete:*`, `hazard_status_*`, `hazard_priority_unknown:*`), plus `kb_includes_draft` when `rules_output.kb_includes_draft` is true. report-api adds one code of its own, last: `generation_degraded` when protection refused the Gemini call (allowance used up, `REPORT_DEGRADED`, prompt too large; see "Abuse and cost protection"); the summary is then the checked drafts. Other meanings are in CODES.md |
| `dropped` | Number of Gemini sentences blocked by the checks (their facts fall back to drafts) |

## Errors

Same shape as rules-api: `{"error": {"code", "message", "fields": [{"field", "message"}]}}`.

| Code | HTTP | Meaning |
|---|---|---|
| `validation_error` | 422 | Body missing a key, or a key is not a JSON object |
| `model_unavailable` | 503 | Gemini failed (timeout, quota, error, no `GEMINI_API_KEY`) **and** no draft passed; otherwise the checked drafts answer with 200 (no retry after quota or a missing key) |
| `model_output_invalid` | 502 | Gemini's answer did not match the schema after the retry **and** no draft passed |
| `validation_failed` | 502 | Report: no sentence at all could be shown (cannot happen while the drafts pass); follow-up: see below |
| `kb_unavailable` | 503 | KB items or sources could not be read from rules-api |
| `rules_unavailable` | 503 | Follow-up only: the rules-api re-run with a supplied year or cladding failed |
| `deadline_exceeded` | 504 | Follow-up only: `REQUEST_DEADLINE_S` ran out between calls |
| `busy` | 503 | `GENERATION_SLOTS` requests already running in this instance; refused at once with `Retry-After: 5` and `Cache-Control: no-store` |
| `not_found`, `method_not_allowed` | 404, 405 | Unknown path or method |
| `internal_error` | 500 | Anything else; no details |

For `/v1/followup`, `validation_error` also covers a missing `question` (when there is no `search_field`), a bad `history` entry and an unknown
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
content beyond extra "says", causes and publishers. For Chinese, Japanese and Korean follow-up sentences it checks
only CJK equivalents of the qualifiers (Chinese, Japanese and Korean lists in `QUALIFIERS`), at least one negation
when the facts have one, English LVR/DTI/First Home Loan, the publishers, and the slot context words ("over",
"at least", "times") anywhere in the sentence; there is no order, keep-phrase, added-cause or added-"says" check.
Mock mode still returns the golden summaries.

## Follow-up (`app/followup.py`, `app/followup_prompt.py`)

**Language (2026-10-11 decision, replacing the 2026-10-10 English-only decision for follow-ups).** A follow-up is
answered in the language of the question (Chinese question, Chinese answer; Japanese question, Japanese answer),
with English terms kept as written (`followup_prompt.KEEP_ENGLISH`: LVR, DTI, LIM, First Home Loan, Cross Lease,
Unit Title, Freehold, Leasehold, NHC, LINZ, Settled.govt.nz, ...; plus layer names and values code fills in). The
report (`/v1/report`) stays English. An answer from a Google Search (step 3) also follows the question's language.
Request: the three documents plus `question` (1-1000 characters) and `history` (at most 6 entries of
`{role: "user"|"assistant", text}`, text at most 2000 characters; web sends the last 3 turns). No server state.
Query `refusal_mode`: `exempt` (default) or `strict`. Optional `search_field`: `"year_built"`, `"cladding"` or
`"last_sale"` (for a "Search the web for this" button; `question` may then be empty; see "Fields the records do not
have" below). web does not forward `search_field` or `language` yet (teammate A's work), so through the public page
only the question trigger searches. Optional `language` (BCP 47, e.g. `"zh-Hant"`, `"zh-TW"`, `"ja"`; default `en`): for a button request
**without** a question, the page's language, used for the search answer and the derived sentences; ignored when
a question is sent (its language is detected as usual). A malformed code is a 422 `validation_error`.

Response 200: `{language, answer: [{text, source_ids}], out_of_scope, user_supplied, links, dropped, warnings}`,
plus `web` when an out-of-scope question was answered from a Google Search (step 3; `answer` is then `[]`) or a
field was searched for (below), plus `user_supplied_source` when a `user_supplied` value came from a web search.
`language` is the detected language of the question (BCP 47: `en`, `zh-Hant`, `zh-Hans`, `ja`, `ko`, ...). A
sentence whose translation failed its checks (step 7) is in English even when `language` is not. When no model call
can be made for the translation (allowance refused, the request stopped, or no time left) there is no translation
call: a Chinese question gets fixed Chinese sentences written by code (a lead "目前無法產生完整回答，以下是紀錄中的事實。",
or "…請查看下方的原始資料連結。" when no fact has Chinese wording, plus the code-written Chinese of the comparison and
missing-field sentences; English sentences without a Chinese version are left out); any other language gets the
English sentences with an English lead, and `language` is then `"en"`. An English question is answered as usual.
`warnings` is `[]` except `rules_rerun_unavailable` (mock mode answered from the input RulesOutput),
`generation_degraded` (protection refused a model call: the answer is the program's facts or fixed sentences, with no
search, no extracted values and no translation; see "Abuse and cost protection") and `search_unavailable` (a Google
Search was called for and gave no answer: refused by the daily Search allowance, failed, out of time or no source;
the reply is then the fixed sentences; not added when `generation_degraded` applies).
1 or more passing sentences gives 200 (at most 3, plus the fixed lead sentence when no model call could be made); 0 gives 502 `validation_failed`.

Steps (INTERFACE_DRAFT 5 and 6.1):

1. Gemini call 1 (classify): the question's language, out of scope, whether public web pages can answer it
   (`searchable`), a stated year built or cladding, suggested links. The language is corrected by script
   (`followup.normalize_language`): Hangul gives `ko`, kana `ja`, Han `zh-Hant` or `zh-Hans` (a bare `zh` is decided
   by common simplified/traditional characters; `ja` is kept for Han-only Japanese); a question without CJK script
   is never zh/ja/ko; an invalid code gives `en`.
2. `user_supplied`, checked by code: `year_built` is an integer 1800..this year or a decade such as `1990s`
   that appears in the question as written; `cladding` is one of `monolithic`, `weatherboard`, `brick`,
   `concrete`, `other` and a matching word must appear in the question. A question with a conditional or
   negation word (`if`, `not`, `如果`, `假如`, `不是`, ...) gives no value, and so does a value the question only
   asks about (`followup.affirmed`: "Was it built in 1997?", `這房子是 1997 年蓋的嗎？`; "It was built in 1997, what
   does that mean?" states it). Logged as `user_supplied_ignored`.
3. Out of scope, and classify says web pages can answer it (schools, commute, services nearby, published crime
   statistics; never advice, verdicts or predictions): a Gemini call with the Google Search tool
   (`followup.gemini_search`, prompt `followup_prompt.web_prompt` with the matched address, the question and the
   history), made once more, time allowing, when the first answer names no source. Field searches (below) and this
   search share one limit: at most 2 search calls per request. The response carries `web: {text, sources: [{title, url}], search_suggestions}`: the answer in the
   language of the question, the pages the search grounded it on (`title` is the site's domain, `url` is
   Google's redirect) and Google's search-suggestions HTML. This text is **not** checked by the validator and is
   not edited by code: the Gemini API terms for Grounding with Google Search require it to be shown as returned,
   together with its search suggestions, and do not allow storing it (so the tests use a hand-written answer).
   When neither answer names a source, the call fails, the mode is `strict` or `REPORT_MOCK` is on, the refusal
   below is returned instead. Only the route searches: `answer_followup` called directly (the eval script) does
   not, because an eval run stores what it gets.

   Otherwise the answer is one fixed refusal chosen by code: English, Chinese written by code (`REFUSAL_ZH`,
   zh-Hans by character mapping), or for other languages a restricted translation of the English (step 7; English
   when it fails). `links` are
   the suggested PropertyContext link ids; if Gemini suggests none, code picks at most 2 by question type
   (valuation or sale: `homes`, `oneroof`, `council_valuation`; flooding: `council_flood_viewer`,
   `council_geomaps`; other hazards: `council_geomaps`, `council_flood_viewer`; anything else: `council_geomaps`),
   keeping only ids PropertyContext has (else its first link). `exempt`: the refusal has `source_ids: []` and skips
   the validator; `strict`: it goes through the validator, is dropped, and the response is 502.
   A question about the loan or the First Home Loan (and nothing else the report has facts for) when the fact list has
   no fact about it (no price, deposit or income entered, or a cash purchase) is not sent to the model: the answer is
   two fixed sentences, "This report does not have enough data to answer this question." and what is missing, with
   `source_ids: []` and `out_of_scope: false` (Chinese by code, other languages translated or English with `language: "en"`).
4. Merge: values only add to `user_inputs.chat_supplied`. A field PropertyContext has with status `found`
   wins: the value is not used and is still returned in `user_supplied`. Code writes the first answer
   sentence comparing both values and cites the field's `source_id`, e.g. "You gave the year built as 1985; the council records the 1990s
   (1990–1999), and this answer uses the council's record." / `你提供的建造年份是 1985 年；市議會資料記錄為 1990 年代
   （1990–1999）。以下說明以市議會資料為準。` A user year inside the council decade is not a
   conflict: "The year you gave, 1998, falls within the council's recorded decade, the 1990s (1990–1999)." /
   `你提供的 1998 年落在市議會資料記錄的 1990 年代（1990–1999）內。` Chinese is written by code; other languages
   translate the English (step 7). Cladding is handled the same way when
   the property has a value. **Program-sentence rule:** this sentence runs through the same checks as any
   answer sentence, except that the user's year is an allowed number for the cited field in this sentence
   only. Gemini is told the comparison is already stated and never to write the user's value.
   When a used value is new, report-api calls `POST {RULES_API_URL}/v1/rules` (ID token for `https://`) and
   uses the new RulesOutput; its `kb_items_from_user_input` are the items that depend on the user's value.
5. Gemini call 2 (answer) from the fact list (`prompt.followup_facts`: the summary facts with the same ids,
   plus each selected KB claim, found property values, fields and hazard layers that could not be checked, and
   the value the user just gave, cited only to the KB items it selected). After a rules re-run the list is
   rebuilt from the new RulesOutput. Gemini returns `{"answer": [{"text", "fact_ids"}], "links"}` with values as
   placeholders, written in the question's language; each sentence goes through `validator.check_rewrite` (step 6
   is its final check) and a blocked one falls back to its facts' English drafts (model order kept, at most 3;
   a draft may be longer than the 25-word limit, which is for rewrites: some verified claims are),
   which step 7 translates. `question` and `history`
   (including assistant turns) are quoted as untrusted data and never enter the evidence. The one exception: a
   used `year_built` is an allowed number for the `kb_items_from_user_input` items only.
   **No fact answers the question** (Ting 10/12): the model also returns `"answered"`, false when no fact says
   anything about what was asked (what a term or a map layer means, how two terms differ, a topic the facts do not
   hold). The reply is then never facts about something else: it is a Google Search answer as in step 3 (`web`,
   `answer: []`, `out_of_scope: false`), or, when there is no search (eval, `strict`, no answer), the one fixed
   sentence "This report does not have enough data to answer this question." (Chinese by code, other languages
   by restricted translation) with the fallback links. Not applied when the question states a year or cladding.
   A question about the loan or the First Home Loan with no loan fact in the list (no deposit or income entered) is
   searched for the same way, before any answer call; with no search answer it gets the two fixed sentences that
   say what is missing.
6. Every sentence goes through `followup.check_answer`: `validator.check_sentence` plus language rules.
   - The sentence must be in the detected language (`language_mismatch`): English has no CJK script; Chinese has
     Han and no kana/Hangul; Japanese has kana; Korean has Hangul. Other Latin-script languages are not told
     apart from English.
   - English: 25 words (`MAX_WORDS`); other Latin-script languages 35. Chinese/Japanese/Korean: 60/90/80 units
     (`CJK_MAX_UNITS_BY_LANG`: CJK characters plus Latin words and numbers) instead of the word limit.
   - CJK: verdict, advice and approximation words per language are `banned_word` (Chinese 安全, 建議, 應該, 必須,
     可能, 大約, 左右, 幾乎, 值得, ...; "may" must be 或許; Japanese 安全, おすすめ, べき, 必ず, 多分, ...; Korean 안전,
     추천, 해야, 반드시, 아마, ...), as are English verdict words glued to CJK text ("很safe"); 兩, 零, 百分之, ゼロ are
     `number_word`; `一` is masked before the numeral check (一般, 之一); ASCII next to CJK gets a space so the
     validator's `\b` checks see whole words, and a domain glued to CJK characters is caught again with ASCII
     boundaries; digits next to CJK characters get a space (`1997年` -> `1997 年`) and a filled-in decade reads
     `1990 年代` / `1990 년대`.
   - CJK: English words other than the kept terms, publisher and layer names, title names, the cladding table and
     this request's filled-in values are `mixed_language` ("leaky 建築" is blocked; "Flood Plains 圖層" passes).
   Domains follow the validator: only the host of a source the sentence cites passes. One retry when nothing passes
   and time allows.
7. **English program sentences in a non-English answer** (fact drafts after a blocked rewrite, program facts,
   comparison sentences for languages other than Chinese, the refusal): one restricted Gemini call
   (`followup_prompt.translate_prompt`) that sees only those sentences and the language code, never the question
   or history (prompt injection cannot steer it). Each result is used only when `followup.check_translation`
   passes: its numbers are exactly the English sentence's numbers; no URL, `@` or domain the English does not
   have; at most 1.5 times as long; the kept English terms and the values the fact shows (title name, cladding,
   land-use words) are copied as written; for CJK, a negation is kept when the English has one; and the step 6
   rules pass in the target language with the sentence's own allowances (a program fact's or comparison's user
   year). A rejected translation is sent once more with the numbers, terms and values to copy; a second failure
   leaves that whole sentence in English (never a mixed sentence). Why translation and not more hand-written
   Chinese: the drafts are the open set of every fact wording (summary facts, KB claims, program facts), so
   hand-written copies would drift from the English the validator and tests pin; Chinese is written by code only
   for the closed set of the comparison sentence, the refusal and the prefix.
8. Sentences citing a `kb_items_from_user_input` item (and program facts stating what the user entered) get a
   prefix from code (a value found by a web search gets the web prefix instead, see below) in the language the sentence ended up in: `依你提供的資訊，` (zh-Hant), `根据你提供的信息，`
   (zh-Hans), "Based on what you told us, " (English, and any English fallback sentence); other languages translate
   the English phrase in the same call (checked: no digits, URL or domain, at most 40 characters, right script),
   falling back to the English prefix.
9. **Program facts** (`prompt._program_topics`, `followup.program_sentences`): a property field no public source
   gives us (cladding: no free source; last sale: no licensed source) has no `source_id` to cite, and a year or
   cladding the user entered earlier is not a property value. For these the fact list has program facts: "No free
   public source records the cladding, so it could not be confirmed; you can enter it in the form if you know it.",
   "... the cladding you gave is monolithic, which Settled.govt.nz discusses in its guidance on leaky buildings;
   public data could not confirm it." (cites `kb_monolithic_cladding`), "... the cladding you gave is brick; ..."
   (no source), "... the year built you gave is 1998, ...". Gemini only picks them by id; the answer shows the
   draft (translated in step 7 for other languages), never a rewrite. A cladding or year given in this very
   question is stated by the user_said fact instead (no "you can enter it in the form" sentence). A safety net adds the program fact of a field the question names (cladding, year built,
   last sale) when Gemini picks none. Checks: `prompt.check_program` = `check_sentence` with the user's year
   allowed under the cited ids, and a sentence with no source exempt from the source-id rule only (as the
   `not_found_notes` and no-loan sentences are). Fields the council page could not be read for (floor area,
   local board, building site coverage) get ordinary cited facts ("Council records could not be read from our
   server, so the floor area is unknown.").

### Fields the records do not have: web search (team decision 10/11, plan C)

Ting reversed "no Gemini help for data the first-hand sources do not have" on 10/11 (the risk is recorded; the team
chose plan C). For `year_built`, `cladding` and `last_sale` (`app/web_fields.py`, `followup.field_search`):

- **Triggers.** (1) A question about one of them (`followup._FIELD_Q`: built, constructed, erected, how old,
  建造, 哪年蓋, ...; any wording that asks: a question mark or a question word) when PropertyContext has it as
  `not_found` / `unavailable` (or lacks it) and the user has not given it: stated in this question (a value the
  question only asks about, "Was it built in 1997?", does not count) or earlier in `chat_supplied`. Other years in
  the question ("It sold in 2019; when was it built?") do not stop the search. (2) The button: `search_field`
  searches for that one field with no classify call (`question` is optional and only sets the language; without
  one, `language`, default English). Both triggers search only a field the records do not have: a field
  PropertyContext has (`found`) is answered from the records (the program's facts), with no search, also for the
  button.
- **Search.** One Gemini call with the Google Search tool (`followup_prompt.field_search_prompt`: the matched
  address and the fields; it asks for plain sentences such as "built in 1987"), a second only when the first names
  no source; at most 2 search calls per request in all (with the out-of-scope search), each only with at least 11 s
  left (see Time). Sources are not filtered by domain (team decision). No answer, no source, a failed call,
  `strict` mode or no `web_search` (eval): the usual steps run (fixed program sentences and links).
- **Shown as returned.** `web: {text, sources, search_suggestions, fields, extracted}`: `text`, `sources` and
  `search_suggestions` as for step 3, never edited or mixed with anything (`text` is the model's text exactly, not
  even trimmed). `fields`: the fields searched for.
  `extracted`: what code took from `text`, e.g. `{"year_built": 1987, "cladding": "wood", "last_sale": {"date":
  "March 2019", "price": "$850,000"}}` (each key only when found).
- **Extraction** (`web_fields.extract`, by code first). The text is split into sentences (a decimal point, as in
  "$1.2 million", never ends one) and clauses (commas, but, whereas, 但, ...). A sentence about another property
  (neighbouring, next door, nearby, the street, the suburb, 隔壁, 附近, ..., or a street address that is not the
  property's) is skipped. A clause with a negation or an uncertainty word (not, unknown, may, estimated, 無法, 可能,
  ...) is skipped, and for the year also one with an approximation (probably, circa, c., approximately, about/around
  1987, before 1990, 大約, 約 1987 年, 左右, 初期, ...); the negation of another clause does not count ("built in 1987,
  but not renovated" gives 1987). Year: a year 1800..this year, or a decade (`1990s`, `1990 年代`), as a whole token
  (1990 is not "1990s"), whose nearest keyword in its clause is a build word (built, constructed, 建於, 興建, ...; not
  renovated, sold, 改建, 售出, ...). Cladding: each material word of `b_common.cladding` is bound to the part of the
  building it belongs to (the part named right after it, "brick exterior", "iron roof", else the nearest part named
  before it in its clause, "the fence is wood", else the clause's wall wording); only wall materials count ("The
  walls are brick and the fence is wood." gives brick), giving one of the 13 shared values. Last sale: one sale
  event (one sentence with a sale word, about this property) with its own date and price; several events: the one
  marked last/latest, else nothing. Ambiguity: two different values for a field, or for the year any other
  plausible year in the whole text (a year with no dated-event keyword, "1965 or 1972"), fills nothing. A field code
  found nothing for may be asked once of Gemini (`followup_prompt.extract_prompt`: the search text only, marked as
  untrusted page data and never instructions, no question): its value is kept only when its quote is written in
  the text and the quote's clause passes the same checks (affirmative, about this property, a build word and no
  other dated event for the year, a wall material for the cladding) and the text gives no other plausible year or
  wall material (`web_fields.check_model_values`). `web.text` is never changed by any of this.
- **Merge.** `year_built` and `cladding` go into `user_supplied` with `user_supplied_source: {"year_built":
  "web_search", ...}` (one entry per `user_supplied` key, `"user"` for values from the question; the key is absent
  when no value came from a search). `last_sale` never goes into `user_supplied` (rules-api has no such input); it
  is only in `web.extracted`. Order: council data > the user's own values > search values; values only add, never
  clear. A search value never fills a field the records have (status `found`) and never replaces a value the user
  gave (in this question or in `chat_supplied`); such values stay in `web.extracted` only, so `user_supplied` never
  holds a search value that conflicts with the council's. (A code comparison of a value found online with a found
  record, "Based on a web search (not checked by this service), web pages give the year built as 1985; the council
  records ...", is kept for values that reach the follow-up some other way.) A new value
  re-runs rules-api with `user_inputs.chat_supplied_source` added (rules-api ignores unknown keys), and the answer
  comes from the new facts ("The year built found online is 1987."). The fixed program sentence of a searched field
  is left out next to the search answer. The button with nothing to fill in (last sale, ambiguous, none found), or
  less than 11 s left, or an answer step that fails: `answer: []` and `web` only.
- **Several owners: shown, not filled in** (user decision 10/11; `followup.multi_unit_kind`). When `title_type` is
  Unit Title or Cross Lease (whitespace-normalised and case-folded, like rules-api's `title_type` match), or its
  `reason` is `multiple_title_types` (several title types at the address, `value` null), a search answer cannot say
  which unit or share a year or a price belongs to (it may describe another unit or the whole building). The search
  runs as usual and `web` (`text`, `sources`, `search_suggestions`, `extracted`) is returned as always, but a
  `year_built` / `cladding` value is **not** put in `user_supplied`, `user_supplied_source` is absent and rules-api
  is not called. `web.autofill_skipped: "multi_unit_title"` is added (only when a value was held back; the key is
  absent otherwise, so old clients are unaffected). `answer` holds three code-written sentences: what was found
  (with the web prefix), why it was not added to the form ("This is a unit title property, so the result may
  describe another unit or the whole building."; cross lease and multiple-title wording differ), and that the user
  may enter it if it matches their unit (home, property). Chinese is written by code, other languages use the
  restricted translation (English when a check fails); every sentence passes the validator and the length limit.
  Freehold, Leasehold and all other cases fill the form as above. The same in `REPORT_MOCK` mode.
- **Prefix.** A sentence that depends on a value found online gets "Based on a web search (not checked by this
  service), " / `依網路搜尋結果（未經本系統核對），` / `依网络搜索结果（未经本系统核对），`; other languages translate the
  English phrase (at most 70 characters). Each fact records the chat-supplied fields it depends on (`Fact.fields`:
  the user-year, user-cladding, user-said and program facts, KB claims selected by the user's input); a sentence
  depends on the fields of its facts plus those of the user-input KB items it cites (`build_era` -> year built,
  `cladding` -> cladding), and, when neither is known, on every chat-supplied year/cladding. If **any** of them came
  from a web search the sentence gets the web prefix, also when it mixes a search value with the user's value;
  otherwise "Based on what you told us, ". A council comparison that involves a search value also gets the web
  prefix (`prompt.from_web`).
- **`/v1/report`.** report-api reads the source from `user_inputs.chat_supplied_source: {"year_built":
  "web_search"}` next to `chat_supplied` (a missing or malformed value, or any value other than `"web_search"`,
  means `"user"`). The summary then says "found online" instead of "you gave" and uses the web prefix by the same
  rule (`prompt.chat_sources`, `prompt.from_web`). web keeps the source with each form value (`"web_search"` from
  the follow-up response until the visitor changes the value) and sends it as `user_inputs.chat_supplied_source` on
  the report recalculation and later follow-ups.
- **The last-sale button on a property with several owners** (Ting 10/12): nothing is ever filled in for a sale, but
  the result may be another unit's, so `answer` holds the one sentence that says so ("This is a unit title property,
  so the result may describe another unit or the whole building."); `autofill_skipped` stays absent.
- **Nothing to fill in and no fact to add** (Ting 10/12): beside a search answer, neither the program sentence nor
  the "could not be checked from public records" fact of a searched field is said, and when the model finds no fact
  that answers the rest of the question the reply is the search answer alone (`answer: []`).
- **Mock** (`REPORT_MOCK=true`): no search; `web` is `followup.MOCK_WEB` (text starting "MOCK web search answer",
  source `example.com`), and extraction by code gives `year_built: 1985`, `cladding: "wood"`, `last_sale: {date:
  "March 2019", price: "$850,000"}` (only the fields asked for).

Time (`gemini.Budget`, used by `/v1/report` and `/v1/followup`): one deadline per request, set when the route is
entered (`gemini.request_budget`): `REQUEST_DEADLINE_S` (25 s; web waits 30 s), or less when web sends
`X-Request-Deadline-Ms`. The KB read, the wait for an identical report in flight (at most 12 s, never more than is
left; a duplicate that gives up generates with what is left, never a fresh budget), each allowance reservation and
every call count against it. After a reservation the time left is checked again and the call's timeout recomputed;
under 11 s the reserved call is kept (not refunded), nothing is sent and the drafts answer (`deadline`). Every Gemini call (summary and its retry, classify, answer and its retry, translations,
extraction, field and out-of-scope searches) is made only when at least 11 s are left (`gemini.ADMIT_S`) and is sent
with a deadline of at least 10 s (`gemini.MIN_CALL_S`; the API refuses shorter ones with 400 "Minimum allowed
deadline is 10s"), at most `GEMINI_TIMEOUT_S` and 1 s less than what is left, so no call runs past the deadline.
At most 2 search calls per request (`gemini.MAX_SEARCHES`). The rules re-run gets at most 5 s, the KB read at most
5 s. Without time for a call the answer is code's: the summary's must-mention drafts, the follow-up's program
sentences (or must-mention drafts), English instead of a translation, the fixed refusal instead of a search.

Mock (`REPORT_MOCK=true`, no Gemini call, for front-end work without a key): code classifies the question.
Out-of-scope keywords (school, zone, commute, crime, neighbour, traffic, 學區, 通勤, 治安, ...; not when the
question names a hazard) give the refusal (Chinese for a Chinese question, else English) and the link fallback.
Mock mode never translates: other languages get English sentences. A year
or cladding goes through the same checks, merge and rules re-run; if rules-api is unavailable the input
RulesOutput is used and `warnings` has `rules_rerun_unavailable`. The answer is the comparison sentence (if
any; Chinese for a Chinese question) plus the `claim_en` of each item the user's value selected, verbatim (English,
with the English prefix), validated and prefixed. Any other question gets one fixed mock sentence (Chinese for a
Chinese question) citing a PropertyContext source.

**Limits of the language checks.** Meaning in a non-English sentence is checked only lexically (step 6 and the
CJK branch of the semantic guard above); keep phrases, order, added causes and added "says" are not checked for
CJK, and for Latin-script languages other than English the English guard runs as is, so most rewrites with
qualifiers or negations are blocked and answered with translated drafts. A translation is checked for numbers,
URLs, length, kept terms and values, one negation and the validator rules, not for meaning; dropped qualifiers
("may", "only") in a translation are not detected. zh-Hant vs zh-Hans is not checked in model text.

```
curl -s 'localhost:8090/v1/followup?refusal_mode=exempt' -H 'content-type: application/json' \
  -d "$(jq '{property_context, user_inputs, rules_output} + {question: "這間是 1997 年蓋的，要注意什麼？", history: []}' \
        services/report-api/fixtures/inputs/case1.json)"
# the "Search the web for this" button next to a field the records do not have
curl -s 'localhost:8090/v1/followup' -H 'content-type: application/json' \
  -d "$(jq '{property_context, user_inputs, rules_output} + {search_field: "year_built"}' \
        services/report-api/fixtures/inputs/case1.json)"
```

## Validator (`app/validator.py`)

PropertyContext `sources[].accessed_at` may be `null` (property-api sets a date only for a source it read); report-api
never reads it, and a null leaves sources, evidence, facts and both routes unchanged (`tests/test_followup_multilang.py`).

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
| `REQUEST_DEADLINE_S` | 25 | Total budget; a Gemini call only with at least 11 s left, sent with at least 10 s; one retry (invalid JSON, 0 passing sentences, model error) only if the remaining time covers another attempt; no time for a call: drafts |
| `PROTECTION_BACKEND`, `GEMINI_DAILY_CALLS` (100), `SEARCH_DAILY_CALLS` (30), `GEMINI_EVENT_CALLS` (3000), `GENERATION_SLOTS` (2), `GEMINI_MAX_OUTPUT_TOKENS` (2048), `GEMINI_INPUT_MAX_BYTES` (32000), `REPORT_DEGRADED` (false), `PROTECTION_TIMEOUT_S` (1), `REPORT_CACHE_SIZE` (100), `REPORT_CACHE_TTL_S` (600) | | See "Abuse and cost protection" |

## Abuse and cost protection (Codex F03, `app/protection.py`)

- **Generation slots**: at most `GENERATION_SLOTS` /v1/report and /v1/followup requests run at once per process; the
  next is refused at once with 503 `busy` (`Retry-After: 5`), no queue. `/health` takes no slot.
- **Allowance**: each Gemini SDK call (`gemini.gemini_generate`, `followup.gemini_json`, `followup.gemini_search`;
  every classify, answer, retry, translation, extraction and Search goes through one of them) first reserves one call
  in a Firestore transaction (`protection/control` + `protection/daily-<UTC date>`; DEPLOY.md section 8d). Refused:
  daily (`GEMINI_DAILY_CALLS`) or total (`GEMINI_EVENT_CALLS`) allowance used up, `protection/control.disabled`,
  Firestore failure or a transaction slower than `PROTECTION_TIMEOUT_S`. Refused calls are never sent; failed calls
  stay counted. A Search call also counts against `SEARCH_DAILY_CALLS`; when only that is used up, only the search is
  skipped (no warning).
- **Degraded answers**: a refused call (or `REPORT_DEGRADED=true`, or a prompt over `GEMINI_INPUT_MAX_BYTES`) is not
  retried, and it stops the request (below). Degradation can be partial: what an earlier call of the same request
  already produced stays. The report summary is the checked drafts (`model: "code-draft"` when no sentence is a Gemini
  rewrite; the Gemini model name when at least one is). A follow-up keeps, for example, a Search answer or a
  classification made before the refusal, and makes no further search, extraction or translation after it; with
  nothing from Gemini it is the program's facts or fixed sentences (see the follow-up's `language` above). Both are
  200 with the warning `generation_degraded`; `X-Report-Mode` is `drafts` only when no accepted Gemini content is
  left, and then `X-Report-Degraded-Reason` says why.
- **Request stop**: provider quota (429), a configuration error (no key), an allowance refusal (any reason except
  the Search allowance) or no time after a reservation stops the whole request: every later SDK wrapper refuses before
  reserving (`gemini.check_stopped`). A used-up Search allowance only skips searches; the answer is still generated.
- **Allowance transaction**: `PROTECTION_TIMEOUT_S`, never more than the request has left, bounds its whole life
  (begin, reads, commit, rollback get only what is left of it, no RPC retry). The caller stops waiting at the limit and
  the next reservation may start at once; at most 2 run per process (a third is refused at once, `backend_error`).
- **Caps per call**: `max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS`; one transport attempt (google-genai 2.28.0 retries
  only with `retry_options`; it is set to `attempts=1`).
- **Report cache** (`app/cache.py`): /v1/report only, per process, LRU `REPORT_CACHE_SIZE` (100) entries for
  `REPORT_CACHE_TTL_S` (600 s; either 0 turns it off). Key: SHA-256 of the three inputs as canonical JSON plus the
  model, `cache.CACHE_VERSION` (bump it when prompts, facts or checks change) and a fingerprint of the KB items and
  sources read for the request (a KB change misses). Left out of the key: only `rules_output.generated_at`,
  `property_context.generated_at` and `property_context.sources[].accessed_at` (when an input was computed; never
  read by report-api), so the same inputs computed in another second hit; rates dates, warnings, amounts, sources and
  array order stay in. Stored: reports with `X-Report-Mode: gemini` only (never drafts, mock, degraded or errors), never
  when `user_inputs.chat_supplied_source` has a web-search value, and never when `chat_supplied` has values whose
  source is missing from `chat_supplied_source` or does not match its keys (provenance unknown). Identical requests in flight wait for the first (single flight; no slot, no call). A hit
  keeps its `generated_at`. Follow-ups are never cached.
- Logs: `degraded_reason` (as `X-Report-Degraded-Reason`), `allowance` (`ok`, `daily_exhausted`, `event_exhausted`, `search_exhausted`, `disabled`, `backend_error`,
  `degraded`, `prompt_too_large`), `busy` and `cache` (`none`, `miss`, `hit`) in the completion record.
- Tests: `tests/test_protection.py`, `tests/test_cache.py` (sockets blocked; the Firestore transactions also run against the emulator when
  `FIRESTORE_EMULATOR_HOST` is set, e.g. after `make up`).

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
