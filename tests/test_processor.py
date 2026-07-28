"""Processor decision logic.

`process_message` is pure — message in, Outcome out, no I/O — which is what
makes this file possible without a broker. The Kafka-facing half of the loop
(commit ordering, flush confirmation) is covered by integration tests instead;
see tests/test_delivery_semantics.py.

The invariant behind most of these tests: FAIL CLOSED. Anything unexpected
routes to quarantine, never to the clean topic.
"""
from __future__ import annotations

import json

import pytest

from conftest import StubMessage, make_message, raw_message
from pipelineguard import processor as P
from pipelineguard.models import Envelope


def payload_of(outcome) -> dict:
    return json.loads(outcome.out_bytes.decode())["payload"]


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def test_message_without_pii_is_clean(detector, clean_payload):
    outcome = P.process_message(make_message(clean_payload), detector)
    assert outcome.action == "clean"
    assert outcome.findings == []
    assert outcome.failure is None
    assert payload_of(outcome) == clean_payload


def test_message_with_confident_pii_is_redacted(detector, pii_payload):
    outcome = P.process_message(make_message(pii_payload), detector)
    assert outcome.action == "redacted"
    assert outcome.failure is None
    assert len(outcome.findings) >= 4


def test_message_with_uncertain_pii_is_quarantined(detector, pii_payload):
    """A checksum-failing IBAN yields confidence < 1.0, which must route the
    whole record to quarantine for review."""
    pii_payload["iban_from"] = "PK99MEZN5748718428058488"   # mod-97 fails
    outcome = P.process_message(make_message(pii_payload), detector)
    assert outcome.action == "quarantined"


def test_quarantined_message_forwards_original_bytes_unredacted(detector, pii_payload):
    """The reviewer must see exactly what arrived, not a redacted copy."""
    pii_payload["iban_from"] = "PK99MEZN5748718428058488"
    msg = make_message(pii_payload)
    outcome = P.process_message(msg, detector)
    assert outcome.out_bytes == msg.value()


def test_non_string_fields_are_passed_through_untouched(detector):
    payload = {"amount_pkr": 1500.0, "retries": 3, "ok": True, "tags": ["a"]}
    outcome = P.process_message(make_message(payload), detector)
    assert payload_of(outcome) == payload


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_redaction_replaces_entities_with_type_placeholders(detector):
    outcome = P.process_message(make_message({"cnic": "35202-1234567-1"}), detector)
    assert payload_of(outcome)["cnic"] == "[CNIC]"


def test_redacted_output_contains_none_of_the_original_entity_values(detector, pii_payload):
    """The load-bearing invariant of the whole product: nothing that was
    detected may survive into the clean topic."""
    outcome = P.process_message(make_message(pii_payload), detector)
    emitted = outcome.out_bytes.decode()
    for field, value in pii_payload.items():
        if not isinstance(value, str):
            continue
        for finding in detector.detect(value, field):
            assert value[finding.span_start : finding.span_end] not in emitted


def test_multiple_entities_in_one_field_are_all_redacted(detector):
    memo = "CNIC 35202-1234567-1, phone 03001234567, mail a@example.com"
    outcome = P.process_message(make_message({"memo": memo}), detector)
    redacted = payload_of(outcome)["memo"]
    assert "[CNIC]" in redacted and "[PHONE_PK]" in redacted and "[EMAIL]" in redacted
    assert not any(ch.isdigit() for ch in redacted.replace("[PHONE_PK]", ""))


def test_redact_applies_spans_right_to_left(detector):
    """Left-to-right would shift every subsequent span; this text has two
    entities of different lengths so the bug would corrupt the second."""
    text = "a@example.com and 35202-1234567-1"
    findings = detector.detect(text, "memo")
    assert P.redact(text, findings) == "[EMAIL] and [CNIC]"


def test_unredacted_text_is_returned_unchanged():
    assert P.redact("nothing here", []) == "nothing here"


# --------------------------------------------------------------------------- #
# Fail-closed: every malformed shape must quarantine, never crash
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "label, raw, expected_failure",
    [
        ("truncated json", b"{not json", "JSONDecodeError"),
        ("empty value", b"", "JSONDecodeError"),
        ("missing payload key", b'{"message_id":"x"}', "KeyError"),
        ("non-utf8 bytes", b"\xff\xfe\x00binary", "UnicodeDecodeError"),
        ("payload is a string", json.dumps({"message_id": "x", "event_ts": "t", "payload": "oops"}).encode(), "TypeError"),
        ("payload is a list", json.dumps({"message_id": "x", "event_ts": "t", "payload": [1, 2]}).encode(), "TypeError"),
        ("payload is null", json.dumps({"message_id": "x", "event_ts": "t", "payload": None}).encode(), "TypeError"),
    ],
)
def test_malformed_messages_quarantine_with_a_recorded_reason(
    detector, label, raw, expected_failure
):
    outcome = P.process_message(StubMessage(raw), detector)
    assert outcome.action == "quarantined", label
    assert outcome.failure is not None, label
    assert outcome.failure[0] == expected_failure, label


def test_failure_detail_is_bounded(detector):
    """Audit columns must not become a dumping ground for arbitrary text."""
    outcome = P.process_message(StubMessage(b"{" + b"x" * 5000), detector)
    assert len(outcome.failure[1]) <= P._FAILURE_DETAIL_MAXLEN


def test_poison_message_forwards_raw_bytes(detector):
    raw = b"\xff\xfe\x00binary"
    outcome = P.process_message(StubMessage(raw), detector)
    assert outcome.out_bytes == raw


def test_a_detector_that_raises_does_not_crash_the_processor(detector, pii_payload):
    """Fail closed covers our own bugs too: a broken detector must quarantine,
    never let unredacted data through to the clean topic."""

    class ExplodingDetector:
        def detect(self, text, field):
            raise RuntimeError("detector bug")

    outcome = P.process_message(make_message(pii_payload), ExplodingDetector())
    assert outcome.action == "quarantined"
    assert outcome.failure[0] == "RuntimeError"


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_message_id_is_preserved_across_the_pipeline(detector, pii_payload):
    """message_id is the join key between topics and the audit tables."""
    envelope = Envelope(payload=pii_payload)
    outcome = P.process_message(StubMessage(envelope.to_bytes()), detector)
    assert outcome.envelope.message_id == envelope.message_id
    assert json.loads(outcome.out_bytes)["message_id"] == envelope.message_id


def test_poison_id_is_stable_for_the_same_coordinates():
    msg = StubMessage(b"bad", topic="txn.raw", partition=2, offset=99)
    assert len({P.poison_id(msg) for _ in range(5)}) == 1


@pytest.mark.parametrize(
    "topic, partition, offset",
    [("txn.raw", 2, 100), ("txn.raw", 3, 99), ("other.raw", 2, 99)],
)
def test_poison_id_differs_when_any_coordinate_differs(topic, partition, offset):
    base = P.poison_id(StubMessage(b"bad", topic="txn.raw", partition=2, offset=99))
    other = P.poison_id(StubMessage(b"bad", topic=topic, partition=partition, offset=offset))
    assert base != other


def test_poison_id_ignores_message_content():
    """Two different bad payloads at the same offset cannot coexist — the
    coordinates identify the record, not its bytes."""
    a = P.poison_id(StubMessage(b"one", partition=1, offset=5))
    b = P.poison_id(StubMessage(b"two", partition=1, offset=5))
    assert a == b


def test_poison_id_is_a_valid_uuid():
    import uuid

    uuid.UUID(P.poison_id(StubMessage(b"bad")))   # raises if malformed
