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

from conftest import make_message
from pipelineguard import processor as P
from pipelineguard.audit import AuditWriter
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
