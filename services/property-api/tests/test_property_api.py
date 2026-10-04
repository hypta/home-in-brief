"""Each test replays real responses recorded on the date in tests/fixtures/manifest.json."""
import time
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box, shape

from app import context, council, http, linz
from app.main import app
from app.models import PropertyContext
from b_common.schemas import RulesRequest

FLOODED, DRY = 2100210, 1132249  # public facilities: 740 Sandringham Road Extension; 44 Lorne Street
client = TestClient(app)


def build(address_id, **kw):
    return PropertyContext.model_validate(context.build(address_id, **kw))


def hazards(ctx):
    return {h.layer: h for h in ctx.hazards}


def test_suggest_returns_auckland_addresses():
    assert linz.suggest("44 lorne") == [
        {"address_id": DRY, "full_address": "44 Lorne Street, Auckland Central, Auckland"}]


def test_suggest_keeps_user_text_out_of_the_filter(monkeypatch):
    seen = {}
    monkeypatch.setattr(http, "request", lambda m, u, p=None, **k: seen.update(p) or {"features": []})
    linz.suggest("44 lorne' OR 1=1 --%_")
    assert "'44 lorne OR 1 1 --%'" in seen["cql_filter"]
    assert linz.suggest("ab") == []
    linz.suggest("12 Te Atatū Road, Te Atatū")  # commas are part of full_address; the column is ASCII
    assert "'12 Te Atatu Road, Te Atatu%'" in seen["cql_filter"]


def test_address_and_parcel_are_matched():
    ctx = build(FLOODED, typed="740 sandringham")
    assert ctx.address.matched == "740 Sandringham Road Extension, Wesley, Auckland"
    assert ctx.address.input == "740 sandringham" and ctx.address.status == "found"
    assert (ctx.parcel.parcel_id, ctx.parcel.spatial_method, ctx.parcel.status) == (4807871, "parcel", "found")


def test_unknown_address_reports_not_found_everywhere(monkeypatch):
    monkeypatch.setattr(linz, "address", Mock(return_value=None))
    ctx = build(1)
    assert ctx.address.status == "not_found" and ctx.parcel.spatial_method == "point"
    assert {h.status for h in ctx.hazards} == {"not_found"}
    assert {f["status"] for f in ctx.property.model_dump().values()} == {"not_found"}


def test_linz_outage_is_unavailable_not_not_found(monkeypatch):
    monkeypatch.setattr(linz, "address", Mock(side_effect=TimeoutError("simulated")))
    ctx = build(1)
    assert {ctx.address.status, ctx.parcel.status, ctx.property.cv.status} == {"unavailable"}
    assert {(h.status, h.reason) for h in ctx.hazards} == {("unavailable", "service_timeout")}


def test_failed_layer_metadata_keeps_the_finding(monkeypatch):
    replay = http.request

    def no_metadata(method, url, params=None, **kw):
        if params == {"f": "json"}:
            raise TimeoutError("simulated")
        return replay(method, url, params, **kw)
    monkeypatch.setattr(http, "request", no_metadata)
    council.data_date.cache_clear()
    council.rating_year.cache_clear()
    ctx = build(FLOODED)
    flood = hazards(ctx)["flood_plain"]
    assert (flood.status, flood.overlap_pct, flood.data_date) == ("intersects", 51.2, None)
    assert (ctx.property.cv.value, ctx.property.cv.source_id) == (340000, "ac_valuation")
    assert ctx.property.annual_rates.rating_year is None


def test_flood_layers_report_overlap_and_line_crossing():
    hz = hazards(build(FLOODED))
    assert set(hz) >= {"flood_plain", "flood_prone_area", "overland_flow_path", "coastal_inundation"}
    assert (hz["flood_plain"].status, hz["flood_plain"].overlap_pct) == ("intersects", 51.2)
    assert (hz["flood_prone_area"].status, hz["flood_prone_area"].overlap_pct) == ("intersects", 50.8)
    ofp = hz["overland_flow_path"]
    assert (ofp.geometry, ofp.status, ofp.crosses_parcel, ofp.overlap_pct) == ("line", "intersects", True, None)
    assert hz["coastal_inundation"].status == "none"
    assert all(h.source_id for h in hz.values())
    assert [name for name, h in hz.items() if not h.data_date] == ["coastal_erosion"]  # that server gives no date
    assert hz["flood_plain"].data_date == "2026-10-03"  # last edited 01:07 that day in New Zealand, still 2 October in UTC
    assert hz["coastal_erosion"].status == "none"


def test_only_flood_layers_are_primary_and_dry_address_has_none():
    hz = hazards(build(DRY))
    primary = {name for name, h in hz.items() if h.priority == "primary"}
    assert primary == {"flood_plain", "flood_prone_area", "overland_flow_path", "coastal_inundation"}
    assert {hz[name].status for name in primary} == {"none"}
    assert hz["landslide_shallow"].susceptibility == "Moderate"


def test_one_failing_layer_does_not_affect_the_others(monkeypatch):
    replay = http.request

    def flaky(method, url, params=None, **kw):
        if "Flood_Prone_Areas" in url:
            raise TimeoutError("simulated")
        return replay(method, url, params, **kw)
    monkeypatch.setattr(http, "request", flaky)
    hz = hazards(build(FLOODED))
    assert (hz["flood_prone_area"].status, hz["flood_prone_area"].reason) == ("unavailable", "service_timeout")
    assert hz["flood_plain"].status == "intersects"


def test_point_method_when_no_parcel(monkeypatch):
    monkeypatch.setattr(linz, "parcel", Mock(return_value=None))
    monkeypatch.setattr(council, "HAZARD_LAYERS", council.HAZARD_LAYERS[:3])
    ctx = build(FLOODED)
    assert (ctx.parcel.spatial_method, ctx.parcel.reason) == ("point", "no_parcel_at_address_point")
    hz = hazards(ctx)
    assert hz["flood_plain"].overlap_pct is None
    assert hz["overland_flow_path"].reason == "point_method_line_within_20m"
    assert hz["overland_flow_path"].crosses_parcel is False


@pytest.mark.parametrize("types, value, reason", [
    (["Freehold"], "Freehold", None),
    (["Cross lease"], "Cross Lease", None),
    (["Supplementary Record Sheet", "Unit Title"], "Unit Title", None),
    (["Leasehold"], None, "unmapped_title_type"),
    (["Cross lease", "Freehold"], None, "multiple_title_types"),
])
def test_title_type_mapping(types, value, reason):
    out = context._title_type(types, None)
    assert (out["value"], out["status"], out["reason"], out["linz_types"]) == (value, "found", reason, types)


def test_title_type_from_linz():
    assert build(DRY).property.title_type.value == "Freehold"


def test_cv_rates_and_prefilled_council_link():
    ctx = build(FLOODED)
    cv = ctx.property.cv
    assert (cv.value, cv.valuation_date, cv.status, cv.source_id) == (340000, "2024-05-01", "found", "ac_valuation")
    rates = ctx.property.annual_rates
    assert (rates.status, rates.reason, rates.rating_year) == ("not_found", "non_rateable_property", "2026/2027")
    links = {l.id: l for l in ctx.links}
    assert links["council_valuation"].kind == "direct" and links["council_valuation"].url.endswith("/12344049554.html")
    assert links["homes"].kind == links["oneroof"].kind == "first_result"
    assert links["homes"].url == ("https://duckduckgo.com/?q=%5Csite%3Ahomes.co.nz+"
                                  "%22740+Sandringham+Road+Extension%22+Wesley+Auckland")
    assert links["oneroof"].url.startswith("https://duckduckgo.com/?q=%5Csite%3Aoneroof.co.nz+%22740+")
    assert (links["nhc_claims_map"].kind, links["nhc_claims_map"].prefilled) == ("home", False)
    assert links["council_geomaps"].url.endswith("?task=select&entity=rateacckey&values=12344049554")
    assert ctx.property.last_sale.status == ctx.property.cladding.status == "unavailable"
    dry_rates = build(DRY).property.annual_rates
    assert (dry_rates.value, dry_rates.rating_year, dry_rates.status) == (46162.29, "2026/2027", "found")


def test_page_failure_keeps_valuation(monkeypatch):
    monkeypatch.setattr(council, "property_page", Mock(side_effect=TimeoutError("simulated")))
    p = build(FLOODED).property
    assert p.cv.value == 340000 and p.land_use.value == "Recreational - vacant"
    assert (p.year_built.status, p.local_board.status, p.local_board.reason) == (
        "unavailable", "unavailable", "service_timeout")


def test_geomaps_down_falls_back_to_the_council_page(monkeypatch):
    monkeypatch.setattr(council, "rate_assessment", Mock(side_effect=ConnectionError("simulated")))
    ctx = build(FLOODED)
    p = ctx.property
    assert (p.cv.value, p.cv.valuation_date, p.cv.status, p.cv.source_id) == (
        340000, "2024-05-01", "found", "ac_property_page")
    assert (p.land_value.value, p.improvements_value.value, p.land_use.value, p.land_area_m2.value,
            p.legal_description.value, p.record_of_title.value, p.valuation_number.value) == (
        290000, 50000, "Recreational - vacant", 668, "Lot 1 DP 46871", "NA51D/614", "02270-00000087900")
    assert (p.annual_rates.reason, p.annual_rates.rating_year) == ("non_rateable_property", "2026/2027")
    # the page does not show these two
    assert (p.improvement_description.status, p.improvement_description.reason,
            p.improvement_description.source_id) == ("unavailable", "service_error:ConnectionError", "ac_valuation")
    assert p.rateability.status == "unavailable"
    assert p.local_board.value == "Puketapapa"
    assert next(l for l in ctx.links if l.id == "council_valuation").url.endswith("/12344049554.html")


def test_geomaps_and_fallback_both_down(monkeypatch):
    down = Mock(side_effect=TimeoutError("simulated"))
    monkeypatch.setattr(council, "rate_assessment", down)
    monkeypatch.setattr(council, "rate_account_key", down)
    p = build(FLOODED).property
    assert {p.cv.status, p.year_built.status, p.local_board.status} == {"unavailable"}
    assert p.cv.reason == "service_timeout" and p.title_type.value == "Freehold"


@pytest.mark.parametrize("label, expected", [("668 M2", 668), ("2.8668 HA", 28668), ("0 M2", 0), (None, None)])
def test_area_label(label, expected):
    assert council.area_m2(label) == expected


def test_pick_assessment_matches_the_street_number():
    # illustrative rows, not recorded data
    rows = [{"FORMATTEDADDRESS": "1 Example Road\rSuburb\rAuckland 1000", "RATEACCOUNTKEY": "1"},
            {"FORMATTEDADDRESS": "1A Example Road\rSuburb\rAuckland 1000", "RATEACCOUNTKEY": "2"}]
    assert council.pick_assessment(rows, "1A", "Example Road")["RATEACCOUNTKEY"] == "2"
    assert council.pick_assessment(rows, "3", "Example Road") is None
    assert council.pick_assessment(rows[:1], "1-5", "Example Road") == rows[0]  # one row: accept a range mismatch
    assert council.pick_assessment([], "1", "Example Road") is None


@pytest.mark.parametrize("category, expected", [
    ("RD197A", [1970, 1979]), ("RH192B", [1914, 1929]), ("RA191C", [None, 1913]), ("LI200B", [2000, 2009]),
    ("RDXXXB", None), ("OP", None), ("COCB", None), (None, None),
])
def test_decade_from_property_category(category, expected):
    assert council.decade_range(category) == expected


def test_other_council_fields():
    p = build(FLOODED).property
    got = {name: getattr(p, name).value for name in context._FACTS}
    assert got == {"land_value": 290000, "improvements_value": 50000, "rateability": "Non Rateable",
                   "improvement_description": "RESERVE LAND", "land_use": "Recreational - vacant",
                   "land_area_m2": 668, "legal_description": "Lot 1 DP 46871", "record_of_title": "NA51D/614",
                   "valuation_number": "02270-00000087900", "floor_area_m2": None,
                   "building_site_coverage_m2": 0, "local_board": "Puketapapa"}
    assert (p.floor_area_m2.status, p.floor_area_m2.reason) == ("not_found", "not_on_council_page")
    assert {getattr(p, name).source_id for name in context._LAYER_FACTS} == {"ac_valuation"}
    assert {getattr(p, name).source_id for name in context._PAGE_FACTS} == {"ac_property_page"}
    dry = build(DRY).property
    assert (dry.rateability.value, dry.land_area_m2.value, dry.floor_area_m2.value,
            dry.improvement_description.value) == ("Mixed Rateable", 3257, 12181, "CENTRAL CITY LIBRARY")


def test_year_built_not_found_for_non_residential_category():
    yb = build(FLOODED).property.year_built
    assert (yb.value, yb.status, yb.property_category) == (None, "not_found", "OP")


def test_context_matches_what_rules_api_reads():
    ctx = build(FLOODED)
    seen = RulesRequest.model_validate({"property_context": ctx.model_dump()}).property_context
    assert seen.property.cv.value == 340000 and seen.property.title_type.value == "Freehold"
    assert [h.layer for h in seen.hazards] == [c["layer"] for c in council.HAZARD_LAYERS]
    assert {l.id for l in ctx.links} == {"homes", "oneroof", "council_valuation", "council_flood_viewer",
                                         "council_geomaps", "nhc_claims_map"}
    ids = {s.id for s in ctx.sources}
    assert all(s.attribution and s.licence for s in ctx.sources)
    assert {h.source_id for h in ctx.hazards} <= ids and ctx.property.cv.source_id in ids


def test_timestamps_are_new_zealand_time_on_a_utc_host(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    ctx = build(FLOODED)
    monkeypatch.undo()
    time.tzset()
    nz = datetime.now(ZoneInfo("Pacific/Auckland"))
    assert datetime.fromisoformat(ctx.generated_at).utcoffset() == nz.utcoffset()
    assert ctx.sources[0].accessed_at == nz.date().isoformat()


def test_map_data_is_clipped_to_the_window_around_the_parcel():
    data = context.map_data(FLOODED)
    layers = {f["properties"]["layer"] for f in data["features"]}
    assert layers == {"address", "parcel", "flood_plain", "flood_prone_area", "overland_flow_path",
                      "landslide_shallow", "landslide_large_scale", "liquefaction"}
    assert data["unavailable"] == []
    graded = {f["properties"]["class"] for f in data["features"] if f["properties"]["layer"] == "liquefaction"}
    assert graded <= {"Very Low", "Unlikely", "Possible"} and graded
    window = box(*data["bbox"]).buffer(1e-9)
    assert all(window.contains(shape(f["geometry"])) for f in data["features"])
    assert client.get("/v1/map", params={"address_id": FLOODED}).status_code == 200


def test_http_endpoints():
    assert client.get("/health").json() == client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/v1/addresses", params={"q": "44 lorne"}).json()[0]["address_id"] == DRY
    body = client.get("/v1/property-context", params={"address_id": FLOODED}).json()
    assert body["schema_version"] == "0.1" and body["property"]["cladding"] == {
        "value": None, "status": "unavailable", "reason": "no_free_source", "source_id": None}
    assert client.get("/v1/property-context", params={"address_id": 0}).status_code == 422
