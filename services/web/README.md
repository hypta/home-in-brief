# web

The only public service. It serves the report page and relays the page's API calls to the private services;
browsers never call property-api or rules-api directly.

| Path | Goes to |
|---|---|
| `GET /` | The report page (`app/static/`) |
| `GET /api/addresses`, `/api/property-context`, `/api/map` | property-api `/v1/...` |
| `POST /api/rules`, `GET /api/rates/latest`, `/api/kb/items`, `/api/sources` | rules-api `/v1/...` |

Any other `/api/` path returns 404. On Cloud Run the relay sends an ID token (DEPLOY.md section 11); a plain
`http://` upstream gets none.

## Run locally

Three terminals, from the repo root, with `LINZ_API_KEY` in `.env`:

```bash
make local-api
```

```bash
PYTHONPATH=shared:services/property-api .venv/bin/python -m uvicorn app.main:app --port 8081 --env-file .env
```

```bash
PYTHONPATH=services/web .venv/bin/python -m uvicorn app.main:app --port 8082 --env-file .env
```

Then open http://localhost:8082. `PROPERTY_API_URL` and `RULES_API_URL` default to ports 8081 and 8080. Run
rules-api with `INCLUDE_DRAFT=true` to see knowledge-base notes while they are still drafts.

## Tests

```bash
cd services/web && ../../.venv/bin/python -m pytest tests -q
```

The relay is tested against real rules-api and property-api responses (`tests/fixtures/README.md`).

## Page

Plain HTML, CSS and JavaScript modules, no build step: `app.js` (page flow), `report.js` (the report blocks),
`map.js` (Leaflet map). The summary and follow-up blocks show "not available yet" until the Gemini layer exists.
The map background is LINZ Basemaps aerial imagery when `LINZ_BASEMAPS_KEY` is set (the page gets the key from
`/config.json`, because browsers fetch the tiles themselves), with OpenStreetMap as the other choice and the fallback.
