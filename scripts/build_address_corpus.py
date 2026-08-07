"""Turn raw OSM output into an address corpus this project is allowed to hold.

docs/tier2-detection-findings.md §5 records the softest point under the address
finding: the author wrote the test addresses. This builds a replacement from
addresses OpenStreetMap contributors wrote.

PRIVACY. This is the control, and it is the reason the module exists as a
separate step rather than being folded into the probe. Raw OSM carries real
personal data: a `building=house` node can have `name="Muhammad Ibrahim"`, and
~3% of elements carry a phone number. `docs/decisions.md` §1 forbids processing
real personal data.

    Only `addr:*` keys are read. Every other key is discarded, never copied,
    never written out.

An address with no occupant named is not personal data. A `name` tag might be a
person. The whitelist is asserted in tests/test_address_corpus.py rather than
left as a convention, because a convention degrades silently.

Reads either the Overpass API responses written by fetch_osm_addresses.py or a
Kaggle export of the same query -- identical schema, so one reader handles both.

Usage:
    python scripts/build_address_corpus.py --source "<path to .txt or .json>"
    python scripts/build_address_corpus.py --source data/osm-addresses/raw
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "data" / "osm-addresses"

# The only keys ever read. Anything absent from this set cannot reach the output.
ADDR_PREFIX = "addr:"

# Case and script variants seen in the Kaggle export: 'lahore', 'LAHORE',
# 'لاہور'. Left unnormalised these split one city across four rows in the
# composition report and make the corpus look more diverse than it is.
CITY_ALIASES = {
    "lahore": "Lahore",
    "لاہور": "Lahore",
    "کراچی": "Karachi",
    "اسلام آباد": "Islamabad",
    "اسلام‌آباد": "Islamabad",
    "shahdara": "Shahdara",
}

# Structural vocabulary that marks a planned-development address. These are the
# forms §3 found encoders lose ground on, so the corpus is classified by them.
BLOCK_FORM = re.compile(
    r"\b(block|blk|phase|ph|sector|sec|society|colony|town|scheme)\b", re.I
)
SECTOR_CODE = re.compile(r"\b[A-Z]-\d+(/\d+)?\b")

# A digit run this long in an address field is a phone number or an ID that
# slipped into the wrong tag, not a house number. Exactly one of the Kaggle
# export's 9,446 elements trips this.
LONG_DIGIT_RUN = re.compile(r"\d{7,}")


def load_elements(source: Path) -> list[dict]:
    """Elements from one Overpass JSON file, or every .json in a directory."""
    files = sorted(source.glob("*.json")) if source.is_dir() else [source]
    elements = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        elements.extend(payload.get("elements", []))
    return elements


def addr_tags(element: dict) -> dict[str, str]:
    """The privacy control. Only `addr:*` keys survive this function.

    Written as a whitelist rather than a blocklist on purpose: a blocklist has
    to anticipate every key OSM might add, and silently leaks the one nobody
    thought of.
    """
    return {
        key[len(ADDR_PREFIX):]: value
        for key, value in element.get("tags", {}).items()
        if key.startswith(ADDR_PREFIX) and isinstance(value, str) and value.strip()
    }


def normalise_city(city: str) -> str:
    return CITY_ALIASES.get(city.strip().lower(), city.strip())


def compose(tags: dict[str, str]) -> str | None:
    """One address string, in the order Pakistani addresses are written.

    Deliberately does NOT clean the values. The Kaggle export contains
    '8km', '19 - Level 10' and doubled spaces; that mess is what a real
    detector meets, and normalising it away would rebuild the tidy hand-written
    corpus this work exists to replace.
    """
    if "full" in tags:
        parts = [tags["full"]]
    else:
        street = tags.get("street")
        if not street:
            return None
        parts = [p for p in (tags.get("housenumber"), street) if p]

    # 101 street values in the Kaggle export already end in the city --
    # 'Sector F Dha Phase 1, Lahore, Pakistan'. Appending addr:city to those
    # produced 'Lahore, Pakistan, Lahore', which is not an address anyone wrote
    # and would have been scored as one.
    city = normalise_city(tags.get("city", ""))
    joined = ", ".join(parts)
    if city and city.lower() not in joined.lower():
        parts.append(city)

    address = ", ".join(parts)
    # Never emit a value carrying what looks like a phone number or long ID.
    return None if LONG_DIGIT_RUN.search(address) else address


def classify(address: str) -> str:
    if SECTOR_CODE.search(address):
        return "sector_code"     # F-8/3, G-9/1 -- the Islamabad convention
    if BLOCK_FORM.search(address):
        return "block_phase"     # Model Town, DHA Phase 3, Johar Town
    return "plain_street"


def build(elements: list[dict]) -> list[dict]:
    """Deduplicated address records, each with its city and form."""
    seen: set[str] = set()
    records = []
    for element in elements:
        tags = addr_tags(element)
        address = compose(tags)
        # Overpass returns a node AND a way for the same building, and the
        # Kaggle export repeats 'Model Town Block G' six times. Without this the
        # evaluation silently weights toward whatever repeats most.
        if not address or address in seen:
            continue
        seen.add(address)
        records.append({
            "address": address,
            "city": normalise_city(tags.get("city", "")) or "(unknown)",
            "form": classify(address),
            "has_housenumber": "housenumber" in tags,
        })
    return records


def report(elements: list[dict], records: list[dict]) -> None:
    print(f"elements read      : {len(elements):,}")
    print(f"addresses composed : {len(records):,} "
          f"(deduplicated from {len(elements):,})")

    print("\nby city:")
    for city, n in Counter(r["city"] for r in records).most_common(10):
        print(f"  {city:<24} {n:>6,}")

    print("\nby form:")
    for form, n in Counter(r["form"] for r in records).most_common():
        print(f"  {form:<24} {n:>6,}  {n / len(records):>5.1%}")

    with_num = sum(r["has_housenumber"] for r in records)
    print(f"\nwith house number  : {with_num:,} ({with_num / len(records):.1%})")

    print("\nsamples:")
    for form in ("sector_code", "block_phase", "plain_street"):
        matches = [r["address"] for r in records if r["form"] == form][:3]
        for address in matches:
            print(f"  [{form:<12}] {address}")
        if not matches:
            print(f"  [{form:<12}] (none)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True,
                    help="Overpass JSON file, or a directory of them")
    ap.add_argument("--out", default=str(OUT_DIR / "addresses.json"))
    args = ap.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        print(f"no such source: {source}", file=sys.stderr)
        return 1

    elements = load_elements(source)
    records = build(elements)
    if not records:
        print("no addresses composed -- wrong file, or the query returned none",
              file=sys.stderr)
        return 1

    report(elements, records)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "attribution": ("Data (c) OpenStreetMap contributors, ODbL 1.0. "
                        "https://www.openstreetmap.org/copyright"),
        "source": str(source),
        "count": len(records),
        "addresses": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
