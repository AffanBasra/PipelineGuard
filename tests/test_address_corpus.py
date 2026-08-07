"""The OSM address corpus, and the privacy control on it.

Most of this file guards one property: raw OpenStreetMap carries real personal
data, and none of it may reach the corpus. A `building=house` node can hold
`name="Muhammad Ibrahim"`, and roughly 3% of elements carry a phone number.
docs/decisions.md section 1 forbids processing real personal data.

The control is a whitelist -- only `addr:*` keys are read. It is tested rather
than documented because a convention degrades silently, and the failure mode is
a privacy tool that quietly stores someone's name.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package and is not on pytest's path. The probe scripts do
# the same insert for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_address_corpus import (  # noqa: E402
    addr_tags,
    build,
    classify,
    classify_kind,
    compose,
    normalise_city,
    safe_meta,
)


def node(**tags):
    return {"type": "node", "id": 1, "tags": tags}


# --------------------------------------------------------------------------- #
# The privacy control
# --------------------------------------------------------------------------- #
def test_personal_tags_never_survive_extraction():
    """The exact shape found in the real file: a house with an occupant's name
    and a phone number on it."""
    element = node(**{
        "addr:housenumber": "94-Q",
        "addr:street": "Model Town",
        "addr:city": "Lahore",
        "name": "Muhammad Ibrahim",
        "phone": "+92 300 1234567",
        "building": "house",
        "operator": "Some Person",
    })
    tags = addr_tags(element)

    assert set(tags) == {"housenumber", "street", "city"}
    flattened = " ".join(tags.values())
    assert "Muhammad Ibrahim" not in flattened
    assert "1234567" not in flattened


@pytest.mark.parametrize(
    "key",
    ["name", "name:ur", "name:en", "phone", "operator", "contact:phone",
     "email", "website", "description", "note", "building", "amenity"],
)
def test_only_addr_prefixed_keys_are_read(key):
    """A whitelist, not a blocklist. A blocklist has to anticipate every key OSM
    might add, and leaks the one nobody thought of."""
    tags = addr_tags(node(**{key: "sensitive", "addr:street": "Mall Road"}))
    assert list(tags) == ["street"]


def test_long_digit_runs_are_dropped():
    """A phone number in the wrong tag. Exactly one of the real file's 9,446
    elements trips this, which is why it is a rule and not a spot fix."""
    assert compose({"street": "Mall Road", "housenumber": "03001234567"}) is None
    assert compose({"street": "Mall Road", "housenumber": "94-Q"}) is not None


def test_build_output_carries_no_unexpected_keys():
    """Guards the written artifact, not just the extractor."""
    records = build([node(**{
        "addr:street": "Mall Road", "addr:city": "Lahore",
        "name": "Private Residence", "phone": "03001234567",
    })])
    assert len(records) == 1
    assert set(records[0]) == {
        "address", "city", "form", "kind", "has_housenumber", "latin",
    }
    assert "Private Residence" not in records[0]["address"]


# --------------------------------------------------------------------------- #
# The one deliberate exception to the whitelist
# --------------------------------------------------------------------------- #
def test_safe_meta_admits_building_and_nothing_else():
    """`building` describes a structure, not a person, and it is what makes the
    residential/commercial split reportable. It is the only exception, and it
    lives outside addr_tags so the address whitelist stays a single rule."""
    meta = safe_meta(node(**{
        "building": "house",
        "name": "Muhammad Ibrahim",
        "phone": "03001234567",
        "operator": "Someone",
        "addr:street": "Mall Road",
    }))
    assert meta == {"building": "house"}


def test_addr_tags_still_excludes_building():
    """The exception must not leak back into the address whitelist."""
    assert addr_tags(node(**{"building": "house", "addr:street": "X"})) == {
        "street": "X"
    }


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"building": "house"}, "residential"),
        ({"building": "apartments"}, "residential"),
        ({"building": "commercial"}, "commercial"),
        ({"building": "hospital"}, "institutional"),
        # A POI marker makes it a business even with no building type.
        ({"shop": "bakery"}, "commercial"),
        ({"amenity": "bank"}, "commercial"),
        # 'yes' means "a building" and nothing more. Guessing would invent data.
        ({"building": "yes"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_kind_classification(tags, expected):
    assert classify_kind(node(**tags)) == expected


def test_unknown_is_the_honest_majority():
    """78.7% of the real corpus is unknown, because OSM contributors map the
    outline and skip the type. Forcing those into a bucket would manufacture a
    residential/commercial split that the data does not support."""
    elements = [node(**{"addr:street": f"Road {i}", "building": "yes"})
                for i in range(10)]
    assert {r["kind"] for r in build(elements)} == {"unknown"}


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
def test_city_is_not_appended_when_already_present():
    """101 street values in the real file already end in the city. Appending it
    produced 'Lahore, Pakistan, Lahore' -- not an address anyone wrote, and it
    would have been scored as one."""
    address = compose({"street": "Sector F Dha Phase 1, Lahore, Pakistan",
                       "city": "Lahore"})
    assert address == "Sector F Dha Phase 1, Lahore, Pakistan"
    assert address.lower().count("lahore") == 1


def test_messy_values_are_preserved():
    """The real file holds '8km' and '19 - Level 10' as house numbers. Cleaning
    them would rebuild the tidy hand-written corpus this work replaces."""
    assert compose({"housenumber": "8km", "street": "Multan Road"}) == \
        "8km, Multan Road"
    assert compose({"housenumber": "19 - Level 10",
                    "street": "346-B Ferozepur Road"}) == \
        "19 - Level 10, 346-B Ferozepur Road"


def test_street_is_required():
    """addr:street anchors the query, so an element without one is not an
    address record."""
    assert compose({"city": "Lahore", "housenumber": "12"}) is None


def test_duplicates_are_collapsed():
    """Overpass returns a node and a way for the same building. In the real file
    Karachi is one street repeated 252 times, so without this the corpus would
    look far more diverse than it is."""
    elements = [node(**{"addr:street": "Nazimabad 5", "addr:city": "Karachi"})
                for _ in range(252)]
    assert len(build(elements)) == 1


@pytest.mark.parametrize(
    "city, expected",
    [
        ("lahore", "Lahore"), ("LAHORE", "Lahore"), ("Lahore", "Lahore"),
        # Urdu-script city names, which is how most OSM records write them.
        ("لاہور", "Lahore"), ("اسلام آباد", "Islamabad"),
        ("راولپنڈی", "Rawalpindi"),
        # Values carrying province and country too.
        ("Rawalpindi, Punjab, Pakistan", "Rawalpindi"),
        ("Lahore, Pakistan", "Lahore"),
        ("Multan", "Multan"),
    ],
)
def test_city_variants_normalise(city, expected):
    assert normalise_city(city) == expected


def test_unknown_cities_pass_through_unchanged():
    """Normalisation must not invent a match. A city not in the alias table or
    the prefix list keeps its own name rather than being folded into one."""
    assert normalise_city("Sialkot") == "Sialkot"
    assert normalise_city("Gujranwala") == "Gujranwala"


@pytest.mark.parametrize(
    "address, form",
    [
        ("364, E-1 Society, Johar Town, Lahore", "sector_code"),
        ("House 12, Street 4, F-8/3 Islamabad", "sector_code"),
        ("St 29 Sec W Ph 3, Lahore", "block_phase"),
        ("94-Q, Model Town, Lahore", "block_phase"),
        ("Noor Jahan Road, Lahore", "plain_street"),
    ],
)
def test_form_classification(address, form):
    assert classify(address) == form
