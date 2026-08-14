"""Processor decision logic.

`process_message` is pure — message in, Outcome out, no I/O — which is what
makes most of this file possible without a broker. The invariant behind most
of these tests: FAIL CLOSED. Anything unexpected routes to quarantine, never
to the clean topic.

The "Batch loop (main)" section at the bottom is different: main() builds its
own Consumer/Producer/AuditWriter, so those tests patch processor.Consumer,
.Producer, and .AuditWriter with the fakes in conftest.py (FakeConsumer,
FakeProducer, FakeAuditWriter) and drive main() itself. That proves the
wiring — error filtering, batch ordering, commit offsets — without a real
broker or database. It does not replace true integration testing against a
live Kafka/Postgres, which this suite doesn't attempt.
"""
from __future__ import annotations

import dataclasses
import json
import time
import uuid

import pytest
from confluent_kafka import KafkaError, KafkaException

from conftest import (
    ErrorMessage,
    StubMessage,
    make_message,
    patch_main_deps,
    raw_message,
)
from pipelineguard import processor as P
from pipelineguard.models import Envelope, Finding, Tier


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


def test_declared_field_is_redacted_not_detected(detector, pii_payload):
    """account_holder holds a name in every record by contract, and Tier 1 has
    no name rule — so before the schema rule existed this field reached the
    clean topic verbatim. Asserts the value is gone, not merely that a finding
    was recorded."""
    outcome = P.process_message(make_message(pii_payload), detector)

    assert outcome.action == "redacted"
    assert payload_of(outcome)["account_holder"] == "[PERSON_NAME]"
    assert "Ayesha Malik" not in outcome.out_bytes.decode()

    declared = [f for f in outcome.findings if f.field == "account_holder"]
    assert len(declared) == 1
    assert declared[0].entity_type == "PERSON_NAME"
    assert declared[0].confidence == 1.0


def test_declared_field_is_not_also_rule_scanned(detector):
    """The schema span already covers the whole field, so a rule hit inside it
    would be a redundant overlapping span — and redact() rewrites spans
    right-to-left assuming they do not overlap, so two spans here would corrupt
    the output rather than merely duplicate work."""
    payload = {"account_holder": "ayesha@example.com"}
    outcome = P.process_message(make_message(payload), detector)

    assert len(outcome.findings) == 1
    assert outcome.findings[0].entity_type == "PERSON_NAME"
    assert payload_of(outcome)["account_holder"] == "[PERSON_NAME]"


def test_empty_declared_field_stays_clean(detector):
    """No name present, so nothing to redact and no marker to invent."""
    outcome = P.process_message(make_message({"account_holder": ""}), detector)

    assert outcome.action == "clean"
    assert outcome.findings == []
    assert payload_of(outcome)["account_holder"] == ""


def test_undeclared_fields_still_go_through_rules(detector):
    """The dispatch must not have swallowed the normal path: a CNIC in an
    ordinary field is still detected by Tier 1."""
    payload = {"note": "CNIC 35202-1234567-1", "account_holder": "Ayesha Malik"}
    outcome = P.process_message(make_message(payload), detector)

    by_field = {f.field for f in outcome.findings}
    assert by_field == {"note", "account_holder"}
    assert "35202-1234567-1" not in outcome.out_bytes.decode()


# --------------------------------------------------------------------------- #
# Tier 2 injection, span merging, free-text dispatch
# --------------------------------------------------------------------------- #
def t2(field, start, end, etype="PERSON", conf=0.87):
    """An encoder finding, as main()'s batched pre-pass would supply it."""
    return Finding(
        entity_type=etype, field=field, span_start=start, span_end=end,
        tier=Tier.ENCODER, confidence=conf,
    )


def test_injected_tier2_findings_redact_free_text(detector):
    memo = "Transfer to Ayesha Malik"
    outcome = P.process_message(
        make_message({"memo": memo}), detector,
        {"memo": [t2("memo", memo.index("Ayesha"), len(memo))]},
    )

    assert outcome.action == "redacted"
    assert payload_of(outcome)["memo"] == "Transfer to [PERSON]"
    assert "Ayesha Malik" not in outcome.out_bytes.decode()


def test_tier2_findings_do_not_quarantine(detector):
    """Encoder scores are continuous, so the old `confidence < 1.0` predicate
    would send every record containing a name to review."""
    outcome = P.process_message(
        make_message({"memo": "Paid Ayesha Malik"}), detector,
        {"memo": [t2("memo", 5, 17, conf=0.31)]},
    )
    assert outcome.action == "redacted"


def test_tier1_checksum_failure_still_quarantines(detector):
    """The narrowing must not have disabled the Tier 1 case it exists for."""
    outcome = P.process_message(
        make_message({"iban_from": "PK99MEZN5748718428058488"}), detector,
    )
    assert outcome.action == "quarantined"


def test_free_text_gets_both_tiers(detector):
    """A memo carries embedded identifiers as well as names."""
    memo = "Refund to Ayesha Malik, call 03001234567"
    outcome = P.process_message(
        make_message({"memo": memo}), detector,
        {"memo": [t2("memo", memo.index("Ayesha"), memo.index(","))]},
    )

    tiers = {f.tier for f in outcome.findings}
    assert tiers == {Tier.RULES, Tier.ENCODER}
    body = outcome.out_bytes.decode()
    assert "Ayesha Malik" not in body and "03001234567" not in body


def test_encoder_finding_inside_a_rule_span_is_dropped(detector):
    """Tier 2 reads the local part of an email as a name. The rule span already
    covers those characters exactly, so the guess changes no redaction -- only
    the label and the audit counts. See findings §26."""
    memo = "Refund processed, notify at ayesha.malik@example.com"
    email_start = memo.index("ayesha")
    outcome = P.process_message(
        make_message({"memo": memo}), detector,
        {"memo": [t2("memo", email_start, email_start + len("ayesha.malik"))]},
    )

    redacted = payload_of(outcome)["memo"]
    assert redacted == "Refund processed, notify at [EMAIL]"
    assert "ayesha" not in redacted
    assert {f.tier for f in outcome.findings} == {Tier.RULES}


def test_encoder_finding_overlapping_a_rule_span_is_kept(detector):
    """The leak guard, stated where it actually applies. A span that pokes out
    past the rule span must survive: dropping it would leave the characters
    outside the rule span in the clear."""
    memo = "Refund processed, notify Ayesha at ayesha.malik@example.com"
    outcome = P.process_message(
        make_message({"memo": memo}), detector,
        # Starts at the free-standing 'Ayesha' and runs into the email.
        {"memo": [t2("memo", memo.index("Ayesha"),
                     memo.index("ayesha.malik") + len("ayesha.malik"))]},
    )

    redacted = payload_of(outcome)["memo"]
    assert "Ayesha" not in redacted
    assert "ayesha" not in redacted
    assert {f.tier for f in outcome.findings} == {Tier.RULES, Tier.ENCODER}


def test_merge_spans_unions_partial_overlap():
    """The leak case stated directly: dropping either span leaves characters."""
    findings = [
        Finding("EMAIL", "memo", 10, 30, Tier.RULES, 1.0),
        Finding("PERSON", "memo", 5, 15, Tier.ENCODER, 0.9),
    ]
    assert P.merge_spans(findings) == [(5, 30, "EMAIL+PERSON")]


def test_merge_spans_keeps_touching_spans_separate():
    findings = [
        Finding("EMAIL", "memo", 0, 5, Tier.RULES, 1.0),
        Finding("PHONE_PK", "memo", 5, 10, Tier.RULES, 1.0),
    ]
    assert P.merge_spans(findings) == [(0, 5, "EMAIL"), (5, 10, "PHONE_PK")]


# --------------------------------------------------------------------------- #
# shield / combine — keeping the encoder off characters a rule already owns
# --------------------------------------------------------------------------- #
def test_shield_blanks_a_rule_span_and_keeps_the_length():
    text = "notify at ayesha.malik@example.com now"
    findings = [Finding("EMAIL", "memo", 10, 34, Tier.RULES, 1.0)]

    out = P.shield(text, findings)

    assert len(out) == len(text)
    assert out == text[:10] + " " * 24 + text[34:]
    assert "example.com" not in out


def test_shield_without_findings_is_the_original_text():
    assert P.shield("nothing here", []) == "nothing here"


def test_shield_blanks_every_rule_span():
    text = "a@b.com and 35202-1234567-1 both"
    findings = [
        Finding("EMAIL", "memo", 0, 7, Tier.RULES, 1.0),
        Finding("CNIC", "memo", 12, 27, Tier.RULES, 1.0),
    ]

    out = P.shield(text, findings)

    assert out == " " * 7 + " and " + " " * 15 + " both"
    assert len(out) == len(text)


def test_combine_drops_an_encoder_finding_inside_a_rule_span():
    rule = Finding("EMAIL", "memo", 10, 30, Tier.RULES, 1.0)
    inside = Finding("PERSON", "memo", 12, 20, Tier.ENCODER, 0.6)

    assert P.combine([rule], [inside]) == [rule]


def test_combine_drops_an_encoder_finding_matching_a_rule_span_exactly():
    """The boundary. Equal bounds are containment, not a partial overlap."""
    rule = Finding("EMAIL", "memo", 10, 30, Tier.RULES, 1.0)
    same = Finding("ADDRESS", "memo", 10, 30, Tier.ENCODER, 0.6)

    assert P.combine([rule], [same]) == [rule]


@pytest.mark.parametrize("start,end", [(5, 20), (20, 40), (5, 40)])
def test_combine_keeps_an_encoder_finding_that_pokes_out(start, end):
    """Anything not fully covered must survive, or its outer part leaks."""
    rule = Finding("EMAIL", "memo", 10, 30, Tier.RULES, 1.0)
    overlap = Finding("ADDRESS", "memo", start, end, Tier.ENCODER, 0.9)

    assert P.combine([rule], [overlap]) == [rule, overlap]


def test_combine_keeps_a_disjoint_encoder_finding():
    rule = Finding("EMAIL", "memo", 10, 30, Tier.RULES, 1.0)
    other = Finding("PERSON", "memo", 40, 50, Tier.ENCODER, 0.9)

    assert P.combine([rule], [other]) == [rule, other]


def test_combine_without_rule_findings_keeps_everything():
    encoder = [Finding("PERSON", "memo", 0, 5, Tier.ENCODER, 0.9)]
    assert P.combine([], encoder) == encoder


def test_empty_memo_is_clean(detector):
    outcome = P.process_message(make_message({"memo": ""}), detector, {})
    assert outcome.action == "clean"
    assert outcome.findings == []


# --------------------------------------------------------------------------- #
# free_text_fields — the best-effort pre-pass
# --------------------------------------------------------------------------- #
def test_free_text_fields_extracts_only_free_text():
    raw = make_message(
        {"memo": "Paid Ayesha", "channel": "atm", "amount_pkr": 5.0}
    ).value()
    assert P.free_text_fields(raw) == {"memo": "Paid Ayesha"}


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_free_text_fields_skips_blank(value):
    """Blank memos must never enter the model batch."""
    assert P.free_text_fields(make_message({"memo": value}).value()) == {}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"",
        b"not json",
        b"{}",                                  # no payload key
        b'{"payload": "a string"}',             # payload not a dict
        b'{"payload": {"memo": 42}}',           # memo not a string
        "\udcff".encode("utf-8", "surrogatepass"),   # undecodable bytes
    ],
)
def test_free_text_fields_never_raises(raw):
    """Unreadable means absent from the batch, not an exception — process_message
    still quarantines the message through the normal fail-closed path."""
    assert P.free_text_fields(raw) == {}


def test_unreadable_message_still_quarantines_after_prepass(detector):
    """The pre-pass declining to read a message must not stop it being handled."""
    assert P.free_text_fields(b"not json") == {}
    outcome = P.process_message(StubMessage(b"not json"), detector, {})
    assert outcome.action == "quarantined"
    assert outcome.failure is not None


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
        ("payload is a string", json.dumps({"message_id": str(uuid.uuid4()), "event_ts": "2024-01-01T00:00:00+00:00", "payload": "oops"}).encode(), "TypeError"),
        ("payload is a list", json.dumps({"message_id": str(uuid.uuid4()), "event_ts": "2024-01-01T00:00:00+00:00", "payload": [1, 2]}).encode(), "TypeError"),
        ("payload is null", json.dumps({"message_id": str(uuid.uuid4()), "event_ts": "2024-01-01T00:00:00+00:00", "payload": None}).encode(), "TypeError"),
        ("message_id is not a uuid", json.dumps({"message_id": "not-a-uuid", "event_ts": "2024-01-01T00:00:00+00:00", "schema_version": 1, "payload": {}}).encode(), "ValueError"),
        ("message_id is an int", json.dumps({"message_id": 123, "event_ts": "2024-01-01T00:00:00+00:00", "schema_version": 1, "payload": {}}).encode(), "TypeError"),
        ("message_id is null", json.dumps({"message_id": None, "event_ts": "2024-01-01T00:00:00+00:00", "schema_version": 1, "payload": {}}).encode(), "TypeError"),
        ("event_ts is not iso8601", json.dumps({"message_id": str(uuid.uuid4()), "event_ts": "not-a-timestamp", "schema_version": 1, "payload": {}}).encode(), "ValueError"),
        ("event_ts is an int", json.dumps({"message_id": str(uuid.uuid4()), "event_ts": 12345, "schema_version": 1, "payload": {}}).encode(), "TypeError"),
        ("schema_version is not an int", json.dumps({"message_id": str(uuid.uuid4()), "event_ts": "2024-01-01T00:00:00+00:00", "schema_version": "abc", "payload": {}}).encode(), "TypeError"),
        ("schema_version is a bool", json.dumps({"message_id": str(uuid.uuid4()), "event_ts": "2024-01-01T00:00:00+00:00", "schema_version": True, "payload": {}}).encode(), "TypeError"),
    ],
)
def test_malformed_messages_quarantine_with_a_recorded_reason(
    detector, label, raw, expected_failure
):
    outcome = P.process_message(StubMessage(raw), detector)
    assert outcome.action == "quarantined", label
    assert outcome.failure is not None, label
    assert outcome.failure[0] == expected_failure, label


@pytest.mark.parametrize(
    "label, body",
    [
        ("message_id is not a uuid", {"message_id": "not-a-uuid", "event_ts": "2024-01-01T00:00:00+00:00", "schema_version": 1, "payload": {}}),
        ("event_ts is not iso8601", {"message_id": str(uuid.uuid4()), "event_ts": "not-a-timestamp", "schema_version": 1, "payload": {}}),
        ("schema_version is not an int", {"message_id": str(uuid.uuid4()), "event_ts": "2024-01-01T00:00:00+00:00", "schema_version": "abc", "payload": {}}),
    ],
)
def test_untrustworthy_envelope_fields_fall_back_to_poison_id(detector, label, body):
    """message_id is the very field that failed validation, so the audit key
    must fall back to the (topic, partition, offset)-derived poison id rather
    than trusting it."""
    msg = StubMessage(json.dumps(body).encode())
    outcome = P.process_message(msg, detector)
    assert outcome.envelope.message_id == P.poison_id(msg), label


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


@pytest.mark.parametrize(
    "label, wire_form",
    [
        ("urn prefix", "urn:uuid:550e8400-e29b-41d4-a716-446655440000"),
        ("braces", "{550e8400-e29b-41d4-a716-446655440000}"),
        ("no hyphens", "550e8400e29b41d4a716446655440000"),
    ],
)
def test_message_id_is_canonicalized_to_the_hyphenated_form(detector, label, wire_form):
    """uuid.UUID() accepts forms Postgres's uuid column does not (urn: prefix,
    braces, no hyphens). The canonical hyphenated form must be what's used
    everywhere downstream — audit key, emitted bytes, producer key — not the
    as-received string."""
    canonical = "550e8400-e29b-41d4-a716-446655440000"
    body = {"message_id": wire_form, "event_ts": "2024-01-01T00:00:00+00:00", "payload": {}}
    outcome = P.process_message(raw_message(body), detector)
    assert outcome.envelope.message_id == canonical, label
    assert json.loads(outcome.out_bytes)["message_id"] == canonical, label


@pytest.mark.parametrize(
    "label, wire_form, normalized",
    [
        ("Z suffix", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00+00:00"),
        ("Z suffix with microseconds", "2024-01-01T00:00:00.123456Z", "2024-01-01T00:00:00.123456+00:00"),
    ],
)
def test_event_ts_z_suffix_is_normalized_and_accepted(detector, label, wire_form, normalized):
    """datetime.fromisoformat() only learned trailing "Z" in Python 3.11; this
    project's floor is 3.10, where a Z-suffixed timestamp (the JS/Go default)
    must not quarantine a message that is actually valid."""
    body = {"message_id": str(uuid.uuid4()), "event_ts": wire_form, "payload": {}}
    outcome = P.process_message(raw_message(body), detector)
    assert outcome.action == "clean", label
    assert outcome.failure is None, label
    assert outcome.envelope.event_ts == normalized, label
    assert json.loads(outcome.out_bytes)["event_ts"] == normalized, label


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


# --------------------------------------------------------------------------- #
# Batch commit offsets
# --------------------------------------------------------------------------- #
def as_offset_map(topic_partitions) -> dict[tuple[str, int], int]:
    return {(tp.topic, tp.partition): tp.offset for tp in topic_partitions}


def test_highest_offsets_is_empty_for_an_empty_batch():
    assert P.highest_offsets([]) == []


def test_highest_offsets_commits_one_past_the_only_message():
    messages = [StubMessage(b"x", topic="txn.raw", partition=0, offset=5)]
    result = P.highest_offsets(messages)
    assert as_offset_map(result) == {("txn.raw", 0): 6}


def test_highest_offsets_takes_the_max_not_the_last_message_seen():
    """Order-within-partition holds today but must not be relied on: the
    highest offset must win even if it doesn't arrive last in the batch."""
    messages = [
        StubMessage(b"a", topic="txn.raw", partition=0, offset=10),
        StubMessage(b"b", topic="txn.raw", partition=0, offset=3),
        StubMessage(b"c", topic="txn.raw", partition=0, offset=7),
    ]
    result = P.highest_offsets(messages)
    assert as_offset_map(result) == {("txn.raw", 0): 11}


def test_highest_offsets_tracks_each_partition_independently():
    messages = [
        StubMessage(b"a", topic="txn.raw", partition=0, offset=5),
        StubMessage(b"b", topic="txn.raw", partition=1, offset=2),
        StubMessage(b"c", topic="txn.raw", partition=0, offset=9),
        StubMessage(b"d", topic="txn.raw", partition=1, offset=8),
    ]
    result = P.highest_offsets(messages)
    assert as_offset_map(result) == {("txn.raw", 0): 10, ("txn.raw", 1): 9}


def test_highest_offsets_tracks_each_topic_independently():
    """Two different topics can share a partition number; they must not be
    conflated into one commit position."""
    messages = [
        StubMessage(b"a", topic="txn.raw", partition=0, offset=5),
        StubMessage(b"b", topic="other.raw", partition=0, offset=20),
    ]
    result = P.highest_offsets(messages)
    assert as_offset_map(result) == {("txn.raw", 0): 6, ("other.raw", 0): 21}


@pytest.mark.parametrize(
    "code",
    [
        KafkaError.ILLEGAL_GENERATION,
        KafkaError.UNKNOWN_MEMBER_ID,
        KafkaError.REBALANCE_IN_PROGRESS,
    ],
)
def test_is_rebalance_error_is_true_for_the_three_tolerated_codes(code):
    assert P.is_rebalance_error(KafkaException(KafkaError(code))) is True


@pytest.mark.parametrize(
    "code",
    [
        KafkaError.UNKNOWN_TOPIC_OR_PART,      # 3 — not a group-membership issue
        KafkaError._MAX_POLL_EXCEEDED,         # -147 — this consumer fell behind, not the group
        KafkaError._AUTHENTICATION,            # -169 — the coordinator rejected credentials
    ],
)
def test_is_rebalance_error_is_false_for_everything_else(code):
    """Tolerance here is deliberately narrow: only the exact codes that mean
    'the group moved under us' are True. Anything else — including something
    that sounds adjacent, like falling behind on polling — must still crash
    and replay rather than be swallowed."""
    assert P.is_rebalance_error(KafkaException(KafkaError(code))) is False


# --------------------------------------------------------------------------- #
# Batch loop (main)
# --------------------------------------------------------------------------- #
def committed_offset_map(consumer) -> dict[tuple[str, int], int]:
    return {(tp.topic, tp.partition): tp.offset for batch in consumer.commits for tp in batch}


def test_main_processes_a_batch_and_commits_highest_offsets_per_partition(monkeypatch):
    msg1 = make_message({"note": "clean"}, topic="txn.raw", partition=0, offset=10)
    msg2 = make_message({"cnic": "35202-1234567-1"}, topic="txn.raw", partition=1, offset=20)
    consumer, producer, audit, log = patch_main_deps(monkeypatch, batches=[[msg1, msg2]])

    P.main([])

    assert len(audit.batches) == 1
    assert len(audit.batches[0]) == 2
    assert len(producer.produced) == 2
    assert committed_offset_map(consumer) == {("txn.raw", 0): 11, ("txn.raw", 1): 21}


def test_main_preserves_audit_before_emit_before_commit_across_multiple_batches(monkeypatch):
    """The batching change moves the granularity these invariants apply at,
    not their order: audit still precedes emit, commit still comes last —
    once per batch, repeated correctly across consecutive batches."""
    msg_a = make_message({"note": "a"}, topic="txn.raw", partition=0, offset=0)
    msg_b = make_message({"note": "b"}, topic="txn.raw", partition=0, offset=1)
    consumer, producer, audit, log = patch_main_deps(monkeypatch, batches=[[msg_a], [msg_b]])

    P.main([])

    assert log == ["audit", "produce", "commit", "audit", "produce", "commit"]


def test_main_skips_commit_and_audit_on_idle_and_error_only_batches(monkeypatch):
    """consume() returning [] is the normal idle case (linger expired, no
    traffic), not an error — it must not touch audit, produce, or commit.
    A batch containing only broker-side notices must behave the same way."""
    consumer, producer, audit, log = patch_main_deps(
        monkeypatch,
        batches=[[], [ErrorMessage(topic="txn.raw", partition=0, offset=5)]],
    )

    P.main([])

    assert audit.batches == []
    assert producer.produced == []
    assert consumer.commits == []
    assert log == []


def test_main_filters_error_entries_before_computing_the_commit_offset(monkeypatch):
    """An error entry carries a real topic/partition/offset. Letting it reach
    highest_offsets would silently advance the commit position past a message
    that was never processed — this batch's error entry sits at a HIGHER
    offset than the real message specifically to catch that."""
    good = make_message({"note": "clean"}, topic="txn.raw", partition=0, offset=5)
    err = ErrorMessage(topic="txn.raw", partition=0, offset=99)
    consumer, producer, audit, log = patch_main_deps(monkeypatch, batches=[[good, err]])

    P.main([])

    assert len(audit.batches[0]) == 1
    assert committed_offset_map(consumer) == {("txn.raw", 0): 6}


def test_main_raises_and_does_not_commit_when_any_delivery_in_the_batch_fails(monkeypatch):
    """One failed delivery among N must block the commit for all N — checking
    only the last tracker or only flush()'s return value would let a partial
    batch be marked complete. Here only the middle of 3 deliveries fails."""
    msgs = [
        make_message({"note": f"m{i}"}, topic="txn.raw", partition=0, offset=i)
        for i in range(3)
    ]
    consumer, producer, audit, log = patch_main_deps(
        monkeypatch, batches=[msgs], failing_indices=frozenset({1})
    )

    with pytest.raises(P.DeliveryFailed):
        P.main([])

    assert consumer.commits == []


def test_main_raises_and_does_not_commit_when_flush_leaves_messages_queued(monkeypatch):
    """The other half of the delivery check: every individual tracker can be
    clean while flush() still reports messages left in the queue."""
    msgs = [make_message({"note": "m"}, topic="txn.raw", partition=0, offset=0)]
    consumer, producer, audit, log = patch_main_deps(
        monkeypatch, batches=[msgs], flush_remaining=1
    )

    with pytest.raises(P.DeliveryFailed):
        P.main([])

    assert consumer.commits == []


@pytest.mark.parametrize(
    "code",
    [
        KafkaError.ILLEGAL_GENERATION,
        KafkaError.UNKNOWN_MEMBER_ID,
        KafkaError.REBALANCE_IN_PROGRESS,
    ],
)
def test_main_tolerates_a_rebalance_error_on_commit_and_keeps_looping(monkeypatch, code):
    """Proving main() doesn't crash on a tolerated commit error is weak — an
    implementation that swallows the exception and then silently breaks out
    of the loop would pass that too. The behaviour that actually matters is
    that the loop CONTINUES: batch two must still be processed and committed
    after batch one's commit is rejected for a tolerated reason."""
    msg_a = make_message({"note": "a"}, topic="txn.raw", partition=0, offset=0)
    msg_b = make_message({"note": "b"}, topic="txn.raw", partition=0, offset=1)
    consumer, producer, audit, log = patch_main_deps(
        monkeypatch,
        batches=[[msg_a], [msg_b]],
        commit_errors=[KafkaError(code)],
    )

    P.main([])   # must return normally (KeyboardInterrupt on batch 3), not propagate

    assert len(audit.batches) == 2
    assert len(producer.produced) == 2
    assert committed_offset_map(consumer) == {("txn.raw", 0): 2}   # only batch two's commit landed


def test_main_reraises_a_non_rebalance_commit_error(monkeypatch):
    """This is the test that stops someone 'simplifying' the predicate into a
    bare `except KafkaException: pass` — that would convert a genuine
    coordinator/infrastructure failure into silent data loss, exactly what
    crash-and-replay exists to prevent. UNKNOWN_TOPIC_OR_PART is not a group-
    membership issue and must kill the process like any other commit error."""
    msg = make_message({"note": "a"}, topic="txn.raw", partition=0, offset=0)
    consumer, producer, audit, log = patch_main_deps(
        monkeypatch,
        batches=[[msg]],
        commit_errors=[KafkaError(KafkaError.UNKNOWN_TOPIC_OR_PART)],
    )

    with pytest.raises(KafkaException):
        P.main([])

    assert consumer.commits == []


def test_main_still_audits_and_produces_a_batch_whose_commit_was_skipped(monkeypatch):
    """The work is done and recorded even though the bookmark didn't move:
    this batch's audit row and produced message both happen before the
    commit is attempted, and neither is undone when the commit is rejected
    for a tolerated reason. Only the commit is missing — the new partition
    owner will reprocess this batch, and the audit upsert (keyed on
    message_id) makes that harmless rather than a duplicate."""
    msg = make_message({"note": "a"}, topic="txn.raw", partition=0, offset=7)
    consumer, producer, audit, log = patch_main_deps(
        monkeypatch,
        batches=[[msg]],
        commit_errors=[KafkaError(KafkaError.REBALANCE_IN_PROGRESS)],
    )

    P.main([])

    assert len(audit.batches) == 1
    assert len(audit.batches[0]) == 1
    assert len(producer.produced) == 1
    assert consumer.commits == []


def test_main_exits_after_reaching_exit_after_count(monkeypatch):
    """The break lands after the commit and the stats loop, not before: if it
    broke earlier, the batch that crosses the threshold would be audited and
    produced but never committed, desyncing the benchmark's message count
    from what the audit table holds. Three batches of two are scripted;
    --exit-after 4 must stop after exactly two, leaving the third untouched."""
    batches = [
        [
            make_message({"note": "a1"}, topic="txn.raw", partition=0, offset=0),
            make_message({"note": "a2"}, topic="txn.raw", partition=0, offset=1),
        ],
        [
            make_message({"note": "b1"}, topic="txn.raw", partition=0, offset=2),
            make_message({"note": "b2"}, topic="txn.raw", partition=0, offset=3),
        ],
        [
            make_message({"note": "c1"}, topic="txn.raw", partition=0, offset=4),
            make_message({"note": "c2"}, topic="txn.raw", partition=0, offset=5),
        ],
    ]
    consumer, producer, audit, log = patch_main_deps(monkeypatch, batches=batches)

    P.main(["--exit-after", "4"])

    assert consumer.consume_calls == 2   # the third batch was never fetched
    assert len(audit.batches) == 2
    assert committed_offset_map(consumer) == {("txn.raw", 0): 4}   # batch two's commit landed


def test_main_exit_after_zero_runs_until_consume_is_exhausted(monkeypatch):
    """--exit-after 0 is the default and must mean 'run forever': the loop's
    only exit is the KeyboardInterrupt raised once the scripted batches run
    out, not the exit-after counter."""
    batches = [
        [make_message({"note": "a"}, topic="txn.raw", partition=0, offset=0)],
        [make_message({"note": "b"}, topic="txn.raw", partition=0, offset=1)],
        [make_message({"note": "c"}, topic="txn.raw", partition=0, offset=2)],
    ]
    consumer, producer, audit, log = patch_main_deps(monkeypatch, batches=batches)

    P.main(["--exit-after", "0"])

    assert consumer.consume_calls == 4   # 3 real batches + the exhausted-script call
    assert len(audit.batches) == 3
    assert committed_offset_map(consumer) == {("txn.raw", 0): 3}


# --------------------------------------------------------------------------- #
# Tier 2 in the batch loop
# --------------------------------------------------------------------------- #
class FakeTier2:
    """Records the batch it was handed, and flags the word 'Ayesha'."""

    def __init__(self, delay_ms=0.0):
        self.batches = []
        self.delay_ms = delay_ms

    def detect_batch(self, inputs):
        self.batches.append(inputs)
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        out = {}
        for key, fields in inputs.items():
            for field, text in fields.items():
                if "Ayesha" in text:
                    s = text.index("Ayesha")
                    out.setdefault(key, {})[field] = [
                        t2(field, s, s + len("Ayesha"))
                    ]
        return out


def run_main_with_tier2(monkeypatch, payloads, tier2, **kw):
    batch = [make_message(p) for p in payloads]
    consumer, producer, audit, log = patch_main_deps(monkeypatch, [batch], **kw)
    # Settings is a frozen dataclass, so swap the whole object rather than a
    # field. dataclasses.replace keeps every other setting as configured.
    monkeypatch.setattr(
        P, "settings", dataclasses.replace(P.settings, tier2_enabled=True)
    )
    monkeypatch.setattr(P, "_load_tier2", lambda: tier2)
    P.main(["--exit-after", str(len(batch)), "--stats-every", "999"])
    return consumer, producer, audit, log


def test_only_nonempty_free_text_is_escalated(monkeypatch):
    """A blank memo must never reach the model — that is the whole reason
    dispatch is on the field rather than on a prediction."""
    tier2 = FakeTier2()
    run_main_with_tier2(monkeypatch, [
        {"memo": "Transfer to Ayesha Malik"},
        {"memo": ""},
        {"memo": "   "},
        {"channel": "atm"},
    ], tier2)

    assert len(tier2.batches) == 1
    assert set(tier2.batches[0]) == {0}


def test_tier2_findings_reach_the_emitted_payload(monkeypatch):
    tier2 = FakeTier2()
    _c, producer, _a, _l = run_main_with_tier2(
        monkeypatch, [{"memo": "Transfer to Ayesha Malik"}], tier2
    )
    emitted = producer.produced[0]
    assert b"Ayesha" not in emitted["value"]
    assert b"PERSON" in emitted["value"]


def test_batch_makes_one_model_call_not_one_per_message(monkeypatch):
    """The entire reason for the pre-pass: per-message inference costs 29.4 ms
    against 7.2 batched."""
    tier2 = FakeTier2()
    run_main_with_tier2(
        monkeypatch, [{"memo": f"Ayesha {i}"} for i in range(12)], tier2
    )
    assert len(tier2.batches) == 1
    assert len(tier2.batches[0]) == 12


def test_tier2_cost_is_charged_to_escalated_records(monkeypatch):
    """The model call sits outside the per-message timer, so without explicit
    attribution the audit would report sub-millisecond latency for records whose
    real cost was milliseconds."""
    tier2 = FakeTier2(delay_ms=40)
    _c, _p, audit, _l = run_main_with_tier2(monkeypatch, [
        {"memo": "Transfer to Ayesha Malik"},
        {"memo": ""},
    ], tier2)

    by_memo = {r.envelope.payload.get("memo"): r.latency_ms
               for batch in audit.batches for r in batch}
    assert by_memo["Transfer to Ayesha Malik"] > 30    # carries the model cost
    assert by_memo[""] < 10                            # skipped it entirely
