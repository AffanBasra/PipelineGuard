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

# Ordered by how much each one adds, so --cities on a slow link can stop early.
CITIES = [
    "Islamabad",     # sector forms (F-8/3, G-9/1) -- what §3 and §6.2 tested
    "Karachi",       # largest city; block and phase conventions
    "Lahore",        # cross-check against the 8,466 in the Kaggle export
    "Rawalpindi",
    "Faisalabad",
    "Multan",
    "Peshawar",
    "Quetta",
]

# addr:street is the anchor because every one of the 9,446 elements in the
# Kaggle export carries it, so it is the tag that actually selects addresses.
# `out tags` and not `out center tags`: no geometry is needed, and dropping it
# cuts the payload by roughly half.
#
# Matched on name:en, NOT name. Pakistani administrative boundaries in OSM are
# named in Urdu script -- Punjab is 'پنجاب', Islamabad is 'اسلام‌آباد' -- so
# area["name"="Islamabad"] resolves to nothing and the query returns 0 elements
# with a 200, which reads as "this city has no addresses" rather than as a bug.
QUERY = """
[out:json][timeout:180];
area["name:en"="{city}"]["boundary"="administrative"]->.searchArea;
(
  node["addr:street"](area.searchArea);
  way["addr:street"](area.searchArea);
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
                data={"data": QUERY.format(city=city)},
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
