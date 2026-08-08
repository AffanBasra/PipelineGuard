"""Check the fetcher's bounding boxes actually contain their cities.

Written after the first set was produced from memory and six of eight boxes
clipped the city they were supposed to cover -- a failure that is invisible in
the output, because a too-small box returns fewer addresses rather than an
error.

Compares CITY_BBOX against the administrative boundary Nominatim reports.
Nominatim's own city lookup returns a point for Multan and Quetta (it matches an
office node and a railway station), so those fall back to the district query.

Usage:
    python scripts/verify_bboxes.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_osm_addresses import CITY_BBOX  # noqa: E402

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = (
    "PipelineGuard/0.1 (PII detection research; "
    "https://github.com/AffanBasra/PipelineGuard)"
)
# Nominatim asks for at most one request per second.
SLEEP = 1.2

# Cities whose plain name does not resolve to a usable boundary: Nominatim
# returns a point for Multan and Quetta (an office node and a railway station),
# and nothing in the top results for Lahore and Rawalpindi.
#
# These references are DISTRICTS, which are larger than the city a box targets.
# So the check is containment of the city's core, not of the whole district --
# demanding the latter would fail boxes that are correct for their purpose.
DISTRICT_FALLBACK = {
    "Multan": "Multan District",
    "Quetta": "Quetta District",
    "Lahore": "Lahore District",
    "Rawalpindi": "Rawalpindi District",
    "Islamabad": "Islamabad Capital Territory",
}

# Fraction of the reference box each side must cover. A district reference is
# deliberately wider than the city, so requiring full containment would reject
# correct boxes; requiring the centre plus most of the span catches the real
# failure, which is a box centred wrong or far too small.
MIN_OVERLAP = 0.55


def nominatim_bbox(query: str) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) of the first administrative boundary match."""
    response = requests.get(
        NOMINATIM,
        params={"q": f"{query}, Pakistan", "format": "json", "limit": 5},
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    response.raise_for_status()
    for result in response.json():
        south, north, west, east = (float(v) for v in result["boundingbox"])
        # A point result has no span and would "fit" inside any box, which is
        # the exact false pass this script exists to catch.
        if max(north - south, east - west) < 0.02:
            continue
        if result.get("class") == "boundary":
            return south, west, north, east
    return None


def main() -> int:
    failures = 0
    skipped = 0
    print(f"{'city':<13} {'ours (S,W,N,E)':<32} {'nominatim':<32} verdict")
    print("-" * 92)

    for city, box in CITY_BBOX.items():
        ours = tuple(float(v) for v in box.split(","))
        reference = nominatim_bbox(DISTRICT_FALLBACK.get(city, city))
        time.sleep(SLEEP)

        if reference is None:
            skipped += 1
            print(f"{city:<13} {box:<32} {'no boundary found':<32} SKIP")
            continue

        s, w, n, e = ours
        rs, rw, rn, re_ = reference

        # Must contain the reference centre, and cover most of its span.
        centre_lat, centre_lon = (rs + rn) / 2, (rw + re_) / 2
        holds_centre = s <= centre_lat <= n and w <= centre_lon <= e
        lat_overlap = max(0.0, min(n, rn) - max(s, rs)) / max(rn - rs, 1e-9)
        lon_overlap = max(0.0, min(e, re_) - max(w, rw)) / max(re_ - rw, 1e-9)
        covers = (holds_centre
                  and lat_overlap >= MIN_OVERLAP and lon_overlap >= MIN_OVERLAP)

        failures += not covers
        mine = f"{s},{w},{n},{e}"
        ref = f"{rs:.2f},{rw:.2f},{rn:.2f},{re_:.2f}"
        note = "OK" if covers else (
            "CENTRE OUTSIDE BOX" if not holds_centre
            else f"COVERS {min(lat_overlap, lon_overlap):.0%} OF SPAN")
        print(f"{city:<13} {mine:<32} {ref:<32} {note}")

    checked = len(CITY_BBOX) - skipped
    print()
    print(f"checked {checked} of {len(CITY_BBOX)}; {failures} failed, "
          f"{skipped} unverified")
    if failures:
        print("widen the failing boxes in fetch_osm_addresses.py, "
              "then re-fetch with --refresh")
    # A skip is not a pass. Exiting 0 with three of eight verified would make
    # this script's endorsement worthless, and section 15.1 leans on it.
    return 1 if (failures or skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
