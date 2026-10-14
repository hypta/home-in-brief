"""Assemble a PropertyContext for one LINZ address. Every lookup fails on its own."""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import quote_plus

from shapely.geometry import Point, shape

from . import council, http, linz

log = logging.getLogger(__name__)
LINZ_CREDIT = "Contains data sourced from the LINZ Data Service licensed for reuse under CC BY 4.0"
AC_CREDIT = "Source: Auckland Council. Indicative only; not a substitute for a LIM or a site-specific assessment."
GEOMAPS_TERMS = "GeoMaps terms: personal and business use; no substantial copying without consent"
_ITEM = "https://www.arcgis.com/home/item.html?id="
# id, title, url, licence, credit line
_SOURCES = [
    ("linz_addresses", "NZ Addresses - LINZ", "https://data.linz.govt.nz/layer/123113-nz-addresses/",
     "CC BY 4.0", LINZ_CREDIT),
    ("linz_parcels", "NZ Primary Parcels - LINZ", "https://data.linz.govt.nz/layer/50772-nz-primary-parcels/",
     "CC BY 4.0", LINZ_CREDIT),
    ("linz_titles", "NZ Property Titles - LINZ", "https://data.linz.govt.nz/layer/50804-nz-property-titles/",
     "CC BY 4.0", LINZ_CREDIT),
    ("ac_flood_plains", "Flood Plains - Auckland Council", _ITEM + "0d50200579364e0bb6cda2be0893fc8b",
     "No licence stated: internal and external use, with disclaimer", AC_CREDIT),
    ("ac_flood_prone", "Flood Prone Areas - Auckland Council", _ITEM + "0be75c47bc1a4f28b9a7745a3eba5cbf",
     "Creative Commons (version not stated)", AC_CREDIT),
    ("ac_overland_flow", "Overland Flow Paths - Auckland Council", _ITEM + "8217a19b1079402bbd026f5a869e8eed",
     "Creative Commons (version not stated)", AC_CREDIT),
    ("ac_coastal", "Coastal Inundation 1% AEP - Auckland Council", _ITEM + "e1b4f3e57dc64b6395098a17ac979eeb",
     "Creative Commons (version not stated)", AC_CREDIT),
    ("ac_landslide", "Landslide Susceptibility 2025 (shallow and large scale) - Auckland Council",
     _ITEM + "7bc3e13a8c194946b478736afcd9d840", "CC BY 4.0", AC_CREDIT),
    ("ac_liquefaction", "Liquefaction Vulnerability Basic Assessment - Auckland Council",
     _ITEM + "89f292e28e194457bf04262647bc1905", "CC BY 4.0", AC_CREDIT),
    ("ac_tsunami", "Tsunami Evacuation Zone - Auckland Council", _ITEM + "3fb25ea90d24498b98571f8e84328987",
     "CC BY 4.0", AC_CREDIT),
    ("ac_coastal_erosion", "Coastal Erosion - ASCIE 2130 (RCP8.5+) - Auckland Council GeoMaps (raw data)",
     council.GEOMAPS + "/UnitaryPlan/PNPSUDCoastalErosionPolygon/MapServer/0?f=pjson", GEOMAPS_TERMS, AC_CREDIT),
    # GeoMaps answers 403 to its HTML pages: links ask for pjson. With an address, this one becomes the query for it.
    ("ac_valuation", "Rate Assessment (valuation and rates) - Auckland Council GeoMaps (raw data)",
     council.RATE_ASSESSMENT + "?f=pjson",
     GEOMAPS_TERMS,
     "Source: Auckland Council (copyright). Council valuations do not represent current market value."),
    ("ac_property_page", "Find your property rates or valuation - Auckland Council", council.RATES_SEARCH,
     "Website terms: reproduction for informational purposes with acknowledgement",
     "Source: Auckland Council (subject to copyright)."),
]
FLOOD_VIEWER = "https://experience.arcgis.com/experience/cbde7f2134404f4d90adce5396a0a630"
MAP_PAD_DEG = 0.003  # about 270-330 m around the parcel

# Valuation facts come from the GeoMaps rate assessment; page facts exist only on the council property page.
_LAYER_FACTS = ("land_value", "improvements_value", "rateability", "improvement_description", "land_use",
                "land_area_m2", "legal_description", "record_of_title", "valuation_number")
_PAGE_FACTS = ("floor_area_m2", "building_site_coverage_m2", "local_board")
_FACTS = (*_LAYER_FACTS, *_PAGE_FACTS)


def _first_result(site: str, number: str, road: str, suburb: Optional[str]) -> str:
    """DuckDuckGo link that opens the first result on one site (the leading backslash); we never fetch that site."""
    return "https://duckduckgo.com/?q=" + quote_plus(" ".join(f'\\site:{site} "{number} {road}" {suburb or ""} Auckland'.split()))


def _cause(detail: str) -> str:
    """The cause stays in the log; the response carries it only when REASON_DETAIL=on (development)."""
    return detail if os.environ.get("REASON_DETAIL", "").lower() == "on" else "source_unavailable"


def _try(fn: Callable, *args):
    """(result, None) or (None, reason)."""
    try:
        return fn(*args), None
    except Exception as e:
        name = type(e).__name__
        # requests puts the URL (with the LINZ key) in its messages: log it redacted
        log.warning("%s failed: %s: %s", getattr(fn, "__name__", fn), name, http.redact(str(e)))
        return None, _cause("service_timeout" if "Timeout" in name else f"service_error:{name}")


def _miss(err: Optional[str], why: str) -> dict:
    return {"status": "unavailable" if err else "not_found", "reason": err or why}


def _hazard(cfg: dict, geom: Optional[dict], lat: float, lng: float) -> dict:
    out, err = _try(council.hazard, cfg, geom, lat, lng)
    return out or {**{k: cfg[k] for k in council.HAZARD_KEYS}, "status": "unavailable", "reason": err}


def _title_type(types: Optional[list], err: Optional[str]) -> dict:
    base = {"value": None, "source_id": "linz_titles", "linz_types": types or []}
    if err or not types:
        return {**base, **_miss(err, "no_title_found")}
    core = [t for t in types if t != "Supplementary Record Sheet"] or types  # SRS sits beside unit titles
    if len(core) > 1:
        return {**base, "status": "found", "reason": "multiple_title_types"}
    value = linz.TITLE_TYPES.get(core[0])
    return {**base, "value": value, "status": "found", "reason": None if value else "unmapped_title_type"}


def _council(lat: float, lng: float, number: str, road: str):
    """(rate assessment, its error, rates key, property page, page error)."""
    ra, ra_err = _try(council.rate_assessment, lat, lng, number, road)
    key = ra.get("key") if ra else None
    if ra_err:  # GeoMaps down: get the key from open data
        key, _ = _try(council.rate_account_key, lat, lng, number, road)
    if os.environ.get("COUNCIL_PAGE", "").lower() == "off":  # the council site answers 406 to cloud hosts
        return ra, ra_err, key, None, _cause("council_site_refuses_this_server")
    page, page_err = _try(council.property_page, key) if key else (None, None)
    return ra, ra_err, key, page, page_err


def _council_fields(ra: Optional[dict], ra_err: Optional[str], key: Optional[str],
                    page: Optional[dict], page_err: Optional[str]) -> dict:
    lay, pg = {"source_id": "ac_valuation"}, {"source_id": "ac_property_page"}
    if ra:
        base, src, missing = ra, lay, ("not_found", "not_in_rate_assessment")
    elif ra_err:  # GeoMaps down: the page stands in
        base, src, missing = page or {}, pg, ("unavailable", ra_err)
    else:
        base, src, missing = {}, lay, ("not_found", "no_unique_council_property_match")

    def fact(value, why=None, **extra) -> dict:
        ok = value is not None
        status, reason = ("found", None) if ok else (why or missing)
        # missing on the fallback page: attribute to GeoMaps
        return {"value": value, **extra, "status": status, "reason": reason, **(src if ok or why else lay)}

    area = base.get("land_area_m2")  # the council shows 0 when the land is shared
    out = {
        "cv": fact(base.get("cv"), valuation_date=base.get("valuation_date")),
        "annual_rates": fact(base.get("annual_rates"), rating_year=base.get("rating_year"),
                             why=("not_found", "non_rateable_property") if base.get("non_rateable") else None),
        **{name: fact(base.get(name)) for name in _LAYER_FACTS},
        "land_area_m2": fact(area or None, why=(
            "not_found", "council_shows_0_for_cross_lease_or_unit_title") if area == 0 else None),
    }
    if not page:
        status, reason = ("unavailable", page_err) if key else missing
        blank = {"value": None, "status": status, "reason": reason, **pg}
        return {**out, "year_built": blank, **{name: dict(blank) for name in _PAGE_FACTS}}

    def found(ok: bool, why: str) -> dict:
        return {"status": "found" if ok else "not_found", "reason": None if ok else why, **pg}

    rng = council.decade_range(page["property_category"])
    return {**out,
            "year_built": {"value": rng[0] if rng else None, "decade_range": rng,
                           "property_category": page["property_category"],
                           **found(rng is not None, "property_category_has_no_build_decade")},
            **{name: {"value": page[name], **found(page[name] is not None, "not_on_council_page")}
               for name in _PAGE_FACTS}}


def build(address_id: int, typed: Optional[str] = None) -> dict:
    now = datetime.now(council.NZ)  # Cloud Run hosts run in UTC
    sources = [{"id": i, "title": t, "url": u, "licence": lic, "attribution": c, "accessed_at": None}
               for i, t, u, lic, c in _SOURCES]

    def answered(**ok) -> None:
        """source id = whether it answered this lookup; only those are dated."""
        for source in sources:
            if ok.get(source["id"]):
                source["accessed_at"] = now.date().isoformat()
    links = {link["id"]: link for link in (
        {"id": "homes", "label": "Homes.co.nz", "url": "https://homes.co.nz/"},
        {"id": "oneroof", "label": "OneRoof", "url": "https://www.oneroof.co.nz/"},
        {"id": "council_valuation", "label": "Auckland Council property search", "url": council.RATES_SEARCH},
        {"id": "council_flood_viewer", "label": "Auckland Council Flood Viewer", "url": FLOOD_VIEWER},
        {"id": "council_geomaps", "label": "Auckland Council GeoMaps (zoning, pipes and all council layers)",
         "url": council.GEOMAPS_VIEWER},
        {"id": "nhc_claims_map", "label": "NHC Natural Hazards Portal",
         "url": "https://www.naturalhazardsportal.govt.nz/"})}
    ctx = {"generated_at": now.isoformat(timespec="seconds"), "links": list(links.values()), "sources": sources}

    addr, err = _try(linz.address, address_id)
    answered(linz_addresses=not err)
    outside = bool(addr) and addr.get("territorial_authority") != "Auckland"
    if not addr or outside:
        no = _miss(err, "address_not_found")  # LINZ down is "unavailable", never "not_found"
        ctx.update(
            address={"input": typed, "linz_address_id": address_id,
                     **({"status": "not_found", "reason": "outside_auckland"} if outside
                        else _miss(err, "address_id_not_found"))},
            parcel={"spatial_method": "point", **no},
            hazards=[{**{k: c[k] for k in council.HAZARD_KEYS}, **no} for c in council.HAZARD_LAYERS],
            property={name: dict(no) for name in ("cv", "last_sale", "annual_rates", "title_type", "year_built",
                                                  "cladding", *_FACTS)})
        return ctx

    lat, lng = addr["lat"], addr["lng"]
    # the council writes road names without macrons
    number, road = addr["full_address_number"], addr.get("full_road_name_ascii") or addr["full_road_name"]
    ctx["address"] = {"input": typed, "matched": addr["full_address"], "linz_address_id": address_id,
                      "lat": lat, "lng": lng, "status": "found"}
    with ThreadPoolExecutor(max_workers=len(council.HAZARD_LAYERS) + 2) as pool:
        tt = pool.submit(_try, linz.title_types, address_id, lat, lng)
        cc = pool.submit(_council, lat, lng, number, road)
        parcel, perr = _try(linz.parcel, lat, lng)  # only the hazard layers need the parcel
        geom = parcel["geometry"] if parcel else None
        hz = [pool.submit(_hazard, c, geom, lat, lng) for c in council.HAZARD_LAYERS]
    ctx["parcel"] = {"parcel_id": parcel["properties"]["id"], "spatial_method": "parcel", "status": "found"} \
        if parcel else {"spatial_method": "point", **_miss(perr, "no_parcel_at_address_point")}
    ctx["hazards"] = [h.result() for h in hz]

    ra, ra_err, key, page, page_err = cc.result()
    next(s for s in sources if s["id"] == "ac_valuation")["url"] = council.rate_assessment_url(lat, lng)
    answered(linz_parcels=not perr, linz_titles=not tt.result()[1], ac_valuation=not ra_err, ac_property_page=page,
             **{h["source_id"]: True for h in ctx["hazards"] if h["status"] != "unavailable"})
    if key:
        links["council_valuation"].update(url=council.RATES_PAGE.format(key=key), prefilled=True, kind="direct")
        links["council_geomaps"].update(url=f"{council.GEOMAPS_VIEWER}?task=select&entity=rateacckey&values={key}",
                                        prefilled=True, kind="direct")
    for link_id, site in (("homes", "homes.co.nz"), ("oneroof", "oneroof.co.nz")):
        links[link_id].update(url=_first_result(site, number, road, addr.get("suburb_locality_ascii")),
                              prefilled=True, kind="first_result")
    ctx["property"] = {
        **_council_fields(ra, ra_err, key, page, page_err),
        "title_type": _title_type(*tt.result()),
        "last_sale": {"status": "unavailable", "reason": "no_licensed_source"},
        "cladding": {"value": None, "status": "unavailable", "reason": "no_free_source"},
    }
    return ctx


def map_data(address_id: int) -> Optional[dict]:
    """GeoJSON (WGS84) for a map: the parcel, the address point and nearby hazard features."""
    addr = linz.address(address_id)
    if not addr or addr.get("territorial_authority") != "Auckland":
        return None
    lat, lng = addr["lat"], addr["lng"]
    parcel, _ = _try(linz.parcel, lat, lng, "EPSG:4326")
    minx, miny, maxx, maxy = shape(parcel["geometry"]).bounds if parcel else Point(lng, lat).bounds
    bbox = (minx - MAP_PAD_DEG, miny - MAP_PAD_DEG, maxx + MAP_PAD_DEG, maxy + MAP_PAD_DEG)
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
              "properties": {"layer": "address"}}]
    if parcel:
        feats.append({"type": "Feature", "geometry": parcel["geometry"], "properties": {"layer": "parcel"}})
    with ThreadPoolExecutor(max_workers=len(council.HAZARD_LAYERS)) as pool:
        results = list(pool.map(lambda c: _try(council.map_features, c, bbox), council.HAZARD_LAYERS))
    unavailable = [c["layer"] for c, (_, err) in zip(council.HAZARD_LAYERS, results) if err]
    for found, _ in results:
        feats += found or []
    return {"type": "FeatureCollection", "bbox": list(bbox), "pad_m": int(round(MAP_PAD_DEG * 100_000, -2)),
            "features": feats, "unavailable": unavailable}
