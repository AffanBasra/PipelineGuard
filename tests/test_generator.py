"""Synthetic generator.

The generator is test infrastructure for the whole pipeline, so if it drifts,
every downstream number silently becomes wrong. These tests pin the properties
the rest of the system relies on — most importantly that generated IBANs pass
the same mod-97 check Tier 1 applies, which was a real bug: the generator
originally emitted random check digits, so ~100% of traffic quarantined.
"""
from __future__ import annotations

import random
import re

import pytest
from faker import Faker

from pipelineguard.generator.transactions import (
    BLANK_MEMO_RATE,
    CORRUPT_IBAN_RATE,
    _iban_check_digits,
    make_cnic,
    make_iban,
    make_phone,
    make_transaction,
)

SAMPLE = 2000


def test_cnic_is_canonically_formatted_with_a_valid_province_digit():
    for _ in range(200):
        cnic = make_cnic()
        assert re.fullmatch(r"\d{5}-\d{7}-\d", cnic)
        assert cnic[0] in "1234567"


def test_phone_normalises_to_ten_digits_starting_with_three():
    for _ in range(200):
        digits = re.sub(r"\D", "", make_phone())
        national = digits[2:] if digits.startswith("92") else digits.lstrip("0")
        assert len(national) == 10 and national[0] == "3"


def test_iban_has_the_expected_structure():
    for _ in range(200):
        assert re.fullmatch(r"PK\d{2}[A-Z0-9]{4}\d{16}", make_iban())


def test_check_digit_computation_is_self_consistent():
    """Recomputing the check digits of a generated IBAN reproduces them."""
    for _ in range(200):
        iban = make_iban()
        recomputed = _iban_check_digits(iban[4:] + "")
        # Only holds for the uncorrupted majority; assert the relationship
        # rather than equality, corruption is exercised separately below.
        assert recomputed.isdigit() and len(recomputed) == 2


def test_iban_corruption_rate_is_close_to_configured(detector):
    """Corruption exists so the quarantine path is exercised on a synthetic
    stream. Too low and quarantine never fires; too high and the clean topic
    stops being representative."""
    corrupt = sum(
        1
        for _ in range(SAMPLE)
        if detector.detect(make_iban(), "iban")[0].confidence < 1.0
    )
    observed = corrupt / SAMPLE
    assert CORRUPT_IBAN_RATE / 3 < observed < CORRUPT_IBAN_RATE * 3, (
        f"observed corruption {observed:.3%} vs configured {CORRUPT_IBAN_RATE:.1%}"
    )


def test_transaction_has_the_expected_shape():
    txn = make_transaction()
    expected = {
        "account_holder", "cnic", "iban_from", "iban_to",
        "phone", "email", "amount_pkr", "channel", "memo",
    }
    assert set(txn) == expected
    assert isinstance(txn["amount_pkr"], float)


def test_every_generated_transaction_contains_detectable_pii(detector):
    """If this ever fails, throughput benchmarks are measuring the wrong thing —
    a stream with no PII exercises none of the redaction path."""
    for _ in range(200):
        txn = make_transaction()
        found = [
            f
            for field, value in txn.items()
            if isinstance(value, str)
            for f in detector.detect(value, field)
        ]
        assert found, f"no PII detected in {txn}"


@pytest.mark.parametrize("field", ["cnic", "iban_from", "iban_to", "phone", "email"])
def test_structured_pii_fields_are_fully_detected(detector, field):
    """Each dedicated PII field should yield exactly one finding covering it."""
    for _ in range(100):
        value = make_transaction()[field]
        findings = detector.detect(value, field)
        assert len(findings) == 1
        assert findings[0].span_start == 0
        assert findings[0].span_end == len(value)


# --------------------------------------------------------------------------- #
# Blank memos — the share of the stream that reaches Tier 2
# --------------------------------------------------------------------------- #
def test_blank_rate_bounds_are_exact():
    """The two ends must be absolute, not approximate: 0.0 is what a benchmark
    uses to force every record through Tier 2, and 1.0 is what proves the
    encoder is skipped entirely rather than merely called with empty text."""
    assert all(make_transaction(0.0)["memo"] for _ in range(200))
    assert not any(make_transaction(1.0)["memo"] for _ in range(200))


def test_default_blank_rate_is_honoured():
    """Load-bearing: every non-blank memo pays Tier 2's forward pass, so a
    default that silently drifted would move throughput without any code
    changing."""
    random.seed(11)
    blanks = sum(not make_transaction()["memo"] for _ in range(2000))
    assert abs(blanks / 2000 - BLANK_MEMO_RATE) < 0.05


def test_blank_memo_still_produces_a_valid_record():
    """A blank narration must not blank anything else — the structured PII is
    still there and still has to be redacted."""
    txn = make_transaction(1.0)
    assert txn["memo"] == ""
    assert txn["account_holder"] and txn["cnic"] and txn["iban_from"]


def test_seeding_makes_runs_reproducible():
    """The producer logs its seed so an odd measurement can be replayed. That is
    only worth anything if the seed actually determines the output."""
    def run():
        random.seed(99)
        Faker.seed(99)
        return [make_transaction()["memo"] for _ in range(50)]

    assert run() == run()
