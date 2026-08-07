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

# Cities whose plain name resolves to a point rather than a boundary.
DISTRICT_FALLBACK = {
    "Multan": "Multan District",
    "Quetta": "Quetta District",
    "Lahore": "Lahore District",
    "Rawalpindi": "Rawalpindi District",
    "Islamabad": "Islamabad Capital Territory",
}


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
    print(f"{'city':<13} {'ours (S,W,N,E)':<32} {'nominatim':<32} verdict")
    print("-" * 92)

    for city, box in CITY_BBOX.items():
        ours = tuple(float(v) for v in box.split(","))
        reference = nominatim_bbox(DISTRICT_FALLBACK.get(city, city))
        time.sleep(SLEEP)

        if reference is None:
            print(f"{city:<13} {box:<32} {'no boundary found':<32} SKIP")
            continue

        s, w, n, e = ours
        rs, rw, rn, re_ = reference
        covers = s <= rs + 0.02 and w <= rw + 0.02 and n >= rn - 0.02 and e >= re_ - 0.02
        failures += not covers
        mine = f"{s},{w},{n},{e}"
        ref = f"{rs:.2f},{rw:.2f},{rn:.2f},{re_:.2f}"
        print(f"{city:<13} {mine:<32} {ref:<32} "
              f"{'OK' if covers else 'CLIPS THE CITY'}")

    print()
    if failures:
        print(f"{failures} box(es) clip their city -- widen them in "
              f"fetch_osm_addresses.py and re-fetch with --refresh")
    else:
        print("all boxes contain their city")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
