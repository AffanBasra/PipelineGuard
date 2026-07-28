"""Tier 1 rules engine.

Organised by the question each group answers:
  * does it find what it should?          (detection)
  * does it reject what only *looks* right? (validation — the point of the tier)
  * are the spans usable for redaction?    (span correctness)
  * does it behave under overlap and noise? (robustness)

Tests assert on observable behaviour — the Findings returned — never on which
regex matched. That way the tier can be rewritten (ONNX, Rust, Aho-Corasick)
and these tests still describe what it must do.
"""
from __future__ import annotations

import pytest

from pipelineguard.models import Tier


def types_in(detector, text: str) -> list[str]:
    return sorted(f.entity_type for f in detector.detect(text, "memo"))


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, expected",
    [
        ("CNIC 35202-1234567-1 on file", ["CNIC"]),
        ("id 3520212345671 bare", ["CNIC"]),
        ("send to PK36MEZN0001234567891234", ["IBAN_PK"]),
        ("call 03001234567", ["PHONE_PK"]),
        ("call +92 300 1234567", ["PHONE_PK"]),
        ("call +923001234567", ["PHONE_PK"]),
        ("call 0300-123-4567", ["PHONE_PK"]),
        ("call 00923001234567", ["PHONE_PK"]),   # 00 international prefix
        ("mail affan@example.com", ["EMAIL"]),
        ("mail user@sub.domain.co.uk", ["EMAIL"]),
        ("Zakat contribution", []),
        ("", []),
    ],
)
def test_detects_expected_entity_types(detector, text, expected):
    assert types_in(detector, text) == expected


def test_finds_every_entity_in_a_dense_field(detector):
    text = (
        "Sent by Ali Khan (CNIC 35202-1234567-1), account "
        "PK36MEZN0001234567891234, phone 03001234567, mail ali@example.com"
    )
    assert types_in(detector, text) == ["CNIC", "EMAIL", "IBAN_PK", "PHONE_PK"]


def test_all_findings_are_tagged_tier_one(detector, pii_payload):
    for field, value in pii_payload.items():
        if isinstance(value, str):
            for finding in detector.detect(value, field):
                assert finding.tier is Tier.RULES


def test_finding_carries_the_field_it_was_found_in(detector):
    (finding,) = detector.detect("35202-1234567-1", "cnic")
    assert finding.field == "cnic"


# --------------------------------------------------------------------------- #
# Validation — what separates a rules engine from grep
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("province_digit", ["0", "8", "9"])
def test_cnic_with_invalid_province_digit_is_rejected(detector, province_digit):
    assert types_in(detector, f"id {province_digit}520212345671 here") == []


@pytest.mark.parametrize("province_digit", list("1234567"))
def test_cnic_with_valid_province_digit_is_accepted(detector, province_digit):
    assert types_in(detector, f"id {province_digit}520212345671 here") == ["CNIC"]


def test_iban_failing_mod97_is_reported_at_reduced_confidence(detector):
    """Structurally valid, checksum wrong: still reported, but flagged uncertain
    so the processor quarantines it rather than silently trusting it."""
    (finding,) = detector.detect("PK99MEZN5748718428058488", "iban")
    assert finding.entity_type == "IBAN_PK"
    assert finding.confidence < 1.0


def test_iban_passing_mod97_is_full_confidence(detector):
    (finding,) = detector.detect("PK68MEZN5748718428058488", "iban")
    assert finding.confidence == 1.0


@pytest.mark.parametrize(
    "text",
    [
        "Order 01234567890 shipped",     # leading 0 but not a mobile prefix
        "ratio 0.5 to 92 300 1234567",   # no country/trunk prefix
    ],
)
def test_non_phone_digit_runs_are_rejected(detector, text):
    assert "PHONE_PK" not in types_in(detector, text)


@pytest.mark.parametrize("text", ["a@b", "x@@y.com", "no-at-sign.com", "@example.com"])
def test_malformed_emails_are_rejected(detector, text):
    assert "EMAIL" not in types_in(detector, text)


# --------------------------------------------------------------------------- #
# Span correctness — redaction slices the ORIGINAL text using these
# --------------------------------------------------------------------------- #
def test_spans_index_the_original_string(detector):
    text = "x 35202-1234567-1 y"
    (finding,) = detector.detect(text, "memo")
    assert text[finding.span_start : finding.span_end] == "35202-1234567-1"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("notify at ijackson@example.com.", "ijackson@example.com"),  # trailing period
        ("(affan@example.com)", "affan@example.com"),                  # wrapped in parens
        ("<a@example.com>", "a@example.com"),
    ],
)
def test_punctuation_around_an_email_is_excluded_from_the_span(detector, text, expected):
    """Regression: the candidate regex grabs surrounding punctuation, so the span
    must be trimmed *and* span_start advanced by the amount trimmed."""
    (finding,) = detector.detect(text, "memo")
    assert text[finding.span_start : finding.span_end] == expected


def test_every_span_is_within_bounds_and_non_empty(detector, pii_payload):
    for field, value in pii_payload.items():
        if not isinstance(value, str):
            continue
        for f in detector.detect(value, field):
            assert 0 <= f.span_start < f.span_end <= len(value)


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #
def test_overlapping_matches_keep_the_longest_span(detector):
    """A 13-digit run inside an email local part is part of the email, not a
    separate CNIC — the longer span wins."""
    assert types_in(detector, "3520212345671@example.com") == ["EMAIL"]


def test_findings_are_returned_in_document_order(detector):
    text = "mail a@example.com then CNIC 35202-1234567-1 then 03001234567"
    findings = detector.detect(text, "memo")
    starts = [f.span_start for f in findings]
    assert starts == sorted(starts)


def test_two_separate_entities_are_both_reported(detector):
    assert types_in(detector, "35202-1234567-1 and 3520212345671") == ["CNIC", "CNIC"]


def test_detect_is_side_effect_free(detector):
    """The tier's benchmark claim assumes it can be called repeatedly and
    concurrently; identical input must give identical output."""
    text = "CNIC 35202-1234567-1 and mail a@example.com"
    first = detector.detect(text, "memo")
    second = detector.detect(text, "memo")
    assert first == second
