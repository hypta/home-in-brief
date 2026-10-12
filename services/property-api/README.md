# property-api

Turns one Auckland address into a `PropertyContext` (hazard layers, council valuation, title type,
links and source credits). Every looked-up field carries a `status` (`found` / `not_found` / `unavailable`; a hazard
layer: `intersects` / `none` / `unavailable` / `not_found`) and, where there is something to explain, a `reason` (why
a value is missing, or how a layer was checked), so "not found" is never passed on as "no risk".

When a source fails or is switched off, `reason` is `source_unavailable` and the cause (a timeout, the error type)
goes to the log only. Set `REASON_DETAIL=on` while developing to get the cause in the response instead.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /v1/addresses?q=` | Up to 8 Auckland addresses starting with the typed text |
| `GET /v1/property-context?address_id=` | `PropertyContext` for a LINZ `address_id` |
| `GET /v1/map?address_id=` | GeoJSON (WGS84) for the page's map: parcel, address point and hazard features within about 300 m. A layer with more features in that window than the council returns at once is listed in `unavailable`. Used only by the page; not part of `PropertyContext` |
| `GET /health` | Health check (`/healthz` gives the same answer locally; Cloud Run reserves some paths ending in `z`) |

## Run locally

From the repo root, after `make setup`, with `LINZ_API_KEY` in `.env` (see `.env.example`):

```bash
cd services/property-api
PYTHONPATH=../../shared ../../.venv/bin/uvicorn app.main:app --port 8081 --env-file ../../.env
```

## Tests

Tests replay real upstream responses stored in `tests/fixtures/` (source URL and capture date are in
`manifest.json`); they need no network and no key.

```bash
cd services/property-api && ../../.venv/bin/python -m pytest tests -q
```

Re-record the fixtures (needs the key and network), from `services/property-api`:
`../../.venv/bin/python tests/record_fixtures.py`. It deletes the recorded files in `tests/fixtures/` first and
rewrites `fixtures/property_context_*.json`.
The two addresses in these fixtures are public facilities (a council reserve and the central library), not private
homes. `tests/fixtures/` holds 57 recorded upstream answers for them (LINZ, Auckland Council open data, GeoMaps and two
council property pages), kept only to replay the tests.

## Data sources

| Data | Source | How it is used |
|---|---|---|
| Address search (NZ Addresses), parcel (NZ Primary Parcels), title type (NZ Property Titles; the titles of an address are found through two LINZ reference tables, NZ Properties: Property-Address Reference and Property-Title Reference) | LINZ Data Service (the three layers: CC BY 4.0; the two reference tables have no entry in `sources[]`) | WFS queries per request |
| Flood plains, flood prone areas, overland flow paths, coastal inundation (1% AEP) | Auckland Council open data (as recorded in `sources[].licence`. Flood plains: "No licence stated: internal and external use, with disclaimer"; the other three: "Creative Commons (version not stated)") | One ArcGIS query per layer per request, against the parcel (or the address point when no parcel is matched); the layer's last-edit date comes from the layer description, cached for the life of the process. The service keeps no copy of a layer |
| Landslide susceptibility, liquefaction, tsunami evacuation zone | Auckland Council open data (CC BY 4.0) | Same; reported as secondary layers |
| Coastal erosion (ASCIE 2130, RCP8.5+) | Auckland Council GeoMaps server (GeoMaps terms: personal and business use; no substantial copying without consent) | Same; secondary layer |
| Capital value, land value, value of improvements, annual rates, rateability, improvement description, land use, land area, legal description, record of title, valuation number (the capital value comes with its valuation date, the annual rates with their rating year) | Auckland Council GeoMaps "Rate Assessment" layer (GeoMaps terms: personal and business use; no substantial copying without consent) | One ArcGIS query per request (the rating year comes from the layer description, cached for the life of the process). If the query fails, the rates key comes from the open-data property service `AC_Property_Query` (see Known limits) |
| Build decade, floor area, building site coverage, local board | Auckland Council property page | One page fetch per request, parsed. Not fetched when `COUNCIL_PAGE=off` |

Credit lines to display are returned in `sources[].attribution`.

Links to Homes.co.nz and OneRoof open the first DuckDuckGo result for the address on that site; this service
never queries those sites or the NHC portal. Separately, report-api can ask Gemini with Google Search for a year built,
cladding or last sale the records lack (`services/report-api/README.md`); that answer may quote such sites, and it is
shown as returned, with its sources, and marked as not checked. The council property-search and GeoMaps links open the property when the
address matched one council rating unit; the Flood Viewer link opens the viewer.

## Known limits

- Address search is a prefix match and takes about 2 seconds per query.
- The council property page has no documented format; if its layout changes, build decade, floor area,
  building site coverage and local board return `unavailable` or `not_found`. Valuation and rates are
  not affected: they come from the GeoMaps layer.
- If GeoMaps is down, valuation and rates fall back to the council property page (found through the
  open-data property service); `rateability` and `improvement_description` are then `unavailable`.
- Build decade comes from the council property category (Rating Valuations Rules 2008, Appendix F), so
  it exists for residential and lifestyle properties only, and is a range, not a year.
- Last sale and cladding have no licensed free source: always `unavailable`.
