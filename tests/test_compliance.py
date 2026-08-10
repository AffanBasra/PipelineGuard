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


# Named Pakistani instruments. A pk_basis that references none of these is
# not a Pakistani basis, whatever it says.
PK_INSTRUMENTS = (
    "PECA 2016",
    "NADRA",
    "National Database and Registration Authority",
    "State Bank of Pakistan",
    "SBP",
    "Pakistan Telecommunication Authority",
    "Personal Data Protection Bill",
)


def test_every_classification_names_a_pakistani_instrument() -> None:
    """The regression guard for how this module first shipped.

    Every entry cited GDPR articles; only two named any Pakistani instrument,
    and two `pk_basis` fields were bare descriptions with no legal grounding
    at all -- inverting the scope decision in docs/decisions.md section 1,
    which puts Pakistani law first. Nothing failed, because nothing checked.
    """
    for entity_type, cls in compliance.CLASSIFICATIONS.items():
        assert any(i in cls.pk_basis for i in PK_INSTRUMENTS), (
            f"{entity_type}.pk_basis names no Pakistani instrument: {cls.pk_basis!r}"
        )


def test_every_system_property_cites_both_regimes() -> None:
    """Section 8 was originally four GDPR articles and nothing else -- the
    most substantive part of the compliance framing, with no Pakistani limb."""
    assert compliance.SYSTEM_PROPERTIES
    for prop in compliance.SYSTEM_PROPERTIES:
        assert prop.title.strip(), prop
        assert prop.behaviour.strip(), prop.title
        assert prop.gdpr_basis.strip(), prop.title
        assert any(i in prop.pk_basis for i in PK_INSTRUMENTS), (
            f"{prop.title!r} has no Pakistani basis: {prop.pk_basis!r}"
        )


def test_landscape_note_states_why_the_regimes_differ() -> None:
    """Honesty about the asymmetry is the alternative to padding it. If this
    text is ever softened into implying Pakistan has an enacted general data
    protection law, the report starts overclaiming."""
    note = compliance.LEGAL_LANDSCAPE_NOTE
    assert "no enacted general data protection statute" in note
    assert "remains a draft" in note


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


def test_every_tier_2_entity_type_is_classified() -> None:
    """The same guard for the encoder, which the test above cannot reach.

    It only exercises the Tier 1 detector, so restoring ADDRESS to LABEL_GROUPS
    added an entity type that reached the audit and the governance report while
    being unclassified -- and nothing failed. Read from LABEL_GROUPS rather than
    from a hardcoded list, so adding a third group fails here too.
    """
    from pipelineguard.detectors.tier2_encoder import LABEL_GROUPS

    assert compliance.unclassified(sorted(LABEL_GROUPS)) == []


def test_schema_declared_entity_types_are_classified() -> None:
    """And the third detector. All three write to the same findings table."""
    from pipelineguard.detectors.schema_rules import DECLARED_PII

    assert compliance.unclassified(sorted(set(DECLARED_PII.values()))) == []