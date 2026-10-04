# property-api

Turns one Auckland address into a `PropertyContext` (hazard layers, council valuation, title type,
links and source credits). Every field carries `status` (`found` / `not_found` / `unavailable`) and a
`reason`, so "not found" is never shown as "no risk".

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /v1/addresses?q=` | Up to 8 Auckland addresses starting with the typed text |
| `GET /v1/property-context?address_id=` | `PropertyContext` for a LINZ `address_id` |
| `GET /v1/map?address_id=` | GeoJSON (WGS84) for the report map: parcel, address point and hazard features within about 120 m. Not part of the hand-off contract |
| `GET /health` | Health check (`/healthz` gives the same answer locally; Cloud Run reserves some paths ending in `z`) |

## Run locally

From the repo root, with `LINZ_API_KEY` in `.env` (see `.env.example`):

```bash
python3 -m venv .venv && .venv/bin/pip install -r services/property-api/requirements.txt pytest httpx
cd services/property-api
PYTHONPATH=../../shared ../../.venv/bin/uvicorn app.main:app --port 8081 --env-file ../../.env
```

## Tests

Tests replay real upstream responses stored in `tests/fixtures/` (source URL and capture date are in
`manifest.json`); they need no network and no key.

```bash
cd services/property-api && ../../.venv/bin/python -m pytest tests -q
```

Re-record the fixtures (needs the key and network): `python tests/record_fixtures.py`.
The two fixture addresses are public facilities, not private homes.

## Data sources

| Data | Source | How it is used |
|---|---|---|
| Address search, parcel, title type | LINZ Data Service (CC BY 4.0) | WFS query per request |
| Flood plains, flood prone areas, overland flow paths, coastal inundation (1% AEP) | Auckland Council open data | ArcGIS query per parcel; nothing is copied |
| Landslide susceptibility, liquefaction, tsunami evacuation zone | Auckland Council open data (CC BY 4.0) | Same; reported as secondary layers |
| Coastal erosion (ASCIE 2130, RCP8.5+) | Auckland Council GeoMaps server (GeoMaps terms: personal and business use, no substantial copying) | Same; secondary layer |
| Capital value, land value, value of improvements, annual rates, rateability, improvement description, land use, land area, legal description, record of title | Auckland Council GeoMaps "Rate Assessment" layer (GeoMaps terms: personal and business use, no substantial copying) | One ArcGIS query per request |
| Build decade, floor area, building site coverage, local board | Auckland Council property page | One page fetch per request, parsed |

Credit lines to display are returned in `sources[].attribution`.

Links to Homes.co.nz and OneRoof open the first DuckDuckGo result for the address on that site; this service
never queries those sites or the NHC portal. Council links open the property directly.

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
