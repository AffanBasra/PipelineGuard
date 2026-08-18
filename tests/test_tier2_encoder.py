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
    _TUNED_FOR_BASE,
    _TUNED_FOR_BASE_REVISION,
    _TUNED_FOR_REVISION,
    LABEL_GROUPS,
    _MAX_BRIDGE,
    _MAX_EXTENSIONS,
    Tier2Detector,
    bridge_address_spans,
    cached_base_revisions,
    cached_main_revision,
    extend_address_span,
    pin_main_ref,
    prefetch_pinned,
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
        # Case-insensitively, and for every road word the §24 rule lists.
        ("Model Town, Sialkot road", "Model Town"),
        ("Gulberg, Hyderabad Bypass", "Gulberg"),
        ("Saddar, Multan Cantt", "Saddar"),
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


@pytest.mark.parametrize(
    "text, found, expected",
    [
        # Found by a live Kafka run (§24), not by any offline probe. Roman-Urdu
        # memos continue PAST the city, so requiring end-of-string or
        # punctuation after it left the city in the clear on the shipped path.
        ("Statement Plot E-379, Airport Road, Quetta par bhej dein",
         "Airport Road", "Plot E-379, Airport Road, Quetta"),
        ("Statement Plot 12B, PECHS Block 2, Karachi par bhej dein",
         "PECHS Block 2", "Plot 12B, PECHS Block 2, Karachi"),
        ("Ghar ka pata House 5, Model Town, Lahore hai",
         "Model Town", "House 5, Model Town, Lahore"),
    ],
)
def test_trailing_city_is_taken_when_narration_continues(text, found, expected):
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


def test_the_backbone_commit_is_declared_too():
    """§25: a Tier 2 load touches TWO repos. TIER2_MODEL_REVISION pins the
    checkpoint; the backbone config is resolved at `main` by GLiNER with no
    revision, so it has to be declared separately or it is invisible."""
    from pipelineguard.config import settings

    assert settings.tier2_base_model == _TUNED_FOR_BASE
    assert settings.tier2_base_revision == _TUNED_FOR_BASE_REVISION


def test_the_two_pins_are_different_repos():
    """Carrying one repo's commit to the other would ask the hub for a commit
    that does not exist and fail a load on a config nobody set."""
    assert _TUNED_FOR_BASE != _TUNED_FOR
    assert _TUNED_FOR_BASE_REVISION != _TUNED_FOR_REVISION


def test_cached_base_revisions_reports_an_absent_repo_as_empty(tmp_path):
    """An empty list is the signal that offline mode has nothing to resolve, so
    it must not raise on a cold cache -- that is the normal first-run state."""
    assert cached_base_revisions(_TUNED_FOR_BASE, cache_root=tmp_path) == []


def test_cached_base_revisions_lists_what_is_there(tmp_path):
    """More than one commit cached means offline mode resolves an unspecified
    one, which is exactly the state the prefetch warns about."""
    snapshots = tmp_path / "models--microsoft--deberta-v3-base" / "snapshots"
    (snapshots / "aaa").mkdir(parents=True)
    (snapshots / "bbb").mkdir(parents=True)
    assert cached_base_revisions(_TUNED_FOR_BASE, cache_root=tmp_path) == [
        "aaa", "bbb"
    ]


def _snapshot_only(cache_root, revision=_TUNED_FOR_BASE_REVISION):
    """The state a fresh prefetch leaves behind: right commit, no `main` ref."""
    snapshots = (cache_root / "models--microsoft--deberta-v3-base" / "snapshots")
    (snapshots / revision).mkdir(parents=True)


def test_cached_main_revision_is_none_when_only_the_snapshot_is_there(tmp_path):
    """The exact state that broke the demo image: the pinned commit is cached,
    yet a no-revision load has nothing to resolve and fails offline."""
    _snapshot_only(tmp_path)
    assert cached_base_revisions(_TUNED_FOR_BASE, cache_root=tmp_path) == [
        _TUNED_FOR_BASE_REVISION
    ]
    assert cached_main_revision(_TUNED_FOR_BASE, cache_root=tmp_path) is None


def test_pin_main_ref_makes_main_resolve_to_the_pinned_commit(tmp_path):
    _snapshot_only(tmp_path)
    pin_main_ref(_TUNED_FOR_BASE, _TUNED_FOR_BASE_REVISION, cache_root=tmp_path)
    assert cached_main_revision(_TUNED_FOR_BASE,
                                cache_root=tmp_path) == _TUNED_FOR_BASE_REVISION


def test_prefetch_pinned_records_main_as_well_as_downloading(tmp_path,
                                                             monkeypatch):
    """Both halves in one step. Downloading the right commit without recording
    the ref is exactly how the demo image shipped unloadable."""
    import huggingface_hub

    calls = []
    monkeypatch.setattr(huggingface_hub, "snapshot_download",
                        lambda repo_id, **kw: calls.append(
                            (repo_id, kw.get("revision"))))
    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))

    prefetch_pinned(_TUNED_FOR, _TUNED_FOR_REVISION,
                    _TUNED_FOR_BASE, _TUNED_FOR_BASE_REVISION)

    assert calls == [(_TUNED_FOR, _TUNED_FOR_REVISION),
                     (_TUNED_FOR_BASE, _TUNED_FOR_BASE_REVISION)]
    assert cached_main_revision(_TUNED_FOR_BASE,
                                cache_root=tmp_path) == _TUNED_FOR_BASE_REVISION


def _load_with_fake_gliner(monkeypatch, **detector_kwargs) -> dict:
    """Run `load()` against a stand-in GLiNER and return the kwargs it received.

    A fake module rather than the real one so this stays runnable without the
    tier2 extra, and so the assertion is about the call, not about weights.
    """
    import sys
    import types

    seen: dict = {}

    def from_pretrained(model_id, **kwargs):
        seen.update(kwargs)
        return FakeGLiNER()

    fake = types.ModuleType("gliner")
    fake.GLiNER = types.SimpleNamespace(from_pretrained=from_pretrained)
    monkeypatch.setitem(sys.modules, "gliner", fake)
    monkeypatch.setattr(
        "pipelineguard.detectors.tier2_encoder.resolve_device", lambda _: "cpu")

    Tier2Detector(_TUNED_FOR, revision=_TUNED_FOR_REVISION, batch_size=1,
                  **detector_kwargs).load()
    return seen


def test_load_asks_for_the_half_precision_weights_when_a_variant_is_set(
        monkeypatch):
    """bf16 is what makes the demo fit a 1 GB host (findings §27.5), and
    low_cpu_mem_usage is not optional decoration: without it torch builds a
    full-precision copy on the way in and the peak erases the saving."""
    seen = _load_with_fake_gliner(monkeypatch, variant="bf16")
    assert seen["variant"] == "bf16"
    assert seen["low_cpu_mem_usage"] is True
    assert seen["revision"] == _TUNED_FOR_REVISION


def test_load_without_a_variant_asks_for_no_precision_override(monkeypatch):
    """The pipeline still runs full precision. A variant leaking into the
    default path would change the shipped detector without anyone choosing it."""
    seen = _load_with_fake_gliner(monkeypatch)
    assert "variant" not in seen
    assert "low_cpu_mem_usage" not in seen


def test_prefetch_skips_the_weight_files_a_variant_never_opens(tmp_path,
                                                               monkeypatch):
    """1.6 GB of weights for one 400 MB file that gets read. On a host that
    downloads at boot rather than at build, that is the whole cold start."""
    import huggingface_hub

    calls = []
    monkeypatch.setattr(huggingface_hub, "snapshot_download",
                        lambda repo_id, **kw: calls.append((repo_id, kw)))
    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))

    prefetch_pinned(_TUNED_FOR, _TUNED_FOR_REVISION, _TUNED_FOR_BASE,
                    _TUNED_FOR_BASE_REVISION, variant="bf16")

    ignored = calls[0][1]["ignore_patterns"]
    assert "model.bf16.safetensors" not in ignored
    assert set(ignored) == {"model.fp16.safetensors", "pytorch_model.bin"}


def test_prefetch_without_a_variant_fetches_every_weight_file(tmp_path,
                                                              monkeypatch):
    """The default build is unchanged. Filtering it would strip the fp32
    weights the pipeline actually loads."""
    import huggingface_hub

    calls = []
    monkeypatch.setattr(huggingface_hub, "snapshot_download",
                        lambda repo_id, **kw: calls.append((repo_id, kw)))
    monkeypatch.setattr("huggingface_hub.constants.HF_HUB_CACHE", str(tmp_path))

    prefetch_pinned(_TUNED_FOR, _TUNED_FOR_REVISION, _TUNED_FOR_BASE,
                    _TUNED_FOR_BASE_REVISION)

    assert calls[0][1]["ignore_patterns"] is None


def test_pin_main_ref_replaces_a_ref_left_by_an_earlier_download(tmp_path):
    """A cache that once tracked `main` already holds a ref, and it is the stale
    one that would silently win. Pinning has to overwrite, not skip."""
    refs = tmp_path / "models--microsoft--deberta-v3-base" / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text("0" * 40, encoding="utf-8")
    pin_main_ref(_TUNED_FOR_BASE, _TUNED_FOR_BASE_REVISION, cache_root=tmp_path)
    assert cached_main_revision(_TUNED_FOR_BASE,
                                cache_root=tmp_path) == _TUNED_FOR_BASE_REVISION


def test_an_explicit_revision_survives_a_model_change():
    """Dropping the DEFAULT pin must not drop a hash the operator chose. That
    would silently unpin a deployment that had been pinned on purpose."""
    detector = Tier2Detector("nvidia/gliner-PII", revision="abc123")
    assert detector.resolved_revision() == "abc123"


# --------------------------------------------------------------------------- #
# Span bridging (§23)
#
# The residual left by the extension rules is mostly not a boundary. The encoder
# returns 'C-21' and 'Karachi' and drops the locality between them, and no
# outward walk can reach an interior gap.
# --------------------------------------------------------------------------- #
def test_bridge_joins_spans_separated_by_a_small_gap():
    joined = bridge_address_spans([(0, 4, 0.7), (4 + _MAX_BRIDGE, 60, 0.6)])
    assert joined == [(0, 60, 0.7)]


def test_bridge_leaves_a_wide_gap_alone():
    """One memo can carry two unrelated addresses, and the text between them is
    ordinary narration. An unbounded join would redact the whole memo."""
    apart = [(0, 4, 0.7), (5 + _MAX_BRIDGE, 60, 0.6)]
    assert bridge_address_spans(apart) == apart


def test_bridge_keeps_the_best_score_of_the_group():
    """The group is one address, so the confidence that matters is the best
    evidence for it. Taking the lower score would push a joined span under a
    downstream confidence filter."""
    assert bridge_address_spans([(0, 4, 0.58), (10, 20, 0.94)]) == [(0, 20, 0.94)]


def test_bridge_sorts_before_joining():
    """Label groups are queried separately, so spans do not arrive in positional
    order. Trusting the input order would leave the first span unmerged."""
    assert bridge_address_spans([(10, 20, 0.6), (0, 4, 0.9)]) == [(0, 20, 0.9)]


@pytest.mark.parametrize("spans", [[], [(3, 9, 0.8)]])
def test_bridge_passes_through_empty_and_single(spans):
    assert bridge_address_spans(spans) == spans


def test_an_interior_hole_between_two_address_spans_is_closed(detector):
    """The §23 shape, and the reason bridging exists. The encoder finds the plot
    number and the city and drops the locality between them, which is the most
    identifying part of the address."""
    text = "Deliver to C-21, Block J North Nazimabad Town, Karachi"
    detector._model.TRIGGERS = {"C-21": "location", "Karachi": "location"}
    findings = detector.detect(text, "memo")
    assert len(findings) == 1
    assert text[findings[0].span_start:findings[0].span_end] == (
        "C-21, Block J North Nazimabad Town, Karachi"
    )


def test_bridging_does_not_join_person_spans(detector):
    """Two names either side of an amount must not swallow the amount. ADDRESS
    is the only entity a positional rule is allowed to widen."""
    text = "Ayesha sent 45,000 to Bilal"
    detector._model.TRIGGERS = {"Ayesha": "person", "Bilal": "person"}
    findings = detector.detect(text, "memo")
    assert {text[f.span_start:f.span_end] for f in findings} == {"Ayesha", "Bilal"}


def test_bridging_is_off_when_extension_is_off(detector):
    """§23's before/after measurement runs the same detector twice, so the flag
    has to reach bridging as well as extension."""
    text = "Deliver to C-21, Block J North Nazimabad Town, Karachi"
    detector._model.TRIGGERS = {"C-21": "location", "Karachi": "location"}
    detector.extend_addresses = False
    spans = {text[f.span_start:f.span_end] for f in detector.detect(text, "memo")}
    assert spans == {"C-21", "Karachi"}


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
