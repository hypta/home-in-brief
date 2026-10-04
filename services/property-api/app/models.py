"""PropertyContext (spec 6.2). Extends the subset in b_common.schemas that rules-api reads."""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel

from b_common import schemas as s

Status = Literal["found", "not_found", "unavailable"]


class _Meta(BaseModel):
    """Every looked-up field says whether it was found and, if not, why."""

    status: Status
    reason: Optional[str] = None
    source_id: Optional[str] = None


class CV(_Meta, s.CV):
    pass


class LastSale(_Meta):
    price: Optional[float] = None
    date: Optional[str] = None


class AnnualRates(_Meta):
    value: Optional[float] = None
    rating_year: Optional[str] = None


class TitleType(_Meta, s.StrField):
    linz_types: list[str] = []  # title types exactly as LINZ records them


class YearBuilt(_Meta, s.IntField):
    # the council records a decade: value is its first year
    decade_range: Optional[list[Optional[int]]] = None
    property_category: Optional[str] = None


class Cladding(_Meta, s.StrField):
    pass


class Fact(_Meta):
    """One value from the council rate assessment or property page."""

    value: Optional[Union[float, str]] = None


class Property(s.Property):
    cv: CV
    last_sale: LastSale
    annual_rates: AnnualRates
    title_type: TitleType
    year_built: YearBuilt
    cladding: Cladding
    # rules-api does not read these
    land_value: Fact
    improvements_value: Fact
    rateability: Fact              # the council's wording, e.g. "Fully Rateable"
    improvement_description: Fact  # e.g. "TOWN HOUSE"
    land_use: Fact
    land_area_m2: Fact
    floor_area_m2: Fact
    building_site_coverage_m2: Fact
    legal_description: Fact
    record_of_title: Fact
    valuation_number: Fact
    local_board: Fact


class Hazard(s.Hazard):
    geometry: Literal["polygon", "line"]
    overlap_pct: Optional[float] = None       # polygon layers, parcel method only
    crosses_parcel: Optional[bool] = None     # line layers
    classes: list[str] = []                   # class values found on the parcel (graded / zoned layers)
    susceptibility: Optional[str] = None      # highest landslide class on the parcel
    data_date: Optional[str] = None
    reason: Optional[str] = None
    source_id: str


class Address(_Meta):
    input: Optional[str] = None
    matched: Optional[str] = None
    linz_address_id: Optional[int] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    source_id: str = "linz_addresses"


class Parcel(_Meta):
    parcel_id: Optional[int] = None
    spatial_method: Literal["parcel", "point"]
    source_id: str = "linz_parcels"


class Link(BaseModel):
    id: str
    label: str
    url: str
    prefilled: bool = False  # the URL carries this property or address
    # direct: the property's page; first_result: the first web search result on that site; home: its front page
    kind: Literal["direct", "first_result", "home"] = "home"


class Source(s.Source):
    attribution: str  # the credit line the report must show for this source


class PropertyContext(s.PropertyContext):
    schema_version: str = s.SCHEMA_VERSION
    generated_at: str
    address: Address
    parcel: Parcel
    hazards: list[Hazard]
    property: Property
    links: list[Link]
    sources: list[Source]


class AddressSuggestion(BaseModel):
    address_id: int
    full_address: str
