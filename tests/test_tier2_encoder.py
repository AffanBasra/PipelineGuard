"""Tier 2 orchestration, with a fake model.

The wrapper's job is batching, chunking and mapping labels to entity types —
none of which needs real weights. A fake records every call, so the tests can
assert the things that would silently cost throughput or correctness in
production: that one pass runs per label group, that chunks respect batch_size,
and that results land on the right message.

Inference quality is not tested here. That is measured in
docs/tier2-detection-findings.md against a corpus, which is the right instrument
for it; asserting model output in a unit test would pin today's weights.
"""
from __future__ import annotations

import pytest

from pipelineguard.detectors.tier2_encoder import (
    _TUNED_FOR,
    _TUNED_FOR_REVISION,
    LABEL_GROUPS,
    _MAX_EXTENSIONS,
    Tier2Detector,
    extend_address_span,
)
from pipelineguard.models import Tier


class FakeGLiNER:
    """Returns a hit for any text containing a trigger word, and records the
    (batch size, labels) of every call."""

    TRIGGERS = {"Ayesha": "person", "Islamabad": "location"}

    def __init__(self):
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def eval(self):
        return self

    def batch_predict_entities(self, texts, labels, threshold=0.25):
        self.calls.append((len(texts), tuple(labels)))
        out = []
        for text in texts:
            hits = []
            for word, label in self.TRIGGERS.items():
                if label in labels and word in text:
                    start = text.index(word)
                    hits.append({"start": start, "end": start + len(word),
                                 "label": label, "score": 0.91})
            out.append(hits)
        return out


@pytest.fixture
def detector():
    d = Tier2Detector("fake/model", threshold=0.25, device="cpu", batch_size=4)
    d._model = FakeGLiNER()
    return d


def test_requires_load_before_use():
    """Using an unloaded detector must fail loudly, not silently find nothing —
    a Tier 2 that returns [] looks exactly like clean text."""
    with pytest.raises(RuntimeError, match="load"):
        Tier2Detector("fake/model").detect_batch({0: {"memo": "Ayesha"}})


def test_maps_labels_to_entity_types(detector):
    findings = detector.detect("Ayesha went to Islamabad", "memo")
    assert {f.entity_type for f in findings} == {"PERSON_NAME", "ADDRESS"}
    assert all(f.tier == Tier.ENCODER for f in findings)
    assert all(f.field == "memo" for f in findings)
    assert all(0.0 < f.confidence <= 1.0 for f in findings)


def test_address_labels_are_requested(detector):
    """Restored in §18, replacing the assertion that they never are.

    §13 dropped them because nothing in the stream contained an address, which
    made the pass pure cost. generator/addresses.py ended that, and the measured
    price of restoring them is one extra forward pass and — because every span
    they claim on an address-free memo overlaps one PERSON already claimed —
    exactly zero additional redaction.
    """
    detector.detect_batch({0: {"memo": "Ayesha went to Islamabad"}})
    requested = {label for _n, labels in detector._model.calls for label in labels}
    assert {"address", "street_address", "location"} <= requested


def test_spans_point_at_the_right_characters(detector):
    text = "Paid Ayesha today"
    f = detector.detect(text, "memo")[0]
    assert text[f.span_start:f.span_end] == "Ayesha"


def test_one_pass_per_label_group(detector):
    """Groups are run separately on purpose, because labels compete for the same
    spans when combined. §13 measured that as PERSON 99.4% -> 90.9%; at the
    current checkpoint §18 measured only 100.0% -> 98.4%, for 45% less compute.
    The split still ships: PERSON is the entity this pipeline exists for."""
    detector.detect_batch({0: {"memo": "Ayesha"}})
    assert len(detector._model.calls) == len(LABEL_GROUPS)
    assert {c[1] for c in detector._model.calls} == {
        tuple(v) for v in LABEL_GROUPS.values()
    }


def test_chunks_respect_batch_size(detector):
    """Oversized batches are what run a small card out of VRAM."""
    detector.detect_batch({i: {"memo": f"Ayesha {i}"} for i in range(10)})
    sizes = [n for n, _labels in detector._model.calls]
    assert max(sizes) <= detector.batch_size
    assert sum(sizes) == 10 * len(LABEL_GROUPS)


def test_results_land_on_the_right_message(detector):
    """A zip error here would redact the wrong record — findings from one memo
    applied to another message's spans."""
    out = detector.detect_batch({
        7: {"memo": "nothing here"},
        8: {"memo": "Ayesha only"},
        9: {"memo": "Islamabad only"},
    })
    assert 7 not in out                      # no findings -> key omitted
    assert {f.entity_type for f in out[8]["memo"]} == {"PERSON_NAME"}
    assert {f.entity_type for f in out[9]["memo"]} == {"ADDRESS"}
    assert out[8]["memo"][0].span_start == 0


def test_empty_input_makes_no_calls(detector):
    """A batch of records with no free text must not touch the GPU at all."""
    assert detector.detect_batch({}) == {}
    assert detector._model.calls == []


def test_multiple_fields_per_message(detector):
    out = detector.detect_batch({0: {"memo": "Ayesha here", "note": "Ayesha there"}})
    assert set(out[0]) == {"memo", "note"}
    assert out[0]["note"][0].field == "note"


# --------------------------------------------------------------------------- #
# Span extension for ADDRESS findings (§17, §18)
#
# The encoder finds the street and drops the house number. That is a redaction
# that LOOKS complete and is not, which is the worst failure a firewall has, and
# §17 measured it as 26.8% of all missed identifying characters.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, found, expected",
    [
        # The house number, in the shapes the corpus actually writes it.
        ("14, Hill Road, F-6/3", "Hill Road, F-6/3", "14, Hill Road, F-6/3"),
        ("R1525, FB Area Block 3", "FB Area Block 3", "R1525, FB Area Block 3"),
        ("D 5, Block 10-A", "Block 10-A", "D 5, Block 10-A"),
        ("94-Q, Model Town", "Model Town", "94-Q, Model Town"),
        ("64N, PECHS Block 2", "PECHS Block 2", "64N, PECHS Block 2"),
        # A dwelling word in front of the number goes too, English or Urdu.
        ("Cheque posted to House 12, Street 4",
         "Street 4", "House 12, Street 4"),
        ("Pata: Ghar 87, Gali 4", "Gali 4", "Ghar 87, Gali 4"),
        ("Makan No. 221, Sector G-9/1", "Sector G-9/1", "Makan No. 221, Sector G-9/1"),
        # OSM writes doubled separators, and a single-comma anchor could not
        # reach back over them.
        ("V2HP+3PP, , Imam Khumani Library Rd", "Imam Khumani Library Rd",
         "V2HP+3PP, , Imam Khumani Library Rd"),
        # The trailing city.
        ("A352, Gulistan e Jauhar Block 7, Karachi", "Gulistan e Jauhar Block 7",
         "A352, Gulistan e Jauhar Block 7, Karachi"),
    ],
)
def test_extension_recovers_the_house_number_and_city(text, found, expected):
    start = text.index(found)
    new_start, new_end = extend_address_span(text, start, start + len(found))
    assert text[new_start:new_end] == expected


@pytest.mark.parametrize(
    "text, found",
    [
        # No digit, so not a house number. 'Qadri Manzil' is a building name and
        # could be a person's; leaving it is the conservative failure.
        ("qadri manzil, House 87 Street 4", "House 87 Street 4"),
        # Ordinary prose must never be swallowed.
        ("Cheque posted to Model Town", "Model Town"),
        ("Customer resides at Gulberg III", "Gulberg III"),
        # An invoice number is comma-adjacent in exactly the same shape as a
        # house number, and is the one case the digit rule alone gets wrong.
        ("Payment against order #4821, Model Town", "Model Town"),
        # A city that is not the one trailing this address stays put.
        ("Model Town, Sialkot Road", "Model Town"),
    ],
)
def test_extension_leaves_everything_else_alone(text, found):
    start = text.index(found)
    assert extend_address_span(text, start, start + len(found)) == (
        start, start + len(found)
    )


@pytest.mark.parametrize(
    "text, found, expected",
    [
        # The shape §21.2 found behind every worst-scoring well-formed address:
        # the plot number sits TWO components out, behind a structural one that
        # carries no digit, so a single-step rule never reached it.
        ("C-21, Block J North Nazimabad Town, Karachi", "North Nazimabad Town",
         "C-21, Block J North Nazimabad Town, Karachi"),
        ("B-14, Block D North Nazimabad Town, Karachi", "North Nazimabad Town",
         "B-14, Block D North Nazimabad Town, Karachi"),
        ("R-47, Sector 16/A Bufferzone, Karachi", "Bufferzone",
         "R-47, Sector 16/A Bufferzone, Karachi"),
        ("94-Q, Phase 3, Model Town", "Model Town", "94-Q, Phase 3, Model Town"),
    ],
)
def test_extension_steps_over_a_structural_component(text, found, expected):
    start = text.index(found)
    new_start, new_end = extend_address_span(text, start, start + len(found))
    assert text[new_start:new_end] == expected


def test_the_walk_is_bounded():
    """The walk is a loop now. Unbounded, a memo made of comma-separated
    fragments would let it cross the sentence and redact the narration."""
    text = "Block 1, Block 2, Block 3, Block 4, Block 5, Model Town"
    start = text.index("Model Town")
    new_start, _ = extend_address_span(text, start, start + len("Model Town"))
    assert new_start > 0, "the walk consumed the whole string"
    assert text[new_start:].count("Block") <= _MAX_EXTENSIONS


def test_the_wider_walk_still_refuses_the_invoice_number():
    """The one false positive the digit rule cannot tell from a house number.
    Stepping over structural components must not open a path back to it."""
    text = "Payment against order #4821, Block J, Model Town"
    start = text.index("Model Town")
    new_start, _ = extend_address_span(text, start, start + len("Model Town"))
    assert "4821" not in text[new_start:]


def test_extension_never_leaves_the_text():
    """An off-by-one here would raise IndexError inside redact() on live
    traffic, below the fail-closed boundary."""
    for text in ("12, Mall Road", "Mall Road", "", "1"):
        for start in range(len(text) + 1):
            for end in range(start, len(text) + 1):
                new_start, new_end = extend_address_span(text, start, end)
                assert 0 <= new_start <= new_end <= len(text)


def test_extension_applies_only_to_address_findings(detector):
    """A PERSON span must not be widened. 'Paid 12, Ayesha' would otherwise
    redact the amount along with the name — over-redaction of exactly the kind
    §16.2 measures."""
    text = "Paid 12, Ayesha today"
    finding = detector.detect(text, "memo")[0]
    assert finding.entity_type == "PERSON_NAME"
    assert text[finding.span_start:finding.span_end] == "Ayesha"


# --------------------------------------------------------------------------- #
# Checkpoint pinning (§20)
#
# A HuggingFace model repo is a git repo, and this one gained fp16/bf16 variants
# on 2026-04-28 with no name change. The 0.55 threshold is only valid for one
# set of weights (§6.1), and a name-only check cannot see weights move.
# --------------------------------------------------------------------------- #
def test_the_pinned_revision_is_the_one_the_threshold_was_swept_against():
    """config.py and tier2_encoder.py each carry the hash. If they drift, the
    warning below fires on a correct deployment and stops being read."""
    from pipelineguard.config import settings

    assert settings.tier2_model_revision == _TUNED_FOR_REVISION
    assert settings.tier2_model == _TUNED_FOR


def test_the_pin_is_passed_through_to_the_hub():
    detector = Tier2Detector(_TUNED_FOR, revision=_TUNED_FOR_REVISION)
    assert detector.resolved_revision() == _TUNED_FOR_REVISION


@pytest.mark.parametrize("revision", ["main", "", None])
def test_branch_and_empty_mean_unpinned(revision):
    """'main' is a moving pointer, not a pin. It is passed as None so the ready
    log can say UNPINNED rather than implying a fixed checkpoint."""
    assert Tier2Detector(_TUNED_FOR, revision=revision).resolved_revision() is None


def test_the_default_pin_is_dropped_for_a_different_model():
    """A commit hash belongs to ONE repo. Carrying this project's pin over to
    nvidia/gliner-PII would ask the hub for a commit that does not exist there
    and fail the load outright — on a config the operator never set."""
    detector = Tier2Detector("nvidia/gliner-PII", revision=_TUNED_FOR_REVISION)
    assert detector.resolved_revision() is None


def test_an_explicit_revision_survives_a_model_change():
    """Dropping the DEFAULT pin must not drop a hash the operator chose. That
    would silently unpin a deployment that had been pinned on purpose."""
    detector = Tier2Detector("nvidia/gliner-PII", revision="abc123")
    assert detector.resolved_revision() == "abc123"


def test_extension_can_be_switched_off(detector):
    """The before/after measurement in probe_address_residual.py runs the same
    detector twice. If the flag did nothing, that comparison would report a
    delta of zero and the rule would look worthless."""
    detector.extend_addresses = False
    text = "Statement to 14, Islamabad"
    finding = detector.detect(text, "memo")[0]
    assert text[finding.span_start:finding.span_end] == "Islamabad"

    detector.extend_addresses = True
    finding = detector.detect(text, "memo")[0]
    assert text[finding.span_start:finding.span_end] == "14, Islamabad"
