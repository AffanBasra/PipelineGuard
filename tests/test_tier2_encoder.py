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

from pipelineguard.detectors.tier2_encoder import LABEL_GROUPS, Tier2Detector
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
    by_type = {f.entity_type for f in findings}
    assert by_type == {"PERSON_NAME", "ADDRESS"}
    assert all(f.tier == Tier.ENCODER for f in findings)
    assert all(f.field == "memo" for f in findings)
    assert all(0.0 < f.confidence <= 1.0 for f in findings)


def test_spans_point_at_the_right_characters(detector):
    text = "Paid Ayesha today"
    f = detector.detect(text, "memo")[0]
    assert text[f.span_start:f.span_end] == "Ayesha"


def test_one_pass_per_label_group(detector):
    """Groups are run separately on purpose: a single combined pass drops
    PERSON coverage 99.4% -> 90.9% and ADDRESS 84.7% -> 70.5%."""
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


def test_empty_input_makes_no_calls(detector):
    """A batch of records with no free text must not touch the GPU at all."""
    assert detector.detect_batch({}) == {}
    assert detector._model.calls == []


def test_multiple_fields_per_message(detector):
    out = detector.detect_batch({0: {"memo": "Ayesha", "note": "Islamabad"}})
    assert set(out[0]) == {"memo", "note"}
    assert out[0]["note"][0].field == "note"
