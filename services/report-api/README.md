# report-api

The Gemini layer. web sends the three documents it fetched server-side (PropertyContext from
property-api, the user's inputs, RulesOutput from rules-api); report-api asks Gemini to reword a short
plain-language summary from facts built by code, checks each summary sentence against the sources it
cites (a sentence that fails is replaced by the program's own wording), and builds the rest of the
report by code. Not checked this way: follow-up answers from Google Search, which are shown as returned,
and values such a search fills in, which are marked as found by a web search and not checked. The checks
compare wording and figures; they cannot judge everything a sentence means (see the limits below).
Contract: the request and response models (`shared/b_common/report_schemas.py`, `app/followup.py`) and
the endpoint sections below.

In this file "report" means the JSON response of `/v1/report`. The page calls what it shows for one
address the results, and the few sentences at the top of them the summary.

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
| `X-Report-Mode` | response | `gemini`: the response has Gemini wording: at least one rewrite that passed the checks, or a Google Search answer (shown as returned, not checked sentence by sentence). It may come from the report cache, and it may be mixed with program drafts; it does not mean a fresh, healthy call was made in this request. `drafts`: no rewrite and no Search answer: the program's drafts and fixed sentences (in a non-English follow-up, possibly Gemini's checked translations of them). `mock`: `REPORT_MOCK` |
| `X-Report-Degraded-Reason` | response | Only when the response fell back to drafts after a refused or failed model call or for lack of time: `allowance_daily`, `allowance_event`, `disabled` (`protection/control.disabled` or `REPORT_DEGRADED`), `backend_error` (allowance backend unavailable, slow or denied), `prompt_too_large`, `model_error` (Gemini timeout, quota 429, 4xx/5xx, invalid JSON, no rewrite passing the checks, or no `GEMINI_API_KEY`), `deadline` (no time left for a call, before it or after its reservation). Absent on a normal drafts answer (e.g. the follow-up's fixed refusal) |
| `X-Request-Deadline-Ms` | request (from web) | Milliseconds web has left for this request. The request's one deadline is the smaller of this and `REQUEST_DEADLINE_S` (a larger or malformed value is ignored). web sends it on every call (what is left of its 30 s limit after it has read the PropertyContext and the rules); a caller that sends none gets 25 s |
| `Cache-Control` | response | `no-store` on every 200 response and on 503 `busy`; web passes it on to the browser |

Logs: one JSON completion record per request (`app/obs.py`: request id, status, latency, model, call and Search counts,
error category, sentence counts, mode, `degraded_reason`; never the question, history, prompts or amounts). A failed
Gemini call logs its exception class, category, HTTP status and the provider's request id, never the exception's
message; tracebacks (ours and uvicorn's "Exception in ASGI application") keep only exception classes and
file:line:function (`obs.safe_traceback`).

Request body: `{"property_context": {...}, "user_inputs": {...}, "rules_output": {...}}`. All three keys
are required JSON objects and are kept whole (`b_common.report_schemas.ReportRequest`). PropertyContext
`sources[].accessed_at` may be `null` (property-api sets a date only for a source it read); report-api never reads
it, and a null leaves sources, evidence, facts and both routes unchanged (`tests/test_followup_multilang.py`).

Response 200:

| Field | Produced by |
|---|---|
| `schema_version` | `"0.1"` |
| `generated_at` | ISO 8601, NZ time |
| `model` | `GEMINI_MODEL` in use when at least one summary sentence is a Gemini rewrite that passed the checks; `code-draft` when every sentence is the program's own draft; `mock` under `REPORT_MOCK` |
| `summary` | `[{text, source_ids}]`. Gemini rewrites a fact list built by code into 3-5 sentences (see "Summary: fact list and rewrite"); rewrites that fail the checks are replaced by the facts' draft sentences, and every must-mention fact is in the summary, as its draft when no rewrite covers it (at most 5 sentences, plus the fixed no-loan sentence; a draft that failed its own check would be left out, and the tests keep the drafts passing). With no loan (`price_missing`, `deposit_exceeds_price`, `cash_purchase_no_loan`) code appends one fixed sentence with `source_ids: []`. Sentences that use chat-supplied facts get the prefix "Based on what you told us, " from code |
| `next_steps` | `[{text, source_ids, kb_item}]`: `next_step_en` of selected KB items, verbatim. Items cited in the summary first, then a fixed program order (flood insurance whenever a primary flood layer intersects, other intersecting-hazard items, build era/cladding, RBNZ LVR/DTI, LIM, title, other lending items, the rest, the auction item last). Identical texts are merged (`kb_item` = first, `source_ids` = all); First Home Loan steps that say nearly the same thing are merged into the more complete one (`kb_item` follows the text kept). At most 6; flood insurance and a leaky-building item the user's input triggered are kept inside the 6. No lending items when there is no loan |
| `not_found_notes` | `[{field, text, reason, fields, labels}]` (`fields` and `labels` added later, additive: `fields` is `field` split into codes, `labels` the display name of each, same order, for example `["Title type", "Year built"]`): property fields and primary hazard layers with status `not_found` or `unavailable`. Fields that share the same wording and reason are merged into one note; `field` is then a comma-separated list (for example `cv,year_built,...`, `hazards.flood_plain,hazards.flood_prone_area`). Hazard notes end with "This does not mean there is no hazard." |
| `warnings` | `rules_output.warnings` without panel-level and internal codes (`lem_base_unverified:*`, `bank_skipped:*`, `kb_hash_mismatch:*`, `kb_verification_incomplete:*`, `hazard_status_*`, `hazard_priority_unknown:*`), plus `kb_includes_draft` when `rules_output.kb_includes_draft` is true. report-api adds one code of its own, last: `generation_degraded` when a Gemini call was refused before it was sent (daily or total allowance used up, `protection/control.disabled`, `REPORT_DEGRADED`, a prompt over `GEMINI_INPUT_MAX_BYTES`, the allowance store unavailable or slow, or no time left after the call was reserved; see "Abuse and cost protection"); the summary is then the checked drafts. It is not added when Gemini itself fails (timeout, 429, 4xx/5xx, invalid JSON, no key) or when no rewrite passes the checks: the summary is the checked drafts then too, and `model: "code-draft"` says so. Other meanings are in CODES.md |
| `dropped` | Number of Gemini sentences blocked by the checks (their facts fall back to drafts) |

## Errors

Same shape as rules-api: `{"error": {"code", "message", "fields": [{"field", "message"}]}}`.

| Code | HTTP | Meaning |
|---|---|---|
| `validation_error` | 422 | Body missing a key, or a key is not a JSON object |
| `model_unavailable` | 503 | Gemini failed (timeout, quota, error, no `GEMINI_API_KEY`) **and** no draft passed; otherwise the checked drafts answer with 200 (no retry after quota or a missing key) |
| `model_output_invalid` | 502 | Gemini's answer did not match the schema after the retry **and** no draft passed |
| `validation_failed` | 502 | Report: no sentence at all could be shown (not expected: the drafts are tested to pass); follow-up: see below |
| `kb_unavailable` | 503 | KB items or sources could not be read from rules-api |
| `rules_unavailable` | 503 | Follow-up only: the rules-api re-run with a supplied year or cladding failed |
| `deadline_exceeded` | 504 | Follow-up only: no sentence could be shown and fewer than 11 s of the request's deadline were left for another call |
| `busy` | 503 | `GENERATION_SLOTS` requests already running in this instance; refused at once with `Retry-After: 5` and `Cache-Control: no-store` |
| `not_found`, `method_not_allowed` | 404, 405 | Unknown path or method |
| `http_error` | other 4xx | Any other HTTP error raised by the framework |
| `internal_error` | 500 | Anything else; no details |

For `/v1/followup`, `validation_error` also covers a missing `question` (when there is no `search_field`), a bad `history` entry and an unknown
`refusal_mode`, and `validation_failed` means no answer sentence passed.

The codes only report-api emits (`model_unavailable`, `model_output_invalid`, `validation_failed`,
`rules_unavailable`, `deadline_exceeded`, `busy`) are listed here and not in CODES.md's warning tables (web's tests
require page text for every code in those tables).

## Summary: fact list and rewrite (`app/prompt.py`, `app/facts.py`, `app/validator.py`, `app/rewrite.py`)

1. **Fact list** (`prompt.fact_list`): code turns the three documents into facts `F1, F2, ...` in a fixed display
   order (hazards, lending, First Home Loan, title, non-residential use, build era, not checked).
   Each fact has `text` (the draft sentence, values as placeholders such as `{{F1.flood_plain_pct}}`),
   `source_ids`, `slots` (name, value, kind, source, optional label / before / after context, strict),
   `topic`, `priority`, `user` (relies on chat-supplied facts) and `must` (a must-mention, kept in the summary; at
   most 5).
   Optional facts add lending and user-input meaning without any calculated value (household type used for the
   First Home Loan cap, a self-entered rate) and topics beyond the 5-sentence cap.
   A user fact that cites a leaky-building item the user's own input triggered (`kb_leaky_build_era`,
   `kb_monolithic_cladding` in `kb_items_from_user_input`) ranks right after the title, so it is a must-mention
   with the prefix whenever it is among the five highest-ranked topics (a second hazard topic and a second lending
   topic rank above the title and can push both out); with the council year unknown, a 1998 + monolithic input
   becomes one sentence naming both ("the year you gave, 1998, and the monolithic cladding are both discussed in
   Settled.govt.nz's guidance on leaky buildings; public data could not confirm them"). The summary repeats only
   a year the user typed (`chat_supplied_source` absent or `user`): it is the fact's `user_year` slot, written
   right after "you gave," (a decade as "the 1990s"), so Gemini never sees the number, a rewrite must keep "you
   gave" and the placeholder in place, and `validator.with_user_year` allows that slot's value for that fact
   only (any other number is still refused). A year found by a web search is never written in the summary
   ("the year found online could not be checked against council records"). The non-residential use
   sentence it pushes out is merged into the title sentence (without the title's KB clause when needed to stay
   within 24 words, one under the limit), so the summary stays at 5 sentences. `next_steps` keeps those triggered
   items within the 6 shown (`assemble.py`).
2. **Rewrite**: Gemini returns `{"summary": [{"text", "fact_ids"}]}`, 3-5 sentences, and may merge facts. It never
   sees a value: digits, years, amounts, title names and the council's land-use words are written only as
   placeholders. Code fills them in (`51.2%`, `$95,000`, `1990s`, `Freehold`).
3. **Checks** (`validator.check_rewrite`): fact ids must exist; `source_ids` come from the facts, never from
   Gemini; a placeholder must belong to a fact the sentence names; no digit outside a placeholder; no strict
   string value (title names, council land-use words) outside a placeholder unless the fact text itself says it;
   a placeholder must sit next to its words (its label is the nearest label; LVR threshold after "over", DTI
   multiple before "times"); user facts are not merged with other facts. Semantic guard (simple): the fact's
   qualifiers (`may`, `can`, `could not`, `only`, `limited`, `limits`, `at least`, `generally`, `exempt`, `unknown`,
   `on individual properties`, `differs`, `same decade`, `assumes`, `within`, `above`, `if`) and keep phrases
   (e.g. "because your income", "it is exempt", "your loan") must be kept in the fact's order; terms (LVR, DTI,
   First Home Loan, body corporate, leaky) and publishers (Auckland Council, Settled.govt.nz, Reserve Bank,
   Kāinga Ora, LINZ, ...) kept; the number of not/no/never words unchanged; no added "says"/"states", causal
   link or publisher. Then the filled sentence runs through `check_sentence` as before (banned words, 25 words,
   URLs, numbers against the cited sources).
4. **Fallback** (`rewrite.compose`): a blocked sentence is replaced by the drafts of its facts (one sentence each,
   unless a passing rewrite already covers the fact); a must fact no sentence used is added as its draft; over 5,
   optional sentences go first. Code adds "Based on what you told us, " to sentences with a user fact.
   One retry when no rewrite passes. The debug record (eval runs and the completion log) counts rewrites, fallback
   drafts and added must facts; the response contract is unchanged.

**Limits of the semantic guard.** It is lexical. It cannot tell whether a rewrite that keeps every listed word
still means the same thing (a changed relation between kept words can pass), and it does not detect added
content beyond extra "says", causes and publishers. For Chinese, Japanese and Korean follow-up sentences it checks
only CJK equivalents of the qualifiers (Chinese, Japanese and Korean lists in `QUALIFIERS`), at least one negation
when the facts have one, English LVR/DTI/First Home Loan, the publishers, and the slot context words ("over",
"at least", "times") anywhere in the sentence; there is no order, keep-phrase, added-cause or added-"says" check.
Mock mode returns the facts' drafts (see Mock mode).

## Follow-up (`app/followup.py`, `app/followup_prompt.py`)

**Language.** A follow-up is answered in the language of the question (Chinese question, Chinese answer; Japanese
question, Japanese answer),
with English terms kept as written (`followup_prompt.KEEP_ENGLISH`: LVR, DTI, LIM, First Home Loan, Cross Lease,
Unit Title, Freehold, Leasehold, NHC, LINZ, Settled.govt.nz, ...; plus layer names and values code fills in). The
report (`/v1/report`) stays English. An answer from a Google Search (step 3) also follows the question's language.
Request: the three documents plus `question` (up to 1000 characters; empty only with `search_field`) and `history`
(at most 6 entries of `{role: "user"|"assistant", text}`, text at most 2000 characters; web sends the last 3
turns). No server state.
Query `refusal_mode`: `exempt` (default) or `strict`. Optional `search_field`: `"year_built"`, `"cladding"` or
`"last_sale"` (for the page's "Search the web" button; `question` may then be empty; see "Fields the records do not
have" below). web forwards `search_field` and `language` as the page sends them: the button beside a year built,
cladding or last sale the records lack sends `{search_field, language}` and no question. Optional `language`
(BCP 47, e.g. `"zh-Hant"`, `"zh-TW"`, `"ja"`; default `en`): for a button request **without** a question, the
language to answer in (the page sends the language of the last answer in the conversation), used for the search
answer and the derived sentences; ignored when a question is sent (its language is detected as usual). A malformed
code is a 422 `validation_error`.

Response 200: `{language, answer: [{text, source_ids}], out_of_scope, user_supplied, links, dropped, warnings}`,
plus `web` when a question was answered from a Google Search (step 3, or an in-scope question no fact answers;
`answer` is then `[]`) or a
field was searched for (below), plus `user_supplied_source` when a `user_supplied` value came from a web search.
`language` is the detected language of the question (BCP 47: `en`, `zh-Hant`, `zh-Hans`, `ja`, `ko`, ...). A
sentence whose translation failed its checks (step 7) is in English even when `language` is not. When no translation
can be used (no model call can be made: allowance refused, the request stopped or no time left; or the call was made
and it failed or none of its sentences passed the checks) the answer takes a fixed form, without another translation
call: a Chinese question gets fixed Chinese sentences written by code (a lead "目前無法產生完整回答，以下是紀錄中的事實。",
or "…請查看下方的原始資料連結。" when no fact has Chinese wording, plus the code-written Chinese of the comparison and
missing-field sentences; English sentences without a Chinese version are left out); any other language gets the
English sentences with an English lead, and `language` is then `"en"`. An English question is answered as usual.
`warnings` is `[]` except `rules_rerun_unavailable` (mock mode answered from the input RulesOutput),
`generation_degraded` (a model call was refused before it was sent: what earlier calls of the same request produced
stays, and no search, extraction or translation is made after it; with nothing from Gemini the answer is the
program's facts or fixed sentences; see "Abuse and cost protection") and `search_unavailable` (a Google Search was
called for and gave no answer: refused by the daily Search allowance, failed, out of time or no source. The reply is
then made without it: the fixed refusal for an out-of-scope question, otherwise the usual steps without the search
(the answer from the facts, or the fixed "not enough data" sentence); for web's search button, the searched field's
fixed sentence alone, with no model call. `search_outcome` then says why: `no_result` (the search ran and named no
source, or its answer was not usable) or `unavailable` (it could not run). A provider quota error (429), a missing key
or another setting error counts as a failed search and adds it. Not added when an allowance refusal stopped the
request: `generation_degraded` says so). `generation_degraded` is also added when the answer call itself failed (5xx,
timeout, 429, no key) and nothing else answers: the reply is then the fixed "not enough data" sentence.
1 or more passing sentences gives 200 (at most 3, plus the fixed lead sentence when no model call could be made); 0 gives 502 `validation_failed`.

Steps:

1. Gemini call 1 (classify): the question's language, out of scope, whether public web pages can answer it
   (`searchable`), a stated year built or cladding, suggested links.
   The language is corrected by script
   (`followup.normalize_language`): Hangul gives `ko`, kana `ja`, Han `zh-Hant` or `zh-Hans` (a bare `zh` is decided
   by common simplified/traditional characters; `ja` is kept for Han-only Japanese); a question without CJK script
   is never zh/ja/ko; an invalid code gives `en`.
2. `user_supplied`, checked by code: `year_built` is an integer 1800..this year or a decade such as `1990s`
   that appears in the question as written; `cladding` is one of the 13 values of the shared table
   (`shared/b_common/cladding.py`: `aluminium`, `brick`, `concrete`, `fibrous_cement`, `glass`, `iron`, `fabric`,
   `plastic`, `monolithic`, `stone`, `tiles`, `wood`, `mixed`), and one of that value's keywords (for example
   "weatherboard" for `wood`) must be written in the question. A question with a conditional or
   negation word (`if`, `not`, `如果`, `假如`, `不是`, ...) gives no value, and so does a value the question only
   asks about (`followup.affirmed`: "Was it built in 1997?", `這房子是 1997 年蓋的嗎？`; "It was built in 1997, what
   does that mean?" states it). Logged as `user_supplied_ignored`.
3. An out-of-scope question of fact that classify says web pages can answer (schools, commute, services nearby,
   published crime statistics), or a complete definition question ("What is a LIM report?", "What does overpriced
   mean?", `什麼是 LIM 報告？`; `followup.definition_question`) that the facts do not answer: a Gemini call with the
   Google Search tool
   (`followup.gemini_search`, prompt `followup_prompt.web_prompt` with the matched address, the question and the
   history), made once more, time allowing, when the first answer names no source. Field searches (below) and this
   search share one limit: at most 2 search calls per request. The response carries `web: {text, sources: [{title, url}], search_suggestions}`: the answer in the
   language of the question, the pages the search grounded it on (`title` is the site's domain, `url` is
   Google's redirect) and Google's search-suggestions HTML. This text is **not** checked by the validator and is
   not edited by code: our reading of the Gemini API terms, section "Grounding with Google Search"
   (https://ai.google.dev/gemini-api/terms; not legal advice), is that it is to be shown as returned, together with
   its search suggestions, and is not to be stored (so the tests use a hand-written answer).
   When neither answer names a source, the call fails, the mode is `strict` or `REPORT_MOCK` is on, the refusal
   below is returned instead. Only the route searches: `answer_followup` called directly (the eval script) does
   not, because an eval run stores what it gets.

   Who may be searched: (a) a property field's fixed query (`field_search`: the button, or a typed question about a
   field the records lack), in the asker's language, never the question or the history; (b) a typed question
   (`followup.search_eligible`) that is either one complete request for what a term or document is
   (`definition_question`: a single clause, a term wording, no wish to buy or bid in it, no prediction, worth or own
   figure; a price word only as the term asked about) or a question classify flags `searchable` (out of scope, or in
   scope when no fact answers it or every sentence is blocked); in both cases only when classify did not flag advice
   and no clause wishes to buy or bid (`purchase_intent`). Never searched: advice (`asks_advice`: the keywords anywhere in the
   question, or classify's `advice` flag unless the whole question asks for facts, `factual_request`: a definition,
   what to check or look out for, or what a report item means for this property; a term or diligence clause beside a
   purchase clause never cancels the flag), any wish to buy or bid in any clause, a prediction, a worth or the buyer's
   own LVR. A search answer is shown whole as returned or not at all (`followup.web_unusable`): a definition answer
   must name the term asked about and say nothing about "this property" or "your home"; any other answer must
   not judge this property or the buyer's home (`judges_property`: a sentence naming it together with a judging word
   such as safe, good, worth, recommend, "the right choice", 值得, 安全, 適合; a fact such as "This home is zoned for
   X" or a direction such as "turn right" passes, and it need not repeat the question's words); a field answer (button
   or typed) must pass the same `judges_property` check and speak of that field (its words, a year or an era); and
   `web_advice` catches the verdict wordings it lists. A rejected answer
   is not edited; the reply is then as if the search gave nothing (`search_unavailable`). This screen narrows what
   model text reaches the page; it is not a proof (see "Known limitations" below). A short reaction with nothing new
   ("Are you sure?", "你確定嗎") is replaced by the earlier user question when that turn reads as a question, not an
   instruction; when the earlier answer is inferred to be a search answer (a definition question whose answer is not a
   fixed sentence), the search is asked again.

   A purchase question that also names a report topic (hazards, title, year built or cladding, loan, LIM; `mixed`)
   has no model text at all: no answer call, no search, no translation. The reply is the fixed `DECLINE_EN` (Chinese
   written by code, `DECLINE_ZH`) followed by the checked drafts of the facts about the topics it names
   (`topic_fact_ids`, at most 3) and the program sentence of a field it asks about. Every language other than Chinese
   gets the English sentences and `language: "en"`; Chinese gets the Chinese decline, code-written Chinese where code
   has it (comparisons, missing-field sentences) and the other drafts in their English original, which `DECLINE_ZH`
   says ("以下是紀錄中的事實（英文原文）"). With no such topic, only the price, or no fact on the topic, the fixed
   refusal below.

   Otherwise the answer is one fixed refusal chosen by code: English, Chinese written by code (`REFUSAL_ZH`,
   zh-Hans by character mapping), or for other languages a restricted translation of the English (step 7; English
   when it fails). The refusal of advice is never translated: Chinese by code, every other language the English with
   `language: "en"`. `links` are
   the suggested PropertyContext link ids; if Gemini suggests none, code picks at most 2 by question type
   (valuation or sale: `homes`, `oneroof`, `council_valuation`; flooding: `council_flood_viewer`,
   `council_geomaps`; other hazards: `council_geomaps`, `council_flood_viewer`; anything else: `council_geomaps`),
   keeping only ids PropertyContext has (else its first link). `exempt`: the refusal has `source_ids: []` and skips
   the validator; `strict`: it goes through the validator, is dropped, and the response is 502.
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
   is its final check), and then through `followup.judges_purchase`: a rewrite with a verdict wording (`web_advice`),
   a judgment of this property (`judges_property`), or a wish to buy or a push ("go ahead", "proceed", 放心, 入手)
   that the facts it cites do not themselves say is blocked too. A blocked one falls back to its facts' English drafts
   (model order kept, at most 3;
   a draft may be longer than the 25-word limit, which is for rewrites: some verified claims are),
   which step 7 translates. `question` and `history`
   (including assistant turns) are quoted as untrusted data and never enter the evidence. The one exception: a
   used `year_built` is an allowed number for the `kb_items_from_user_input` items only.
   **No fact answers the question**: the model also returns `"answered"`, false when no fact says
   anything about what was asked (what a term or a map layer means, how two terms differ, a topic the facts do not
   hold). The reply is then never facts about something else: it is a Google Search answer as in step 3 (`web`,
   `answer: []`, `out_of_scope: false`), or, when there is no search (eval, `strict`, no answer), the one fixed
   sentence "This page does not have enough data to answer this question." (Chinese by code, other languages
   by restricted translation) with the fallback links. Not applied when the question states a year or cladding.
   Except for a question about what this report's items mean ("... mean for this property?", or "What do the hazard
   maps mean?": a report item named with "the") or what to check (`diligence_question`): the checked drafts of the
   facts about the topics it names (`topic_fact_ids`), or, for a diligence question that names no topic ("What should
   I check before I buy?"), of the hazard, title and LIM facts (`DILIGENCE_DEFAULT`), without a search.
   This holds when the answer call returns. When the answer call failed (a 5xx, a timeout, 429, no key) and
   nothing else answers, the reply is the fixed "not enough data" sentence (Chinese written by code, other languages
   English with `language` "en") with `generation_degraded`, not the must-mention drafts. When no model call answers
   because every call was refused or had no time, the reply is the program's own, including for a question classify
   could not sort: the program sentence of a field the question names, when there is one (step 9), else the drafts of
   the facts about the topics the question names (`topic_fact_ids`, at most 3), else the fixed sentence "A full answer
   could not be produced right now; the original sources linked below may help." with the fallback links. Never facts
   about something else. `generation_degraded` marks a refusal; after a failure `X-Report-Degraded-Reason: model_error` tells too.
   The same search is made, before an error or a lead sentence with nothing after it, when the facts give no
   sentence that can be shown: every rewrite and draft blocked, or none that could be put into the asker's language.
   A question about the loan or the First Home Loan (and nothing else the facts cover) with no loan fact in the list
   (no price, deposit or income entered, or a cash purchase) is searched for the same way, before any answer call;
   with no search answer it gets two fixed sentences, "This page does not have enough data to answer this question."
   and one that says what is missing, with `source_ids: []` and `out_of_scope: false` (Chinese by code, other
   languages translated, or English with `language: "en"`).
6. Every sentence goes through `followup.check_answer`: `validator.check_sentence` plus language rules.
   - The sentence must be in the detected language (`language_mismatch`): English has no CJK script; Chinese has
     Han and no kana/Hangul; Japanese has kana; Korean has Hangul. Other Latin-script languages are not told
     apart from English.
   - English: 25 words (`MAX_WORDS`); other Latin-script languages 35. Chinese/Japanese/Korean: 60/90/80 units
     (`CJK_MAX_UNITS_BY_LANG`: CJK characters plus Latin words and numbers) instead of the word limit.
   - CJK: verdict, advice and approximation words per language are `banned_word` (Chinese 安全, 建議, 應該, 必須,
     and more; Japanese and Korean have their own lists, all in `followup._BANNED_BY_LANG`). The Chinese words for
     "may" pass only when a source the sentence cites says may, might, can or could, and are `banned_word`
     otherwise. English verdict words glued to CJK text are banned too; CJK number words (two, zero, per cent) are
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
   or history (so the user's own wording does not reach this prompt). Each result is used only when
   `followup.check_translation` passes: its numbers are exactly the English sentence's numbers; no URL, `@` or
   domain the English does not have; at most 1.5 times as long; the kept English terms and the values the fact shows (title name, cladding,
   land-use words) are copied as written; for CJK, a negation is kept when the English has one; and the step 6
   rules pass in the target language with the sentence's own allowances (a program fact's or comparison's user
   year). A rejected translation is sent once more with the numbers, terms and values to copy; a second failure
   leaves that whole sentence in English (never a mixed sentence). When no sentence at all could be translated, the
   answer takes the fixed form described under `language` above. Why translation and not more hand-written
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

### Known limitations (follow-up routing)

- **Search answers.** A search answer is shown as returned (the Gemini API terms) and is checked only by
  `web_unusable`: a definition answer must name the term and must not speak of this property; any other answer (out of
  scope, or in scope with no fact) must not judge this property in the words `judges_property` knows (it is not checked for relevance to the question);
  every answer must avoid the verdict wordings `web_advice` lists. A web page that gives advice in other words ("LIM
  reports are useful; go ahead with the purchase", "families love this suburb, don't miss out") can pass, in any
  language. The page labels it as an unverified web search answer; the risk is narrowed (never searched for advice
  or a purchase clause), not removed.
- **Translated answers.** A non-English, non-Chinese answer to a factual question still uses Gemini's restricted
  translation of the program's English sentences (step 7). Its checks pin numbers, terms, length and, for CJK,
  negation; they do not read meaning in other languages. Purchase answers (the refusal of advice, the decline and its
  facts) and the buyer's own LVR are never translated (English, `language: "en"`; Chinese by code).
- **Language label of a search answer** (`followup.web_language`) is a script/word-count heuristic: capitalised
  words are left out, quoted text is left out even when it is the substantive answer, and an unexpected Latin-script
  language (Spanish to an English question) is labelled `en`; Han-only Japanese can be labelled Chinese. The text is
  never changed; only the label can be wrong.
- **Where a reaction came from.** web sends only role and text, so whether the earlier answer was a search answer
  is inferred from the earlier question's wording (a definition) and from the first matching turn, not from a
  recorded origin. A repeated question, a non-definition earlier search, or an instruction reworded to avoid the
  instruction words (`_INSTRUCTION`) can be resolved or rechecked wrongly; the resolved question is still checked for
  advice and search eligibility like any other.
- **Keyword lists.** `_ADVICE_*`, `purchase_intent` and `_TERM_Q` cover common English, Chinese, Japanese and Korean
  (and a few French, Spanish, German definition) wordings. They only withhold a search or a model answer; a
  rewording they miss falls back on classify's `advice` flag; a definition wording they miss is searched only when
  classify flags the question `searchable`. `judges_purchase` screens a fact rewrite for the verdict, judging and push
  words it lists, so a purchase push in other words can still pass as a rewrite of a valid fact.

### Fields the records do not have: web search

For `year_built`, `cladding` and `last_sale`, which the first-hand sources often lack, a Google Search may be used
(`app/web_fields.py`, `followup.field_search`). Its answer is shown as returned and marked as not checked:

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
  no source; at most 2 search calls per request in all (with the question search), each only with at least 11 s
  left (see Time). Sources are not filtered by domain. No answer, no source, a failed call,
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
- **Merge.** A value taken from a search answer is not checked against any official record: it is treated like a
  value the user gave, and every sentence that depends on it carries the web-search prefix (below).
  `year_built` and `cladding` go into `user_supplied` with `user_supplied_source: {"year_built":
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
- **Several owners: shown, not filled in** (`followup.multi_unit_kind`). When `title_type` is
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
  restricted translation (English when a check fails); every sentence is checked for wording and length, and against
  the value the search gave (not against any record).
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
- **The last-sale button on a property with several owners**: nothing is ever filled in for a sale, but
  the result may be another unit's, so `answer` holds the one sentence that says so ("This is a unit title property,
  so the result may describe another unit or the whole building."); `autofill_skipped` stays absent.
- **Nothing to fill in and no fact to add**: beside a search answer, neither the program sentence nor
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
extraction, field and question searches) is made only when at least 11 s are left (`gemini.ADMIT_S`) and is sent
with a deadline of at least 10 s (`gemini.MIN_CALL_S`; the API refuses shorter ones with 400 "Minimum allowed
deadline is 10s"), at most `GEMINI_TIMEOUT_S` and 1 s less than what is left, so a call's timeout ends before the
request's deadline.
At most 2 search calls per request (`gemini.MAX_SEARCHES`). The rules re-run gets at most 5 s, the KB read at most
5 s. Without time for a call the answer is code's: the summary's must-mention drafts, the follow-up's program
sentences (or the drafts of the facts the question is about, else a fixed sentence), English instead of a
translation, the fixed refusal instead of a search.

Mock (`REPORT_MOCK=true`, no Gemini call, for front-end work without a key): code classifies the question.
Out-of-scope keywords (school, zone, commute, crime, neighbour, traffic, 學區, 通勤, 治安, ...; not when the
question names a hazard) give the refusal (Chinese for a Chinese question, else English) and the link fallback.
Mock mode never translates: other languages get English sentences. A year
or cladding goes through the same checks, merge and rules re-run; if rules-api is unavailable the input
RulesOutput is used and `warnings` has `rules_rerun_unavailable`. The answer is the comparison sentence (if
any; Chinese for a Chinese question) plus the `claim_en` of each item the user's value selected, verbatim (English,
with the English prefix), validated and prefixed. Any other question gets one fixed mock sentence (Chinese for a
Chinese question) citing the first non-KB source id of the request.

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
# the "Search the web" button next to a field the records do not have
curl -s 'localhost:8090/v1/followup' -H 'content-type: application/json' \
  -d "$(jq '{property_context, user_inputs, rules_output} + {search_field: "year_built"}' \
        services/report-api/fixtures/inputs/case1.json)"
```

## Validator (`app/validator.py`)

A sentence fails when any rule fails (rule code in parentheses, logged server-side); a failed rewrite is replaced by
its facts' drafts:

- empty text (`empty_text`);
- no `source_ids` (`no_source_ids`); an id that is not a PropertyContext source, a rules-api source
  (`/v1/sources`) or a selected KB item (`unknown_source_id`); a `kb_` id outside `kb_items` ∪
  `kb_items_from_user_input` (`kb_not_selected`);
- a number that does not equal a field mapped to a cited id (`number_not_in_cited_sources`: hazard `overlap_pct`
  and data year, property fields by `source_id`, valuation year, published bank rates, First Home Loan income
  caps, numbers inside a cited KB `claim_en`). Exact after removing `$`, `NZ$`, thousands separators and `%`; a
  value rounded to the field's display precision (percent 1 dp, money whole dollars, rates 2 dp) and the year of an
  ISO date also match; "1990s" reads as 1990;
- a number that is not one complete token: glued to letters, written with non-ASCII digits or split by a space
  (`invalid_number_format`);
- a computed value (loan amount, LVR, DTI, high-DTI threshold, repayments, stress figures, LMI amounts,
  deposit share) or 0, unless the number comes from a cited KB claim (`computed_value`);
- `k`/`m` abbreviations (`abbreviated_number`), number words (`number_word`);
- verdict, advice, prediction or approximation words (`banned_word`; "will", "must", "should", "likely",
  "unlikely" and "avoid" are allowed when the cited KB claim contains them);
- URLs or HTML, `@`, or a domain other than the host of a source the sentence cites (`url_or_html`); field names
  or internal terms (`internal_jargon`);
- more than 25 words, not counting the code-added prefix (`too_long`);
- "shows no", "does not show", "no flooding" or "no flood hazard" in a sentence that cites a hazard layer that could
  not be checked (`unsupported_absence`);
- landslide content without a High (or Very High) class or without `kb_landslide_map_context` (`landslide_rule`);
- lending content when there is no loan (`lending_without_loan`).

**Limits of the sentence check.** `check_sentence` on its own does not bind a number or a name to the field a
sentence names: a figure passes when the cited source has that figure anywhere, and it cannot tell whether a
paraphrase keeps a claim's meaning (`tests/test_validator_adversarial.py` keeps these cases as expected failures).
Model text is held tighter because it goes through `check_rewrite`, where values enter only through placeholders.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | none | Gemini API key (Secret Manager on Cloud Run). Never logged |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Same default as jobs/rates-refresh |
| `RULES_API_URL` | `http://localhost:8080` | KB items and sources (cached in process for 10 minutes); an ID token is added for `https://` |
| `KB_SOURCE` | `rules-api` | `local` reads `kb/seed` instead (tests, local runs) |
| `KB_DIR` | `kb/seed` | Seed directory for `KB_SOURCE=local` |
| `REPORT_MOCK` | false | `true`: no Gemini call (below) |
| `GEMINI_TIMEOUT_S` | 20 | Limit for one Gemini call |
| `REQUEST_DEADLINE_S` | 25 | Total budget; a Gemini call only with at least 11 s left, sent with at least 10 s; one retry (invalid JSON, 0 passing sentences, model error) only if the remaining time covers another attempt; no time for a call: drafts |
| `PROTECTION_BACKEND` (`firestore` on Cloud Run, `memory` elsewhere; `off` turns the allowance off), `GEMINI_DAILY_CALLS` (100), `SEARCH_DAILY_CALLS` (30), `GEMINI_EVENT_CALLS` (3000), `GENERATION_SLOTS` (2), `GEMINI_MAX_OUTPUT_TOKENS` (2048), `GEMINI_INPUT_MAX_BYTES` (36000), `REPORT_DEGRADED` (false), `PROTECTION_TIMEOUT_S` (3, the same as `.github/deploy/targets.json`), `REPORT_CACHE_SIZE` (100), `REPORT_CACHE_TTL_S` (600) | | See "Abuse and cost protection" |

## Abuse and cost protection (`app/protection.py`)

- **Generation slots**: at most `GENERATION_SLOTS` /v1/report and /v1/followup requests run at once per process; the
  next is refused at once with 503 `busy` (`Retry-After: 5`), no queue. `/health` takes no slot.
- **Allowance**: each Gemini SDK call (`gemini.gemini_generate`, `followup.gemini_json`, `followup.gemini_search`;
  every classify, answer, retry, translation, extraction and Search goes through one of them) first reserves one call
  in a Firestore transaction (`protection/control` + `protection/daily-<UTC date>`; DEPLOY.md section 8d). Refused:
  daily (`GEMINI_DAILY_CALLS`) or total (`GEMINI_EVENT_CALLS`) allowance used up, `protection/control.disabled`,
  Firestore failure or a transaction slower than `PROTECTION_TIMEOUT_S`. Refused calls are never sent; failed calls
  stay counted. A Search call also counts against `SEARCH_DAILY_CALLS`; when only that is used up, only the search is
  skipped: the follow-up carries `search_unavailable`, not `generation_degraded`. `PROTECTION_BACKEND=memory` keeps
  the same counters in one process (local runs and tests); `off` skips the allowance (the slots, `REPORT_DEGRADED`
  and the prompt size cap still apply).
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

`REPORT_MOCK=true` returns the facts' drafts (the `code-draft` sentences, no Gemini call) for the nine fixtures in
`fixtures/inputs/` (matched on `property_context.address.matched` and `user_inputs`), through the real validator
(including the 25-word limit) and the real code-built parts. Other inputs get one fixed mock sentence plus the
code-built parts.

## Run and test

```
make test-report-api
REPORT_MOCK=true make local-report          # :8090, KB from kb/seed
curl -s localhost:8090/v1/report -H 'content-type: application/json' \
  -d "$(jq '{property_context, user_inputs, rules_output}' services/report-api/fixtures/inputs/case1.json)"
```

Evaluation against the nine golden cases: `scripts/eval_report_api.py` (writes to `out/golden/eval/`, which is not
in git).

## Fixtures

`fixtures/inputs/*.json`: the nine golden cases.
