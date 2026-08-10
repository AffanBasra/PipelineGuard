"""Synthetic Pakistani addresses, built from real place names.

WHY THIS EXISTS. §13 removed ADDRESS from Tier 2 because nothing in the stream
contained an address, which made the capability untestable end to end and the
fine-tuning question unanswerable. This gives ADDRESS a field.

WHAT IS REAL AND WHAT IS NOT. The place names below -- neighbourhoods, blocks,
sectors, roads -- are taken from the OpenStreetMap corpus
(scripts/build_address_corpus.py). A place name is geography, not personal data:
'FB Area Block 3' names a district of Karachi, and naming it identifies nobody.

    House and plot NUMBERS are always random.

That is the line, and it is the whole privacy argument. What identifies a
dwelling is the number attached to the street, so no real address record is
reproduced here -- only the vocabulary and the shape real addresses are written
in. docs/decisions.md §1 requires the generator to generate, never to replay.

WHY NOT HAND-WRITE THEM. §5 recorded the weakness that the OSM work exists to
fix: the author wrote every address the project measured on, so the measurement
and the data shared an author. Hand-writing a fresh set here would rebuild that
circularity one layer down.

FORMS. The three the corpus splits into, because §15 and §16 report against
them, plus the Urdu structural nouns (Ghar/Gali/Makan) that §3 measured a ~21
point coverage penalty on. A generator that only emitted the English forms would
never exercise the weakest case.

Data (c) OpenStreetMap contributors, ODbL 1.0.
https://www.openstreetmap.org/copyright
"""
from __future__ import annotations

import random

# Districts and blocks, by city. Sampled across cities on purpose: the corpus is
# 83% Karachi, and a generator inheriting that skew would make Karachi forms look
# like Pakistani forms.
AREAS = {
    "Karachi": [
        "PECHS Block 2", "Shah Faisal Block 5", "FB Area Block 3",
        "FB Area Block 9", "North Nazimabad Block J", "Gulshan e Iqbal Block 2",
        "Gulistan e Johar Block 8", "New Karachi Sector 5C3", "Clifton Block 4",
        "DHA Phase 6", "Nazimabad 1", "Malir Cantt", "Korangi Sector 31 A",
    ],
    "Lahore": [
        "Model Town Block G", "Johar Town Block H", "Gulberg III",
        "DHA Phase 5", "Askari 10", "Faisal Town Block C", "Iqbal Town",
        "Wapda Town Block J", "Garden Town", "Shadman Colony",
    ],
    "Islamabad": [
        "F-6/3", "F-8/3", "G-9/1", "G-9/3", "F-6/2", "F-10/4", "I-8/2",
        "E-11/2", "G-11/3", "I-14",
    ],
    "Rawalpindi": [
        "Satellite Town Block D", "Chaklala Scheme 3", "Bahria Town Phase 4",
        "Westridge 1", "Gulraiz Housing Scheme",
    ],
    "Faisalabad": ["Peoples Colony 1", "Madina Town", "Gulberg Block Z"],
    "Multan": ["Shah Rukn e Alam Colony", "Gulgasht Colony", "Cantt Area"],
    "Peshawar": ["Hayatabad Phase 3", "University Town", "Gulbahar Colony"],
    "Quetta": ["Jinnah Town", "Satellite Town Block 2", "Chaman Housing Scheme"],
}

# Named roads, which produce the plain_street form. Kept separate from AREAS
# because the two combine differently: a road takes a house number directly,
# an area usually takes a house number and a street number.
#
# Keyed by city for the same reason AREAS is. A flat list produced 'Ferozepur
# Road, Karachi' and 'Rashid Minhas Road, Rawalpindi' -- a Lahore road and a
# Karachi road filed under the wrong cities. A detector cannot tell, but the
# premise of this whole locale argument is that the stream looks like Pakistani
# traffic, and to a Pakistani reader those do not.
ROADS = {
    "Karachi": ["Shahrah-e-Faisal", "Rashid Minhas Road", "Tariq Road",
                "Saba Avenue", "Sharea Ghalib", "Korangi Road"],
    "Lahore": ["Mall Road", "Ferozepur Road", "Jail Road", "Canal Bank Road",
               "Multan Road", "Khayaban-e-Iqbal"],
    "Islamabad": ["Margalla Road", "Hill Road", "Ataturk Avenue",
                  "Jinnah Avenue", "Service Road South"],
    "Rawalpindi": ["Murree Road", "Adamjee Road", "Airport Road", "Peshawar Road"],
    "Faisalabad": ["Jail Road", "Satiana Road", "Sargodha Road"],
    "Multan": ["Bosan Road", "Vehari Road", "Nishtar Road"],
    "Peshawar": ["Jamrud Road", "University Road", "Charsadda Road"],
    "Quetta": ["Zarghoon Road", "Sariab Road", "Airport Road"],
}

# English and Urdu structural nouns, paired so the same address shape can be
# written either way. §3 and §6.2 measured encoders losing ~21 points of
# coverage on the Urdu forms, replicated across two models -- it is the weakest
# case the pipeline has, so the stream has to contain it.
DWELLING_WORDS = ["House", "House No.", "Flat", "Plot", "Ghar", "Makan", "Makan No."]
STREET_WORDS = ["Street", "St", "Gali"]


# Place names that contain a token which is also a surname in the generator's
# own pool. Reviewed and kept, rather than scrubbed, for two reasons.
#
# They identify nobody: Pakistani localities are routinely named after historic
# figures, and Allama Iqbal died in 1938. Gulshan-e-Iqbal is one of Karachi's
# largest districts.
#
# And scrubbing them would make the stream easier than reality. An address
# holding a surname token is the confound §13.2 recorded -- ADDRESS labels
# re-tagging a name -- but it is a confound that genuinely occurs in Pakistani
# traffic, so a generator that removed it would flatter the firewall.
#
# The list is explicit so tests/test_generator_addresses.py stays strict: a new
# vocabulary entry carrying a name still fails until someone reviews it.
NAME_BEARING_PLACES = frozenset({
    "Gulshan e Iqbal Block 2", "Iqbal Town", "Khayaban-e-Iqbal",
})


def make_address(rng: random.Random | None = None) -> str:
    """One synthetic address. The number is always random; the place is real."""
    rng = rng or random
    city = rng.choice(list(AREAS))
    number = rng.choice([
        str(rng.randint(1, 900)),                                    # 214
        f"{rng.randint(1, 400)}-{rng.choice('ABCDEFGHJKLMNPQRS')}",   # 94-Q
        f"{rng.choice('ABCDEFGHJKLMNPQRS')}-{rng.randint(1, 900)}",   # R-31
        f"{rng.randint(1, 90)}{rng.choice('ABC')}",                   # 3B
    ])

    # Roads take a number directly; areas usually carry a street number too.
    # Both shapes occur throughout the corpus, so both are emitted.
    if rng.random() < 0.3:
        road = rng.choice(ROADS[city])
        return f"{rng.choice(DWELLING_WORDS)} {number}, {road}, {city}"

    area = rng.choice(AREAS[city])
    if rng.random() < 0.5:
        street = f"{rng.choice(STREET_WORDS)} {rng.randint(1, 60)}"
        return f"{rng.choice(DWELLING_WORDS)} {number}, {street}, {area}, {city}"
    return f"{rng.choice(DWELLING_WORDS)} {number}, {area}, {city}"
