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
`"last_sale"` (the "Search the web for this" button; `question` may then be empty; see "Fields the records do not
have" below).

Response 200: `{language, answer: [{text, source_ids}], out_of_scope, user_supplied, links, dropped, warnings}`,
plus `web` when an out-of-scope question was answered from a Google Search (step 3; `answer` is then `[]`) or a
field was searched for (below), plus `user_supplied_source` when a `user_supplied` value came from a web search.
`language` is the detected language of the question (BCP 47: `en`, `zh-Hant`, `zh-Hans`, `ja`, `ko`, ...). A
sentence whose translation failed its checks (step 7) is in English even when `language` is not.
`warnings` is `[]` except `rules_rerun_unavailable` (mock mode answered from the input RulesOutput).
1 or more passing sentences gives 200 (at most 3); 0 gives 502 `validation_failed`.

Steps (INTERFACE_DRAFT 5 and 6.1):

1. Gemini call 1 (classify): the question's language, out of scope, whether public web pages can answer it
   (`searchable`), a stated year built or cladding, suggested links. The language is corrected by script
   (`followup.normalize_language`): Hangul gives `ko`, kana `ja`, Han `zh-Hant` or `zh-Hans` (a bare `zh` is decided
   by common simplified/traditional characters; `ja` is kept for Han-only Japanese); a question without CJK script
   is never zh/ja/ko; an invalid code gives `en`.
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

   Otherwise the answer is one fixed refusal chosen by code: English, Chinese written by code (`REFUSAL_ZH`,
   zh-Hans by character mapping), or for other languages a restricted translation of the English (step 7; English
   when it fails). `links` are
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
   is its final check) and a blocked one falls back to its facts' English drafts (model order kept, at most 3),
   which step 7 translates. `question` and `history`
   (including assistant turns) are quoted as untrusted data and never enter the evidence. The one exception: a
   used `year_built` is an allowed number for the `kb_items_from_user_input` items only.
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

- **Triggers.** (1) A question about one of them (`followup._FIELD_Q`, any wording that asks: a question mark or a
  question word) when PropertyContext has it as `not_found` / `unavailable` (or lacks it), the user has not given
  it (in this question or earlier in `chat_supplied`), and, for the year, the question has no year in it. A field
  PropertyContext has (`found`) is answered from the program's facts as before, with no search. (2) The button:
  `search_field` searches for that one field with no classify call (`question` is optional and only sets the
  language; empty means English).
- **Search.** One Gemini call with the Google Search tool (`followup_prompt.field_search_prompt`: the matched
  address and the fields; it asks for plain sentences such as "built in 1987"), a second only when the first names
  no source and at least 8 s are left. Sources are not filtered by domain (team decision). No answer, no source, a
  failed call, `strict` mode or no `web_search` (eval): the usual steps run (fixed program sentences and links).
- **Shown as returned.** `web: {text, sources, search_suggestions, fields, extracted}`: `text`, `sources` and
  `search_suggestions` as for step 3, never edited or mixed with anything. `fields`: the fields searched for.
  `extracted`: what code took from `text`, e.g. `{"year_built": 1987, "cladding": "wood", "last_sale": {"date":
  "March 2019", "price": "$850,000"}}` (each key only when found).
- **Extraction** (`web_fields.extract`, by code first): a year 1800..this year, or a decade (`1990s`, `1990 年代`),
  whose nearest keyword is a build word (built, constructed, 建於, 興建, ...; not renovated, sold, 改建, 售出, ...);
  the cladding words of `b_common.cladding` in a sentence about the walls (not a roof, floor or fence), giving one
  of the 13 shared values; for the last sale one date and one price in sale sentences. Sentences with a negation or
  an uncertainty word (not, unknown, may, estimated, 無法, 可能, ...) are skipped. Two different values for a field:
  nothing (ambiguous). A field code found nothing for may be asked once of Gemini (`followup_prompt.extract_prompt`:
  the search text only, no question; `web_fields.check_model_values`): kept only when the year is written in the
  text and in the model's quote from the text, or the cladding is named in that quote.
- **Merge.** `year_built` and `cladding` go into `user_supplied` with `user_supplied_source: {"year_built":
  "web_search", ...}` (one entry per `user_supplied` key, `"user"` for values from the question; the key is absent
  when no value came from a search). `last_sale` never goes into `user_supplied` (rules-api has no such input); it
  is only in `web.extracted`. The usual merge applies: values only add; a property value with status `found` wins
  and code writes the comparison ("A web search gave the year built as 1985; the council records the 1960s
  (1960–1969), and this answer uses the council's record." / `網路搜尋找到的建造年份是 1985 年；...`). A new value
  re-runs rules-api with `user_inputs.chat_supplied_source` added (rules-api ignores unknown keys), and the answer
  comes from the new facts ("The year built found online is 1987."). The fixed program sentence of a searched field
  is left out next to the search answer. The button with nothing to fill in (last sale, ambiguous, none found), or
  less than 6 s left, or an answer step that fails: `answer: []` and `web` only.
- **Prefix.** A sentence that depends on a value found online gets "Based on a web search (not checked by this
  service), " / `依網路搜尋結果（未經本系統核對），` / `依网络搜索结果（未经本系统核对），`; other languages translate the
  English phrase (at most 70 characters). The source of a sentence is the field of the user-input KB items it cites
  (`build_era` -> year built, `cladding` -> cladding), else every chat-supplied year/cladding; all from the web
  gives the web prefix, otherwise "Based on what you told us, ".
- **`/v1/report`.** web sends the source back as `user_inputs.chat_supplied_source: {"year_built": "web_search"}`
  next to `chat_supplied` (a missing key or any other value means `"user"`). The summary then says "found online"
  instead of "you gave" and uses the web prefix by the same rule (`prompt.chat_sources`, `prompt.from_web`).
- **Mock** (`REPORT_MOCK=true`): no search; `web` is `followup.MOCK_WEB` (text starting "MOCK web search answer",
  source `example.com`), and extraction by code gives `year_built: 1985`, `cladding: "wood"`, `last_sale: {date:
  "March 2019", price: "$850,000"}` (only the fields asked for).

Time: one budget, `REQUEST_DEADLINE_S`; each Gemini call gets at most `GEMINI_TIMEOUT_S` and what is left;
the rules re-run at most 5 s.

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
