"""Fetch real Pakistani addresses from the Overpass API, one city at a time.

docs/tier2-detection-findings.md §5 records the softest point under the address
finding: the author wrote the test addresses. §9 closed the vocabulary half of
that for Roman Urdu. This closes the address half, with data OpenStreetMap
contributors wrote.

Cities are chosen so the forms §3 and §6.2 actually measured are represented.
Islamabad matters most -- its sector addresses (F-8/3, G-9/1) are what those
sections tested, and a Lahore-only corpus cannot speak to them.

PRIVACY. OSM carries real personal data: a `building=house` node can have
`name="Muhammad Ibrahim"`, and ~3% of elements carry a phone number. This script
stores the raw response, and build_address_corpus.py is where the addr:* filter
is applied. The raw cache is gitignored and must stay that way.

LICENCE. OSM data is ODbL. Attribution is written into every cache file rather
than left to a README nobody reads.

Overpass is free and volunteer-run. This script caches every city and re-runs
cost zero requests; do not remove that.

Usage:
    python scripts/fetch_osm_addresses.py                     # all cities
    python scripts/fetch_osm_addresses.py --cities Islamabad
    python scripts/fetch_osm_addresses.py --refresh           # ignore cache
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO / "data" / "osm-addresses" / "raw"

# Tried in order. The main instance returns 504 under load often enough that a
# single-endpoint script fails for reasons that have nothing to do with the
# query, which is indistinguishable from a bug when you are debugging one.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Overpass asks clients to identify themselves so it can contact the operator of
# a misbehaving script rather than silently ban the IP range.
USER_AGENT = (
    "PipelineGuard/0.1 (PII detection research; "
    "https://github.com/AffanBasra/PipelineGuard)"
)

ATTRIBUTION = (
    "Data (c) OpenStreetMap contributors, ODbL 1.0. "
    "https://www.openstreetmap.org/copyright"
)

# Bounding boxes, not named areas. Three approaches were tried against the live
# API and only this one works:
#
#   area["name"=...]                -> 0 elements; PK boundaries are Urdu-script
#   area["name:en"=...]             -> resolves, but also matches three
#                                      'Islomobod' hamlets in Central Asia, and
#                                      the address query inside it times out
#   area(<explicit id>)             -> 504; scanning an admin area for every
#                                      addr:street node is too expensive
#   bbox                            -> 5,320 elements for Islamabad in seconds
#
# Boxes are generous and overlap slightly (Islamabad's includes part of
# Rawalpindi). That is harmless: addr:city is preserved, and the corpus builder
# deduplicates.
# south,west,north,east. Taken from Nominatim's administrative boundary for each
# city, widened slightly, NOT written from memory -- the first version was and
# six of the eight boxes clipped the city. Verify with
# scripts/verify_bboxes.py after any change.
#
# Multan and Quetta resolve to a point in Nominatim's city search (it matches an
# office node and a railway station), so their district boundary is used
# instead. That is wider than the city, which costs some precision in the
# addr:city column and no correctness -- addr:city is preserved per record.
CITY_BBOX = {
    # sector forms (F-8/3, G-9/1) -- what §3 and §6.2 tested
    "Islamabad": "33.46,72.80,33.82,73.39",
    "Karachi": "24.42,66.28,25.68,67.59",
    # cross-check against the 8,466 in the Kaggle export
    "Lahore": "31.18,73.99,31.73,74.67",
    "Rawalpindi": "33.06,72.61,33.89,73.66",
    "Faisalabad": "31.33,72.99,31.53,73.20",
    "Multan": "29.41,71.00,30.46,71.84",
    "Peshawar": "33.91,71.36,34.08,71.63",
    "Quetta": "29.80,66.22,30.49,67.29",
}
CITIES = list(CITY_BBOX)

# addr:street is the anchor because every one of the 9,446 elements in the
# Kaggle export carries it. `out tags` and not `out center tags`: no geometry is
# needed, and dropping it roughly halves the payload.
QUERY = """
[out:json][timeout:180];
(
  node["addr:street"]({bbox});
  way["addr:street"]({bbox});
);
out tags;
"""

SLEEP_BETWEEN_CITIES = 5.0
MAX_ATTEMPTS = 4


def fetch_city(city: str, timeout: float = 300.0) -> dict:
    """One city, rotating mirrors and backing off. Raises after MAX_ATTEMPTS.

    429 means rate-limited and 504 means the server gave up mid-query. Both are
    worth retrying against a different mirror; anything else is a bug in the
    query, and retrying that just wastes someone else's capacity.
    """
    delay = 10.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        mirror = OVERPASS_MIRRORS[(attempt - 1) % len(OVERPASS_MIRRORS)]
        try:
            response = requests.post(
                mirror,
                data={"data": QUERY.format(bbox=CITY_BBOX[city])},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            print(f"    {mirror.split('/')[2]}: {type(exc).__name__}", flush=True)
            response = None

        if response is not None:
            if response.status_code == 200:
                return response.json()
            if response.status_code not in (429, 504, 502, 503):
                response.raise_for_status()
            print(f"    {mirror.split('/')[2]}: HTTP {response.status_code}",
                  flush=True)

        if attempt < MAX_ATTEMPTS:
            print(f"    retry {attempt + 1}/{MAX_ATTEMPTS} in {delay:.0f}s",
                  flush=True)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"{city}: gave up after {MAX_ATTEMPTS} attempts")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cities", nargs="*", default=CITIES,
                    help=f"default: {' '.join(CITIES)}")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch even if a cache file exists")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    args = ap.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    fetched = 0
    for i, city in enumerate(args.cities):
        path = cache_dir / f"{city.lower()}.json"
        if path.exists() and not args.refresh:
            n = len(json.loads(path.read_text(encoding="utf-8"))["elements"])
            print(f"{city:<14} cached ({n:,} elements)", flush=True)
            continue

        # Sleep before each request except the first, so a run that is entirely
        # cache hits costs no time at all.
        if fetched:
            time.sleep(SLEEP_BETWEEN_CITIES)

        print(f"{city:<14} fetching...", flush=True)
        t0 = time.perf_counter()
        payload = fetch_city(city)
        payload["_attribution"] = ATTRIBUTION
        payload["_city_queried"] = city
        path.write_text(json.dumps(payload), encoding="utf-8")
        fetched += 1
        print(f"{city:<14} {len(payload['elements']):,} elements in "
              f"{time.perf_counter() - t0:.0f}s -> {path.name}", flush=True)

    print(f"\n{fetched} fetched, {len(args.cities) - fetched} from cache")
    print(ATTRIBUTION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
