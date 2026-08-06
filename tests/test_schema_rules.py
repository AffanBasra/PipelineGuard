"""Declared-PII fields.

The behaviour under test is deliberately dumb: a field named in DECLARED_PII is
redacted whole at confidence 1.0, and nothing else is touched. The value of
these tests is that they pin the two edges where "dumb" could go wrong — the
empty field, and fields that merely resemble a declared one.
"""
from __future__ import annotations

import pytest

from pipelineguard.detectors.schema_rules import DECLARED_PII, SchemaDetector
from pipelineguard.models import Tier


@pytest.fixture(scope="module")
def schema() -> SchemaDetector:
    return SchemaDetector()


def test_declared_field_is_covered_whole(schema):
    """The span must cover every character: a partial span would leave part of
    a name in the clear, which is the exact failure the schema rule exists to
    make impossible."""
    text = "Ayesha Malik"
    findings = schema.detect(text, "account_holder")

    assert len(findings) == 1
    f = findings[0]
    assert (f.span_start, f.span_end) == (0, len(text))
    assert f.entity_type == "PERSON_NAME"
    assert f.field == "account_holder"
    assert f.confidence == 1.0
    assert f.tier == Tier.RULES


@pytest.mark.parametrize(
    "name",
    [
        "Ayesha Malik",
        "Mahnoor Warraich",        # the rare band the encoder does worst on
        "Noor Khan",               # first name is also an ordinary Urdu noun
        "X",                       # single character
        "  Ayesha  Malik  ",       # padding is part of the field, so redacted
        "Ayesha Malik <ayesha@example.com>",
    ],
)
def test_whole_field_regardless_of_content(schema, name):
    """No content sensitivity at all. This is the property that makes the rule
    worth having: unlike the encoder measured at 99.4% coverage, it cannot be
    wrong about a name it has not seen before."""
    findings = schema.detect(name, "account_holder")
    assert len(findings) == 1
    assert (findings[0].span_start, findings[0].span_end) == (0, len(name))


def test_empty_declared_field_yields_nothing(schema):
    """A zero-width finding would put a [PERSON_NAME] marker where no name ever
    was, and would count toward the record's finding total."""
    assert schema.detect("", "account_holder") == []


@pytest.mark.parametrize(
    "field",
    ["memo", "cnic", "channel", "email", "account_holder_id", "holder", ""],
)
def test_undeclared_fields_are_untouched(schema, field):
    """Exact field-name match only. A substring or prefix match would silently
    redact fields nobody declared."""
    assert schema.detect("Ayesha Malik", field) == []


def test_account_holder_is_declared():
    """Guards the registry itself: the processor's dispatch branches on
    DECLARED_PII membership, so an empty or renamed entry would silently route
    the field back to rule detection, which has no name rule at all."""
    assert DECLARED_PII["account_holder"] == "PERSON_NAME"