"""Auckland Council: hazard layers, GeoMaps rate assessment, property page. Live queries; nothing is copied."""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from functools import lru_cache
from typing import Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from shapely import make_valid
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from . import http

NZ = ZoneInfo("Pacific/Auckland")
ARCGIS = "https://services1.arcgis.com/n4yPwebTjJCmXB6W/arcgis/rest/services"
GEOMAPS = "https://mapspublic.aucklandcouncil.govt.nz/arcgis/rest/services"
GEOMAPS_VIEWER = "https://geomapspublic.aucklandcouncil.govt.nz/viewer/index.html"
RATES_SEARCH = "https://www.aucklandcouncil.govt.nz/en/property-rates-valuations/find-property-rates-valuation.html"
RATES_PAGE = "https://www.aucklandcouncil.govt.nz/en/property-rates-valuations/find-property-rates-valuation/{key}.html"
RATE_ASSESSMENT = GEOMAPS + "/Landbase/MapServer/37"  # one row per rating unit
PROPERTY = GEOMAPS + "/Landbase/MapServer/36"  # council properties, with their local board
_ASSESSMENT_FIELDS = ("FORMATTEDADDRESS,CT,LEGAL,RATEACCOUNTKEY,VALUATIONREF,CV,LV,IV,VALUATIONDATE,RATEABILITY,"
                      "IMPROVEMENT,LANDUSEDESCRIPTION,TOTALRATESINCLCIP,AREALABEL")

# class_field: reported in "classes"; rank_field: orders them (highest wins);
# region_wide: always intersects, only the class matters.
HAZARD_LAYERS = [
    {"layer": "flood_plain", "service": "Flood_Plains", "priority": "primary", "geometry": "polygon",
     "source_id": "ac_flood_plains"},
    {"layer": "flood_prone_area", "service": "Flood_Prone_Areas", "priority": "primary", "geometry": "polygon",
     "source_id": "ac_flood_prone"},
    {"layer": "overland_flow_path", "service": "Overland_Flow_Paths", "priority": "primary", "geometry": "line",
     "source_id": "ac_overland_flow"},
    {"layer": "coastal_inundation", "service": "Coastal_Inundation_1_AEP", "priority": "primary",
     "geometry": "polygon", "source_id": "ac_coastal"},
    {"layer": "landslide_shallow", "service": "Shallow_Landslide_Susceptibility", "priority": "secondary",
     "geometry": "polygon", "source_id": "ac_landslide", "class_field": "SusceptibilityValue",
     "rank_field": "SusceptibilityCode", "region_wide": True},
    {"layer": "landslide_large_scale", "service": "Large_Scale_Landslide_Susceptibility", "priority": "secondary",
     "geometry": "polygon", "source_id": "ac_landslide", "class_field": "SusceptibilityValue",
     "rank_field": "SusceptibilityCode", "region_wide": True},
    # not in spec 3.1; never "primary"
    {"layer": "liquefaction", "service": "Liquefaction_Vulnerability_Basic_Assessment", "priority": "secondary",
     "geometry": "polygon", "source_id": "ac_liquefaction", "class_field": "Vulnerability", "region_wide": True},
    {"layer": "tsunami_evacuation_zone", "service": "Tsunami_Evacuation_Zone", "priority": "secondary",
     "geometry": "polygon", "source_id": "ac_tsunami", "class_field": "ZONETYPE"},
    # the open-data version is a line; GeoMaps has the polygon
    {"layer": "coastal_erosion", "url": GEOMAPS + "/UnitaryPlan/PNPSUDCoastalErosionPolygon/MapServer/0",
     "priority": "secondary", "geometry": "polygon", "source_id": "ac_coastal_erosion"},
]
HAZARD_KEYS = ("layer", "priority", "geometry", "source_id")
POINT_LINE_TOLERANCE_M = 20  # a point never touches a line: report lines within this distance


def _layer_url(cfg: dict) -> str:
    return cfg.get("url") or f"{ARCGIS}/{cfg['service']}/FeatureServer/0"


def _ms_date(ms: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, NZ).date().isoformat() if ms else None


@lru_cache(maxsize=64)
def data_date(layer_url: str) -> Optional[str]:
    """Layer's last edit date, or None."""
    # Cached for the life of the process; add a TTL if instances become long-lived.
    info = http.request("GET", layer_url, {"f": "json"}).get("editingInfo") or {}
    return _ms_date(info.get("dataLastEditDate") or info.get("lastEditDate"))


def _or_none(fn, *args):
    """Layer metadata is an extra: a failed lookup must not fail the finding it describes."""
    try:
        return fn(*args)
    except Exception:
        return None


def _esri_polygon(geojson: dict) -> dict:
    polys = geojson["coordinates"] if geojson["type"] == "MultiPolygon" else [geojson["coordinates"]]
    return {"rings": [ring for poly in polys for ring in poly], "spatialReference": {"wkid": 2193}}


def _features(body: dict) -> list:
    """A cut-off answer would understate a layer (its overlap, its class, what the map shows), so fail the layer
    instead; page with resultOffset if large parcels or windows turn out to hit the server's record limit."""
    if body.get("exceededTransferLimit") or (body.get("properties") or {}).get("exceededTransferLimit"):
        raise RuntimeError("upstream answer truncated at the record limit")
    return body["features"]


def hazard(cfg: dict, parcel_geojson: Optional[dict], lat: float, lng: float) -> dict:
    """Intersect one layer with the parcel, or with the address point when there is no parcel."""
    out = {k: cfg[k] for k in HAZARD_KEYS}
    is_line, region_wide = cfg["geometry"] == "line", cfg.get("region_wide", False)
    measure = bool(parcel_geojson) and not (is_line or region_wide)
    fields = [cfg[k] for k in ("class_field", "rank_field") if cfg.get(k)]
    # no simplification: 0.5 m of tolerance moved a real overlap from 51.2% to 50.5%
    params = {"spatialRel": "esriSpatialRelIntersects", "f": "geojson", "outSR": 2193,
              "outFields": ",".join(fields) or "OBJECTID", "returnGeometry": str(measure).lower()}
    if parcel_geojson:
        params.update(geometry=json.dumps(_esri_polygon(parcel_geojson)), geometryType="esriGeometryPolygon",
                      inSR=2193)
    else:
        params.update(geometry=f"{lng},{lat}", geometryType="esriGeometryPoint", inSR=4167)
        if is_line:
            params.update(distance=POINT_LINE_TOLERANCE_M, units="esriSRUnit_Meter")
    feats = _features(http.request("POST", _layer_url(cfg) + "/query", params))
    out.update(data_date=_or_none(data_date, _layer_url(cfg)), status="intersects" if feats else "none")
    if not feats:
        return out

    props = [f["properties"] for f in feats]
    if cfg.get("class_field"):
        out["classes"] = sorted({str(p[cfg["class_field"]]) for p in props if p.get(cfg["class_field"])})
    if cfg.get("rank_field"):
        out["susceptibility"] = max(props, key=lambda p: p.get(cfg["rank_field"]) or 0).get(cfg["class_field"])
    if measure:
        parcel = shape(parcel_geojson)
        covered = unary_union([make_valid(shape(f["geometry"])).intersection(parcel) for f in feats]).area
        out["overlap_pct"] = round(100 * covered / parcel.area, 1)
    elif region_wide:
        out["reason"] = "region_wide_layer_class_is_the_finding"
    elif is_line:
        out["crosses_parcel"] = bool(parcel_geojson)
        if not parcel_geojson:
            out["reason"] = f"point_method_line_within_{POINT_LINE_TOLERANCE_M}m"
    else:
        out["reason"] = "point_method_overlap_not_calculated"
    return out


def map_features(cfg: dict, bbox: tuple) -> list[dict]:
    """Layer features inside a lng/lat window (WGS84), clipped to it."""
    params = {"geometry": ",".join(str(v) for v in bbox), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
              "spatialRel": "esriSpatialRelIntersects", "outSR": 4326, "f": "geojson", "returnGeometry": "true",
              "outFields": cfg.get("class_field") or "OBJECTID", "geometryPrecision": 6,
              "maxAllowableOffset": 0.000005}  # ~0.5 m: display only
    window = box(*bbox)
    shapes = [(f["properties"].get(cfg.get("class_field")), make_valid(shape(f["geometry"])).intersection(window))
              for f in _features(http.request("POST", _layer_url(cfg) + "/query", params))]
    shapes = [(cls, geom) for cls, geom in shapes if not geom.is_empty]
    if cfg.get("region_wide"):  # hundreds of cells per class: one shape per class draws the same and is far smaller
        by_class: dict = {}
        for cls, geom in shapes:
            by_class.setdefault(cls, []).append(geom)
        shapes = [(cls, unary_union(geoms)) for cls, geoms in by_class.items()]
    return [{"type": "Feature", "geometry": mapping(geom), "properties": {"layer": cfg["layer"], "class": cls}}
            for cls, geom in shapes]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9/]+", " ", s.lower()).strip()


def _point_query(lat: float, lng: float, out_fields: str) -> dict:
    return {"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4167,
            "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false", "outFields": out_fields}


def _rows_at(url: str, lat: float, lng: float, out_fields: str) -> list[dict]:
    return [f["attributes"] for f in http.request("GET", url + "/query", {
        **_point_query(lat, lng, out_fields), "f": "json"})["features"]]


def rate_assessment_url(lat: float, lng: float) -> str:
    """The query rate_assessment makes, as a link a browser opens. GeoMaps answers 403 to its HTML pages; pjson is
    served, so the reader sees the rows the facts were read from."""
    return f"{RATE_ASSESSMENT}/query?{urlencode({**_point_query(lat, lng, _ASSESSMENT_FIELDS), 'f': 'pjson'})}"


def _only(*groups: list) -> Optional[dict]:
    """The single row of the first group that has exactly one."""
    return next((g[0] for g in groups if len(g) == 1), None)


@lru_cache(maxsize=1)
def rating_year() -> Optional[str]:
    """Rating year of the rates figure. The layer only says it in a field alias ('2026/27 Total rates')."""
    fields = http.request("GET", RATE_ASSESSMENT, {"f": "json"})["fields"]
    alias = next((f.get("alias") or "" for f in fields if f["name"] == "PREADJUSTEDRATES"), "")
    m = re.search(r"(\d{2})(\d{2})/(\d{2})", alias)
    return f"{m.group(1)}{m.group(2)}/{m.group(1)}{m.group(3)}" if m else None


def pick_assessment(rows: list[dict], number: str, road: str) -> Optional[dict]:
    """The row for this street number: flats on one section share a polygon, so match the address line."""
    want = _norm(f"{number} {road}")
    return _only([r for r in rows if _norm((r.get("FORMATTEDADDRESS") or "").split("\r")[0]) == want], rows)


def area_m2(label: Optional[str]) -> Optional[float]:
    """'668 M2' -> 668.0, '2.8668 HA' -> 28668.0."""
    m = re.fullmatch(r"([\d,.]+)\s*(m2|m²|ha)", (label or "").strip().lower())
    return round(float(m.group(1).replace(",", "")) * (10000 if m.group(2) == "ha" else 1), 1) if m else None


def rate_assessment(lat: float, lng: float, number: str, road: str) -> Optional[dict]:
    """Valuation and rates for the address, or None when no single rating unit matches it."""
    row = pick_assessment(_rows_at(RATE_ASSESSMENT, lat, lng, _ASSESSMENT_FIELDS), number, road)
    if not row:
        return None
    non_rateable = (row.get("RATEABILITY") or "").lower().startswith("non")
    return {
        "key": row.get("RATEACCOUNTKEY"),
        "cv": row.get("CV"), "land_value": row.get("LV"), "improvements_value": row.get("IV"),
        "valuation_date": _ms_date(row.get("VALUATIONDATE")),
        "annual_rates": None if non_rateable else row.get("TOTALRATESINCLCIP"), "non_rateable": non_rateable,
        "rating_year": _or_none(rating_year),
        "rateability": row.get("RATEABILITY"), "improvement_description": row.get("IMPROVEMENT"),
        "land_use": row.get("LANDUSEDESCRIPTION"), "legal_description": row.get("LEGAL"),
        "record_of_title": row.get("CT"), "valuation_number": row.get("VALUATIONREF"),
        "land_area_m2": area_m2(row.get("AREALABEL")),
    }


def rate_account_key(lat: float, lng: float, number: str, road: str) -> Optional[str]:
    """Rates assessment number from the open-data property service. Only used when GeoMaps is down."""
    rows = [r for r in _rows_at(f"{ARCGIS}/AC_Property_Query/FeatureServer/0", lat, lng,
                                "FORMATTEDADDRESS,ACRATEACCOUNTKEY") if (r.get("ACRATEACCOUNTKEY") or "").isdigit()]
    want = _norm(f"{number} {road}") + " "  # here the address is one line: "1A Example Road Suburb Auckland 1000"
    row = _only([r for r in rows if (_norm(r.get("FORMATTEDADDRESS") or "") + " ").startswith(want)], rows)
    return row["ACRATEACCOUNTKEY"] if row else None


def _by_key(key: str) -> dict:
    if not key.isdigit():  # the key goes into a where clause
        raise ValueError("rate account key is not a number")
    return {"where": f"ACRATEACCOUNTKEY='{key}'", "outFields": "ACRATEACCOUNTKEY,BOARDNAME", "returnGeometry": "false"}


def local_board(key: str) -> Optional[str]:
    """The local board of the rating unit; the council property page has it too, but not for cloud hosts."""
    rows = [f["attributes"] for f in http.request("GET", PROPERTY + "/query", {**_by_key(key), "f": "json"})["features"]]
    boards = {r.get("BOARDNAME") for r in rows} - {None}
    return boards.pop() if len(boards) == 1 else None


def property_url(key: str) -> str:
    """local_board's query as a link a browser opens (pjson, as for the rate assessment)."""
    return f"{PROPERTY}/query?{urlencode({**_by_key(key), 'f': 'pjson'})}"


def _money(s: str) -> Optional[float]:
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", s)
    return float(m.group(1).replace(",", "")) if m else None


def decade_range(category: Optional[str]) -> Optional[list]:
    """Build decade from the property category (Rating Valuations Rules 2008, App. F, Table 21). R? and LI only."""
    m = re.fullmatch(r"(R[ACDFHNR]|LI)(\d{3})[ABC]?", category or "")
    if not m:
        return None
    code = int(m.group(2))
    if code == 191:
        return [None, 1913]
    if code == 192:
        return [1914, 1929]
    return [code * 10, code * 10 + 9] if 193 <= code <= 209 else None


# A label directly followed by another label has a blank value.
_LABELS = {"Record of title", "Legal description", "Property details", "Capital Value (CV)", "Land value",
           "Value of improvements", "Valuation number", "Property rates", "Rates this year",
           "Rates assessment number", "Detailed rates breakdown", "Property information", "Land area",
           "Total floor area", "Land use", "Building site coverage", "Local board", "Property Category"}


def property_page(key: str) -> dict:
    """Parse the council property page (undocumented HTML); most fields are the GeoMaps-down fallback."""
    relay_url, relay_token = os.environ.get("COUNCIL_RELAY_URL", ""), os.environ.get("COUNCIL_RELAY_TOKEN", "")
    if relay_url and relay_token:  # the council answers 406 to cloud hosts: go through the relay in New Zealand
        page = http.relay_page(relay_url, key, relay_token)
    else:
        page = http.request("GET", RATES_PAGE.format(key=key), as_json=False)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    def after(label: str, n: int = 1) -> str:
        """n-th line after label; '' if absent or blank."""
        i = lines.index(label) + n if label in lines else len(lines)
        return lines[i] if i < len(lines) and lines[i] not in _LABELS else ""

    def area(label: str) -> Optional[float]:
        return area_m2(f"{after(label)} {after(label, 2)}")

    rates_line = after("Rates this year", 2)  # an amount, or "This is a non rateable property"
    non_rateable = "non rateable" in rates_line.lower()
    as_at = re.search(r"valuations as at (\d{1,2} \w+ \d{4})", text)
    year = re.fullmatch(r"\d{4}/\d{4}", after("Rates this year"))
    return {
        "property_category": after("Property Category") or None, "local_board": after("Local board") or None,
        "floor_area_m2": area("Total floor area"), "building_site_coverage_m2": area("Building site coverage"),
        "cv": _money(after("Capital Value (CV)")), "land_value": _money(after("Land value")),
        "improvements_value": _money(after("Value of improvements")),
        "valuation_date": datetime.strptime(as_at.group(1), "%d %B %Y").date().isoformat() if as_at else None,
        "annual_rates": None if non_rateable else _money(rates_line), "non_rateable": non_rateable,
        "rating_year": year.group(0) if year else None,
        "land_use": after("Land use") or None, "legal_description": after("Legal description") or None,
        "record_of_title": after("Record of title") or None, "valuation_number": after("Valuation number") or None,
        "land_area_m2": area("Land area"),
    }
