"""LINZ Data Service (WFS): addresses, primary parcels, record-of-title types.

Request format: https://www.linz.govt.nz/guidance/data-service/linz-data-service-guide/web-services
"""
from __future__ import annotations

import os
import re
import unicodedata
from functools import lru_cache
from typing import Optional

from . import http

ADDRESSES = "layer-123113"      # NZ Addresses
PARCELS = "layer-50772"         # NZ Primary Parcels
TITLES = "layer-50804"          # NZ Property Titles (no ownership data)
PROPERTY_ADDRESS = "table-115638"  # NZ Properties: Property-Address Reference
PROPERTY_TITLE = "table-113970"    # NZ Properties: Property-Title Reference

# LINZ value -> the value rules-api matches on (case-sensitive).
TITLE_TYPES = {"Freehold": "Freehold", "Cross lease": "Cross Lease", "Unit Title": "Unit Title"}


def _wfs(type_names: str, cql: str, **extra) -> list[dict]:
    url = f"https://data.linz.govt.nz/services;key={os.environ['LINZ_API_KEY']}/wfs"
    params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "outputFormat": "json",
              "typeNames": type_names, "cql_filter": cql, **extra}
    return http.request("GET", url, params)["features"]


def _quote(values) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(values))


@lru_cache(maxsize=512)
def suggest(q: str) -> list[dict]:
    """Up to 8 Auckland addresses starting with the typed text."""
    q = unicodedata.normalize("NFKD", q).encode("ascii", "ignore").decode()  # the searched column is ASCII
    # goes into a CQL string: drop quotes and LIKE wildcards, keep commas
    q = re.sub(r"[^A-Za-z0-9/,\-]+", " ", q).strip()
    if len(q) < 3:
        return []
    feats = _wfs(ADDRESSES, f"full_address_ascii ILIKE '{q}%' AND territorial_authority='Auckland'",
                 count=8, PropertyName="(address_id,full_address)")
    return [{"address_id": f["properties"]["address_id"], "full_address": f["properties"]["full_address"]}
            for f in feats]


def address(address_id: int) -> Optional[dict]:
    """One address with its point (GeoJSON order: lng, lat; NZGD2000)."""
    feats = _wfs(ADDRESSES, f"address_id={int(address_id)}")
    if not feats:
        return None
    p, (lng, lat) = feats[0]["properties"], feats[0]["geometry"]["coordinates"]
    return {**p, "lat": lat, "lng": lng}


def parcel(lat: float, lng: float, srs: str = "EPSG:2193") -> Optional[dict]:
    """Primary parcel containing the point; NZTM metres by default, EPSG:4326 for maps."""
    feats = _wfs(PARCELS, f"Intersects(shape,POINT({lat} {lng}))", srsName=srs)
    return feats[0] if feats else None


def title_types(address_id: int, lat: float, lng: float) -> list[str]:
    """LINZ title types for the address: by property reference tables, else by location."""
    props = _wfs(PROPERTY_ADDRESS, f"address_id={int(address_id)}")
    ids = {f["properties"]["unit_of_property_id"] for f in props}
    titles = _wfs(PROPERTY_TITLE, f"unit_of_property_id IN ({_quote(ids)})") if ids else []
    nos = {f["properties"]["title_no"] for f in titles}
    cql = f"title_no IN ({_quote(nos)})" if nos else f"Intersects(shape,POINT({lat} {lng}))"
    feats = _wfs(TITLES, cql, PropertyName="(title_no,type)")
    return sorted({f["properties"]["type"] for f in feats if f["properties"].get("type")})
