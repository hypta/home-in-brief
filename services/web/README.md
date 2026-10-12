# web

The only public service. It serves the report page and relays the page's API calls to the private services;
browsers never call property-api, rules-api or report-api directly.

| Path | Goes to |
|---|---|
| `GET /` | The report page (`app/static/`) |
| `GET /api/addresses`, `/api/property-context`, `/api/map` | property-api `/v1/...` |
| `POST /api/rules`, `GET /api/rates/latest`, `/api/kb/items`, `/api/sources` | rules-api `/v1/...` |
| `POST /api/report` | report-api `/v1/report`, after property-api and rules-api (below) |
| `POST /api/followup` | report-api `/v1/followup`, the same way |

Any other `/api/` path returns 404. A council property category typed as the year built (for example `RD199B`)
is replaced by its decade before the rules request is relayed. On Cloud Run the relay sends an ID token (DEPLOY.md section 11); a plain
`http://` upstream gets none.

`POST /api/report` takes `{"address_id", "user_inputs"}` and returns report-api's answer or the error of the first
service that failed. The browser sends no documents: web fetches the PropertyContext (kept for 10 minutes after the
page was given it, so the summary describes what the page shows) and the RulesOutput itself and sends both, with the
inputs, to report-api. The three calls share one 30 s limit.

`POST /api/followup` takes `{"address_id", "user_inputs", "question", "history"}` and works the same way: the two
documents are fetched here, and the question and history go to report-api as sent (it checks their size). The page's
"Search the web" button sends `{"search_field", "language"}` in place of a question.

For both, report-api is told how long the request has left (`X-Request-Deadline-Ms`, counted here, never taken from
the browser), and its `X-Report-Mode`, `X-Report-Degraded-Reason`, `Retry-After` and `Cache-Control` headers are
passed on to the browser.

Every `/api/` call is counted per visitor (the last public address in `X-Forwarded-For`): 6 reports or answers at
once and one more every 10 s, 20 other calls at once and one more every second. Over that: 429 `rate_limited` with
`Retry-After`. The count is in this instance's memory, so it slows a flood down; report-api's daily allowance is the
hard limit. Where Cloud Run puts the visitor's address in that header is not confirmed in Google's documentation.

## Run locally

Four terminals, from the repo root, with `LINZ_API_KEY` and `GEMINI_API_KEY` in `.env`:

```bash
make local-api
```

```bash
PYTHONPATH=shared:services/property-api .venv/bin/python -m uvicorn app.main:app --port 8081 --env-file .env
```

```bash
make local-report
```

```bash
PYTHONPATH=services/web .venv/bin/python -m uvicorn app.main:app --port 8082 --env-file .env
```

Then open http://localhost:8082. `PROPERTY_API_URL`, `RULES_API_URL` and `REPORT_API_URL` default to ports 8081, 8080
and 8090. Without report-api the page works and only the summary block says it is unavailable. Run
rules-api with `INCLUDE_DRAFT=true` to see knowledge-base notes while they are still drafts.

## Tests

```bash
cd services/web && ../../.venv/bin/python -m pytest tests -q
```

The relay is tested against real rules-api, property-api and report-api responses (`tests/fixtures/README.md`).

## Page

Plain HTML, CSS and JavaScript modules, no build step: `app.js` (page flow), `report.js` (the report blocks),
`map.js` (Leaflet map). The look and the rules for each part of the page are in `DESIGN.md`.

Order: the address box, then the details the report was calculated with in one line each (the form is open until the
first report and folded behind "Change" after it), then the summary, the next steps and what could not be found, then
five folded sections with the records. The hazards section is open when a flood layer is mapped on the parcel. A next
step on the summary's list is not repeated under the note it comes from. Each line opens its sources from one control: a source
shown there is its name, and the words quoted from it, its credit line, licence and dates are in the list of section 5.
While the form is open, choosing an address asks for nothing: the report starts when the visitor chooses "Summarise
this address" (or "Update"), so details can be added first. With the form folded behind a report, choosing another
address starts the next report at once. The two steps above the summary end when the page
sees their requests finish (the records with the figures calculated from them, then the summary); nothing is paced by a timer.
The three typefaces are served from `app/static/fonts/` with their licence texts (SIL Open Font License).

Questions sit in one panel: a bar at the bottom of a narrow screen that opens upward over the page, and a column
beside the report from 1000 px. Until something has been asked it offers four questions to start with. A summary sentence links to the section that holds its records. The explanation of
how the summary is made, and the handwritten margin notes, are open for the first report in a tab only.

The summary block is drawn from `/api/report` after each calculation. The follow-up block
sends each question to `/api/followup` with the last 3 answered turns and draws the turns as a conversation; they
live in page memory and are cleared when another address is chosen. An answer report-api found with Google Search
(`web`) is shown as returned, with its sources and with Google's search suggestions in a frame that runs no scripts;
what the program says about it follows in a box of its own.
A year built, cladding or last sale the records lack has a "Search the web" button in section 2. A year or cladding
the search finds goes into the form and is marked "from a web search, not checked" wherever the page states it (the
form, the readout, the build-era line, the notes it selects, the conversation), is sent as
`user_inputs.chat_supplied_source`, and can be taken out again with "Remove"; once the visitor changes it, it is
their own.
The words of the newest answer appear as if typed; that is a display effect only, since every answer arrives whole
(nothing is streamed from the server). A year built or cladding that report-api reads from a question (`user_supplied`) is
put into the form and the report is calculated again. Loan details typed into the form are kept in `sessionStorage`
for the tab.
The map background is LINZ Basemaps aerial imagery when `LINZ_BASEMAPS_KEY` is set (the page gets the key from
`/config.json`, because browsers fetch the tiles themselves), with OpenStreetMap as the other choice and the fallback.
