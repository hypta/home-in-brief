"""Structured extraction from page text. The extractor is injectable for tests."""
import os
from typing import Callable

from pydantic import BaseModel

TERMS = ("6m", "1y", "18m", "2y", "3y", "4y", "5y")
DEFAULT_MODEL = "gemini-2.5-flash"  # override with GEMINI_MODEL
MAX_CHARS = 80_000


class RateItem(BaseModel):
    term: str  # one of TERMS
    rate_pct: float


class Band(BaseModel):
    lvr_from: float
    lvr_to: float
    margin_pct: float


class Extracted(BaseModel):
    special: list[RateItem] = []
    standard: list[RateItem] = []
    floating: float | None = None
    bands: list[Band] = []
    page_effective_date: str | None = None


# (page_text, bank_cfg) -> Extracted
Extractor = Callable[[str, dict], Extracted]

PROMPT = """You are extracting NZ residential mortgage rates for {name} from page text.
Return:
- special: fixed-rate "special" (a.k.a. "special offer" / "low deposit-free" rates for owner-occupiers with >=20% equity) per term. term must be one of 6m, 1y, 18m, 2y, 3y, 4y, 5y. rate_pct is a percent number (e.g. 4.89).
- standard: the standard (card) fixed rates per term, if shown.
- floating: the variable / floating home loan rate, if shown.
- bands: low-equity margin bands (Low Equity Margin or low equity premium) as lvr_from, lvr_to, margin_pct (e.g. 80.01, 85, 0.30). Empty if none shown.
- page_effective_date: the date the rates are stated as effective, as written, or null.
Only use numbers that appear in the text. Omit anything not present. Owner-occupier rates only.
Ignore products restricted to first home buyers (e.g. "FirstHome Combo", "First Home Buyer special"), revolving credit,
investment, reverse equity, "Fresh Start", bridging, construction, term deposits, savings and personal loans.
If the page lists floating rates for both special (20%+ equity) and standard (<20%) borrowers, floating is the special one.

PAGE TEXT:
{text}
"""


def gemini_extractor(text: str, bank: dict) -> Extracted:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        contents=PROMPT.format(name=bank["name"], text=text[:MAX_CHARS]),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Extracted,
            temperature=0,
        ),
    )
    if resp.parsed is not None:
        return resp.parsed
    return Extracted.model_validate_json(resp.text)
