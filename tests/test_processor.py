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

import json
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
