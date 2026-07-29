"""Audit writer.

Two things are being protected here, and both are claims the README makes:

  1. IDEMPOTENCY. At-least-once delivery is only safe because a redelivered
     message upserts its audit row instead of inserting a second one. If the
     upsert or the findings delete-and-reinsert regresses, the guarantee
     silently becomes false — nothing else in the system would notice.

  2. THE AUDIT TRAIL STORES NO PII. It records that a CNIC was found at offset
     12-27 of a field, never the CNIC. An audit table that accumulates the very
     data the product exists to protect would be the worst possible bug.
"""
from __future__ import annotations

import pytest

from conftest import RecordingCursor, make_message
from pipelineguard import processor as P
from pipelineguard.audit import AuditRecord, AuditWriter
from pipelineguard.models import Envelope, Finding, Tier


@pytest.fixture
def writer(recording_conn):
    return AuditWriter("postgresql://ignored")


def test_records_one_upsert_per_message(writer, recording_conn, clean_payload):
    writer.record(Envelope(payload=clean_payload), "txn.raw", [], "clean", 1.0)
    assert len(recording_conn.statements_starting("INSERT INTO messages_processed")) == 1


def test_message_upsert_is_keyed_on_message_id(writer, recording_conn):
    """ON CONFLICT (message_id) DO UPDATE is what makes replay harmless."""
    writer.record(Envelope(payload={}), "txn.raw", [], "clean", 1.0)
    sql, _ = recording_conn.statements[0]
    assert "ON CONFLICT (message_id) DO UPDATE" in sql


def test_replaying_the_same_message_does_not_duplicate_findings(
    writer, recording_conn, detector, pii_payload
):
    """A redelivered message must clear its previous findings before writing
    new ones, otherwise findings accumulate on every replay."""
    outcome = P.process_message(make_message(pii_payload), detector)
    for _ in range(3):
        writer.record(outcome.envelope, "txn.raw", outcome.findings, outcome.action, 1.0)

    deletes = recording_conn.statements_starting("DELETE FROM findings")
    inserts = recording_conn.statements_starting("INSERT INTO findings")
    assert len(deletes) == 3
    assert len(inserts) == 3 * len(outcome.findings)


def test_findings_delete_precedes_findings_insert(writer, recording_conn, detector, pii_payload):
    outcome = P.process_message(make_message(pii_payload), detector)
    writer.record(outcome.envelope, "txn.raw", outcome.findings, outcome.action, 1.0)

    verbs = [s.strip().split()[0] + " " + s.strip().split()[2] for s, _ in recording_conn.statements]
    assert verbs.index("DELETE findings") < verbs.index("INSERT findings")


def test_each_record_commits_once(writer, recording_conn):
    writer.record(Envelope(payload={}), "txn.raw", [], "clean", 1.0)
    assert recording_conn.commits == 1


def test_close_releases_the_connection(writer, recording_conn):
    writer.close()
    assert recording_conn.closed


# --------------------------------------------------------------------------- #
# No PII in the audit trail
# --------------------------------------------------------------------------- #
def test_no_payload_value_ever_reaches_the_database(
    writer, recording_conn, detector, pii_payload
):
    outcome = P.process_message(make_message(pii_payload), detector)
    writer.record(outcome.envelope, "txn.raw", outcome.findings, outcome.action, 1.0)

    written = " ".join(str(p) for p in recording_conn.all_params_flat())
    for value in pii_payload.values():
        if isinstance(value, str):
            assert value not in written, f"{value!r} leaked into the audit trail"


def test_findings_row_carries_type_and_span_but_not_value(writer, recording_conn):
    finding = Finding("CNIC", "cnic", 0, 15, Tier.RULES, 1.0)
    writer.record(Envelope(payload={}), "txn.raw", [finding], "redacted", 1.0)

    _, params = recording_conn.statements_starting("INSERT INTO findings")[0]
    assert "CNIC" in params and 0 in params and 15 in params
    assert len(params) == 8   # message_id, type, field, start, end, tier, confidence, action


# --------------------------------------------------------------------------- #
# Failure columns
# --------------------------------------------------------------------------- #
def test_failure_is_recorded_for_fail_closed_quarantines(writer, recording_conn, detector):
    from conftest import StubMessage

    outcome = P.process_message(StubMessage(b"\xff\xfe binary"), detector)
    writer.record(
        outcome.envelope, "txn.raw", outcome.findings,
        outcome.action, 1.0, failure=outcome.failure,
    )
    _, params = recording_conn.statements[0]
    assert params[-2] == "UnicodeDecodeError"
    assert params[-1] and "utf-8" in params[-1]


def test_failure_columns_are_null_for_healthy_messages(writer, recording_conn, clean_payload):
    writer.record(Envelope(payload=clean_payload), "txn.raw", [], "clean", 1.0)
    _, params = recording_conn.statements[0]
    assert params[-2] is None and params[-1] is None


def test_max_tier_reflects_the_highest_tier_that_fired(writer, recording_conn):
    findings = [
        Finding("EMAIL", "email", 0, 5, Tier.RULES, 1.0),
        Finding("PERSON_NAME", "memo", 6, 9, Tier.ENCODER, 0.9),
    ]
    writer.record(Envelope(payload={}), "txn.raw", findings, "redacted", 1.0)
    _, params = recording_conn.statements[0]
    assert params[3] == int(Tier.ENCODER)


def test_max_tier_is_zero_when_nothing_was_found(writer, recording_conn):
    writer.record(Envelope(payload={}), "txn.raw", [], "clean", 1.0)
    _, params = recording_conn.statements[0]
    assert params[3] == 0


# --------------------------------------------------------------------------- #
# Batch writes
# --------------------------------------------------------------------------- #
def make_records(n: int, findings: list[Finding] | None = None) -> list[AuditRecord]:
    return [
        AuditRecord(Envelope(payload={}), "txn.raw", findings or [], "redacted" if findings else "clean", 1.0)
        for _ in range(n)
    ]


def test_record_batch_with_no_records_is_a_noop(writer, recording_conn):
    writer.record_batch([])
    assert recording_conn.statements == []
    assert recording_conn.commits == 0


def test_record_batch_writes_one_upsert_row_per_record(writer, recording_conn):
    writer.record_batch(make_records(4))
    assert len(recording_conn.statements_starting("INSERT INTO messages_processed")) == 4


def test_record_batch_commits_exactly_once_regardless_of_batch_size(writer, recording_conn):
    """The whole point of batching: N records must not mean N commits — that's
    the round trip the feature exists to remove."""
    writer.record_batch(make_records(5))
    assert recording_conn.commits == 1


def test_record_batch_uses_one_round_trip_per_statement_type(writer, recording_conn):
    """A 5-record batch must not turn into 5x the SQL round trips: the message
    upsert and findings insert go through executemany (one call, many rows),
    and the findings delete collapses to a single statement over every
    message_id in the batch. A loop that calls record() N times would pass
    every other assertion here but show 10 executemany + 5 execute calls
    instead of 2 and 1."""
    findings = [Finding("CNIC", "cnic", 0, 5, Tier.RULES, 1.0)]
    writer.record_batch(make_records(5, findings))
    assert recording_conn.calls.count("executemany") == 2   # messages_processed, findings
    assert recording_conn.calls.count("execute") == 1       # single DELETE over all ids


def test_record_batch_deletes_findings_for_every_message_id_in_one_statement(writer, recording_conn):
    records = make_records(3)
    writer.record_batch(records)
    deletes = recording_conn.statements_starting("DELETE FROM findings")
    assert len(deletes) == 1
    _, params = deletes[0]
    assert set(params[0]) == {r.envelope.message_id for r in records}


def test_record_batch_deletes_findings_before_inserting_new_ones(writer, recording_conn):
    findings = [Finding("EMAIL", "email", 0, 5, Tier.RULES, 1.0)]
    writer.record_batch(make_records(3, findings))

    verbs = [s.strip().split()[0] + " " + s.strip().split()[2] for s, _ in recording_conn.statements]
    assert verbs.index("DELETE findings") < verbs.index("INSERT findings")


def test_record_batch_keeps_findings_attributed_to_the_right_message(writer, recording_conn):
    """Findings from different records in the same batch must not cross-link
    to the wrong message_id."""
    rec_a = AuditRecord(
        Envelope(payload={}), "txn.raw",
        [Finding("CNIC", "cnic", 0, 5, Tier.RULES, 1.0)], "redacted", 1.0,
    )
    rec_b = AuditRecord(
        Envelope(payload={}), "txn.raw",
        [Finding("EMAIL", "email", 0, 5, Tier.RULES, 1.0)], "redacted", 1.0,
    )
    writer.record_batch([rec_a, rec_b])

    inserts = recording_conn.statements_starting("INSERT INTO findings")
    by_message_id = {params[0]: params[1] for _, params in inserts}
    assert by_message_id[rec_a.envelope.message_id] == "CNIC"
    assert by_message_id[rec_b.envelope.message_id] == "EMAIL"


@pytest.mark.parametrize("order", ["clean_first", "dirty_first"])
def test_record_batch_handles_a_mixed_batch_of_clean_and_findings_records(writer, recording_conn, order):
    """Every other batch test uses a uniform batch (all-clean or all-findings),
    which would hide a positional-zip bug where findings get attributed by
    list index instead of by message_id. A mixed batch is the realistic
    shape and the one that would actually catch it."""
    clean = AuditRecord(Envelope(payload={}), "txn.raw", [], "clean", 1.0)
    dirty = AuditRecord(
        Envelope(payload={}), "txn.raw",
        [Finding("CNIC", "cnic", 0, 5, Tier.RULES, 1.0)], "redacted", 1.0,
    )
    batch = [clean, dirty] if order == "clean_first" else [dirty, clean]
    writer.record_batch(batch)

    assert len(recording_conn.statements_starting("INSERT INTO messages_processed")) == 2
    inserts = recording_conn.statements_starting("INSERT INTO findings")
    assert len(inserts) == 1
    _, params = inserts[0]
    assert params[0] == dirty.envelope.message_id
    assert params[1] == "CNIC"


def test_record_batch_rolls_back_and_does_not_commit_on_a_mid_batch_failure(
    writer, recording_conn, monkeypatch
):
    """conn.transaction() is what makes this structural: without it, a
    mid-batch exception leaves the connection aborted with no commit ever
    having happened, and — the danger — no rollback either, poisoning it for
    every statement after."""

    def boom(self, sql, params=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(RecordingCursor, "execute", boom)

    with pytest.raises(RuntimeError):
        writer.record_batch(make_records(3, [Finding("CNIC", "cnic", 0, 5, Tier.RULES, 1.0)]))

    assert recording_conn.commits == 0
    assert recording_conn.rollbacks == 1


def test_record_batch_never_leaks_payload_values(writer, recording_conn, detector, pii_payload):
    outcome = P.process_message(make_message(pii_payload), detector)
    record = AuditRecord(outcome.envelope, "txn.raw", outcome.findings, outcome.action, 1.0)
    writer.record_batch([record])

    written = " ".join(str(p) for p in recording_conn.all_params_flat())
    for value in pii_payload.values():
        if isinstance(value, str):
            assert value not in written, f"{value!r} leaked into the audit trail"
