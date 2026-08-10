"""Synthetic addresses in the generator, and the privacy line under them.

The place names are real (OpenStreetMap); the house numbers are not. That split
is the entire privacy argument for this module, so it is tested rather than
documented -- docs/decisions.md §1 requires the generator to generate, never to
replay a real address record.

The rest pins the properties measurement depends on: the address share of the
stream, and the presence of the Urdu structural forms that §3 measured a ~21
point coverage penalty on.
"""
from __future__ import annotations

import random
import re

import pytest

from pipelineguard.generator.addresses import (
    AREAS,
    DWELLING_WORDS,
    NAME_BEARING_PLACES,
    ROADS,
    STREET_WORDS,
    make_address,
)
from pipelineguard.generator.transactions import (
    ADDRESS_MEMO_RATE,
    ADDRESS_MEMO_TEMPLATES,
    make_memo,
)

SAMPLE = 2000


# --------------------------------------------------------------------------- #
# The privacy line: real places, synthetic numbers
# --------------------------------------------------------------------------- #
def test_every_city_has_both_areas_and_roads():
    """make_address indexes ROADS by city. A city present in AREAS but missing
    from ROADS raises KeyError on ~30% of draws -- a crash that only appears
    once the RNG happens to pick that city and that branch."""
    assert set(AREAS) == set(ROADS)
    assert all(AREAS[city] and ROADS[city] for city in AREAS)


def test_house_numbers_vary_across_draws():
    """The number is what identifies a dwelling. If it were drawn from a fixed
    vocabulary the module would be replaying real address records, which §1 of
    decisions.md forbids."""
    rng = random.Random(4)
    numbers = {make_address(rng).split(",")[0].split()[-1] for _ in range(400)}
    assert len(numbers) > 100, f"only {len(numbers)} distinct house numbers"


def test_place_names_are_drawn_only_from_the_vendored_vocabulary():
    """The converse of the test above, and the half that keeps the module
    honest: every non-numeric component must come from a list a human curated,
    so no real address record can appear by way of a code path nobody reviewed.
    """
    known = {p for places in AREAS.values() for p in places}
    known |= {r for roads in ROADS.values() for r in roads}
    known |= set(AREAS)

    rng = random.Random(5)
    for _ in range(300):
        parts = [p.strip() for p in make_address(rng).split(",")]
        # parts[0] is the dwelling word plus the random number; the remainder
        # must be either a street number or vendored vocabulary.
        for part in parts[1:]:
            street_form = any(
                re.fullmatch(rf"{word} \d+", part) for word in STREET_WORDS
            )
            assert street_form or part in known, f"unvendored component {part!r}"


def test_no_generated_address_carries_a_person_name():
    """Two reasons, and they point the same way.

    Privacy: OSM's raw data holds building names like 'Qadri Manzil' that can
    embed a person, and the corpus builder's filter does not apply to vocabulary
    a human transcribed by hand.

    Measurement: a surname inside an address is the exact confound §13.2 found,
    where ADDRESS labels re-tagged a name the PERSON labels had already claimed.
    This caught 'Dhoke Kala Khan' — a real Rawalpindi locality, no living person
    involved, and still unusable here because 'Khan' is in the generator's own
    surname pool.
    """
    from pipelineguard.generator.transactions import FIRST_NAMES, LAST_NAMES

    names = {n.lower() for n in FIRST_NAMES + LAST_NAMES}
    places = [p for group in AREAS.values() for p in group]
    places += [r for group in ROADS.values() for r in group]

    for place in places:
        carries = {n for n in names if re.search(rf"\b{n}\b", place.lower())}
        assert not carries or place in NAME_BEARING_PLACES, (
            f"{place!r} carries {sorted(carries)}; scrub it, or add it to "
            f"NAME_BEARING_PLACES with the reason"
        )


def test_the_name_bearing_allowlist_has_no_dead_entries():
    """An allowlist that outlives the entry it excused stops being a review
    record and starts being a hole."""
    places = {p for group in AREAS.values() for p in group}
    places |= {r for group in ROADS.values() for r in group}
    assert NAME_BEARING_PLACES <= places


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_address_starts_with_a_dwelling_word_and_a_number():
    rng = random.Random(6)
    for _ in range(200):
        head = make_address(rng).split(",")[0]
        assert any(head.startswith(word) for word in DWELLING_WORDS), head
        assert re.search(r"\d", head), head


def test_city_is_always_the_last_component():
    """The span-extension rule (§17) extends right to the city, so the city has
    to be terminal for that rule to have anything to anchor on."""
    rng = random.Random(7)
    for _ in range(200):
        assert make_address(rng).rsplit(", ", 1)[-1] in AREAS


def test_urdu_structural_forms_are_actually_emitted():
    """§3 measured encoders losing ~21 points of coverage on Ghar/Gali/Makan
    against House/Street/Flat. A generator that only produced the English forms
    would leave the pipeline's weakest measured case untested."""
    rng = random.Random(8)
    addresses = [make_address(rng) for _ in range(SAMPLE)]
    urdu = sum(
        any(re.search(rf"\b{word}\b", a) for word in ("Ghar", "Makan", "Gali"))
        for a in addresses
    )
    assert urdu / SAMPLE > 0.20, f"only {urdu / SAMPLE:.1%} carried an Urdu form"


# --------------------------------------------------------------------------- #
# The share of the stream that carries an address
# --------------------------------------------------------------------------- #
def test_address_rate_bounds_are_exact():
    """Both ends absolute, matching the blank-rate test: 0.0 is what a run uses
    to reproduce the pre-address stream, 1.0 forces every memo to carry one."""
    random.seed(12)
    assert not any(_has_address(make_memo(0.0, 0.0)) for _ in range(300))
    assert all(_has_address(make_memo(0.0, 1.0)) for _ in range(300))


def test_default_address_rate_is_honoured():
    """Applies to NON-BLANK memos, so it is measured against those rather than
    against the whole stream — a rate silently applied to all transactions would
    read as ~18% here and pass a looser assertion."""
    random.seed(13)
    memos = [m for m in (make_memo() for _ in range(SAMPLE)) if m]
    observed = sum(_has_address(m) for m in memos) / len(memos)
    assert abs(observed - ADDRESS_MEMO_RATE) < 0.05, (
        f"observed {observed:.1%} vs configured {ADDRESS_MEMO_RATE:.1%}"
    )


def test_blank_rate_still_wins_over_the_address_rate():
    """The blank draw happens first. If the two were independent, asking for a
    fully blank stream would still emit address memos and every throughput
    number taken with --blank-memo-rate 1.0 would be wrong."""
    random.seed(14)
    assert not any(make_memo(1.0, 1.0) for _ in range(300))


@pytest.mark.parametrize("template", ADDRESS_MEMO_TEMPLATES)
def test_every_address_template_renders(template):
    """A template naming a placeholder make_memo does not pass raises KeyError
    on the draw that selects it, which is a crash the default rate would hide
    for thousands of records."""
    random.seed(15)
    rendered = template.format(
        name="Ayesha Malik", phone="03001234567", cnic="35202-1234567-8",
        email="a@b.pk", inv=1234, address=make_address(random.Random(1)),
    )
    assert "{" not in rendered


def _has_address(memo: str) -> bool:
    return any(re.search(rf"\b{word}\b", memo) for word in DWELLING_WORDS)
