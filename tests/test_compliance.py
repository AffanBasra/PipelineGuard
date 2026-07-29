"""Tests for the regulatory classification layer.

Pure functions over a static mapping, so no infrastructure and no doubles.
"""
from __future__ import annotations

from pipelineguard import compliance


def test_every_classification_is_populated() -> None:
    """An empty basis string would render as a blank bullet in the report --
    a regulatory claim that looks present and says nothing."""
    for entity_type, cls in compliance.CLASSIFICATIONS.items():
        assert cls.data_category.strip(), entity_type
        assert cls.pk_basis.strip(), entity_type
        assert cls.gdpr_basis.strip(), entity_type


def test_classify_returns_none_for_unknown() -> None:
    assert compliance.classify("NOT_A_REAL_TYPE") is None


def test_classify_returns_the_mapping() -> None:
    cnic = compliance.classify("CNIC")
    assert cnic is not None
    assert cnic.data_category == "National identity number"
    assert cnic.special_category is False


def test_unclassified_lists_only_unknown_types_once_in_order() -> None:
    result = compliance.unclassified(["CNIC", "ZETA", "EMAIL", "ALPHA", "ZETA"])
    assert result == ["ZETA", "ALPHA"]


def test_unclassified_is_empty_when_all_known() -> None:
    assert compliance.unclassified(list(compliance.CLASSIFICATIONS)) == []


def test_every_entity_type_the_detector_emits_is_classified(detector, pii_payload) -> None:
    """Drift guard, and the reason this test is worth more than it looks.

    Adding an entity type to the detector without classifying it would not
    break anything: the report would render it as '**unclassified**' and keep
    going, which is the correct runtime behaviour but a silent regression in a
    compliance document. This fails the build instead.
    """
    emitted = {
        finding.entity_type
        for field, value in pii_payload.items()
        if isinstance(value, str)
        for finding in detector.detect(value, field)
    }
    assert emitted, "fixture produced no findings; the guard would pass vacuously"
    assert compliance.unclassified(sorted(emitted)) == []