"""property-api: Auckland address -> PropertyContext (spec 6.2)."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware

from . import context, linz
from .models import AddressSuggestion, PropertyContext

app = FastAPI(title="property-api", version="0.1.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/addresses", response_model=list[AddressSuggestion])
def addresses(q: str = Query(..., min_length=3, max_length=100)) -> list[dict]:
    try:
        return linz.suggest(q)
    except Exception as e:
        raise HTTPException(502, f"address lookup unavailable: {type(e).__name__}")


@app.get("/v1/property-context", response_model=PropertyContext)
def property_context(address_id: int = Query(..., ge=1),
                     input: str | None = Query(None, max_length=200)) -> dict:
    return context.build(address_id, input)


@app.get("/v1/map")
def map_geojson(address_id: int = Query(..., ge=1)) -> dict:
    """GeoJSON for the report map. Not part of the PropertyContext hand-off."""
    try:
        data = context.map_data(address_id)
    except Exception as e:
        raise HTTPException(502, f"map lookup unavailable: {type(e).__name__}")
    if data is None:
        raise HTTPException(404, "address not found in Auckland")
    return data

