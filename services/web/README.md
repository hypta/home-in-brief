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
documents are fetched here, and the question and history go to report-api as sent (it checks their size).

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
`map.js` (Leaflet map). The summary block is drawn from `/api/report` after each calculation. The follow-up block
sends each question to `/api/followup` with the last 3 answered turns and draws the turns as a conversation; they
live in page memory and are cleared when another address is chosen. An answer report-api found with Google Search
(`web`) is shown as returned, with its sources and with Google's search suggestions in a frame that runs no scripts.
The words of the newest answer appear as if typed; that is a display effect only, since every answer arrives whole
(nothing is streamed from the server). A year built or cladding that report-api reads from a question (`user_supplied`) is
put into the form and the report is calculated again. Loan details typed into the form are kept in `sessionStorage`
for the tab.
The map background is LINZ Basemaps aerial imagery when `LINZ_BASEMAPS_KEY` is set (the page gets the key from
`/config.json`, because browsers fetch the tiles themselves), with OpenStreetMap as the other choice and the fallback.
