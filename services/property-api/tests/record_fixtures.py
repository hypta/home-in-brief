"""Re-record the real upstream responses the tests replay. Needs LINZ_API_KEY and network.

    python tests/record_fixtures.py
"""
import json
import sys
import threading
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parents[2] / "shared")]

from app import context, council, http, linz  # noqa: E402
from app.models import PropertyContext  # noqa: E402

FIXTURES = HERE / "fixtures"
FLOODED, DRY = 2100210, 1132249  # public facilities: 740 Sandringham Road Extension; 44 Lorne Street
_live, _lock, _manifest = http.request, threading.Lock(), {}


def recording(method, url, params=None, *, as_json=True):
    body = _live(method, url, params, as_json=as_json)
    sig = http.signature(method, url, params)
    name = f"{sig}.{'json' if as_json else 'html'}"
    (FIXTURES / name).write_text(json.dumps(body, separators=(",", ":")) if as_json else body)
    with _lock:
        _manifest[sig] = {"method": method, "url": http.redact(url), "params": params or {},
                          "captured_at": date.today().isoformat(), "file": name}
    return body


FIXTURES.mkdir(exist_ok=True)
for old in FIXTURES.iterdir():
    old.unlink()
http.request = recording

linz.suggest("44 lorne")
for address_id in (FLOODED, DRY):
    ctx = PropertyContext.model_validate(context.build(address_id)).model_dump()
    name = ctx["address"]["matched"].split(",")[0].lower().replace(" ", "_")
    (HERE.parent / "fixtures" / f"property_context_{name}.json").write_text(json.dumps(ctx, indent=1) + "\n")
context.map_data(FLOODED)
a = linz.address(FLOODED)
council.rate_account_key(a["lat"], a["lng"], a["full_address_number"], a["full_road_name"])  # GeoMaps-down path
for cfg in council.HAZARD_LAYERS[:3]:  # point fallback
    council.hazard(cfg, None, a["lat"], a["lng"])
(FIXTURES / "manifest.json").write_text(json.dumps(_manifest, indent=1, sort_keys=True))
