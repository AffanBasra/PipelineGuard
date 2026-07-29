"""Idempotent Postgres audit writer.

Idempotency contract (this is what lets us claim at-least-once safely):
  * messages_processed is UPSERTed on message_id.
  * findings for that message_id are deleted and reinserted in the same
    transaction, so a redelivered Kafka message overwrites rather than
    duplicates its audit trail.

Batching: record() is a single-record convenience wrapper around
record_batch() rather than a separate SQL path — one path means one place to
fix a bug, and it means record()'s existing test coverage exercises the
batch path too. record_batch() writes N records in one transaction:
messages_processed and findings inserts go through executemany (one round
trip, many rows); the findings delete collapses further, to one DELETE over
every message_id in the batch rather than one per message.

record_batch() uses conn.transaction() rather than a manual commit(), so a
mid-batch exception rolls back instead of leaving the connection aborted
(InFailedSqlTransaction on every statement after) with no commit ever having
happened. Today an audit failure is infrastructure-class and kills the
process before a next call could observe that state — but the rollback is
what makes that a property of the caller, not an accident this method
depends on.

Known gap, not handled: a duplicate message_id within one batch (e.g. an
at-least-once producer re-sending inside the same poll) upserts the row
twice as expected, but its findings become the UNION of both records', not
a replacement — the single DELETE runs once before all inserts, not once
per occurrence. Cross-batch redelivery (the normal case this idempotency
contract targets) is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from pipelineguard.config import settings
from pipelineguard.models import Envelope, Finding

_UPSERT_MESSAGE = """
INSERT INTO messages_processed
    (message_id, source_topic, event_ts, max_tier, action, latency_ms, schema_version,
     failure_class, failure_detail)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (message_id) DO UPDATE SET
    max_tier = EXCLUDED.max_tier,
    action = EXCLUDED.action,
    latency_ms = EXCLUDED.latency_ms,
    failure_class = EXCLUDED.failure_class,
    failure_detail = EXCLUDED.failure_detail,
    processed_ts = now();
"""

# Cast to uuid[] explicitly: an uncast array parameter comes across as
# unknown/text[] and Postgres won't implicitly compare that against a uuid
# column, the same class of type mismatch the envelope-field validation
# closed on the ingest side.
_DELETE_FINDINGS = "DELETE FROM findings WHERE message_id = ANY(%s::uuid[]);"

_INSERT_FINDING = """
INSERT INTO findings
    (message_id, entity_type, field, span_start, span_end, tier, confidence, action)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
"""


@dataclass(frozen=True)
class AuditRecord:
    """Everything one audit row (plus its findings) needs — bundled so
    record_batch() takes a list of these instead of six parallel lists."""

    envelope: Envelope
    source_topic: str
    findings: list[Finding]
    action: str                                  # 'clean' | 'redacted' | 'quarantined'
    latency_ms: float
    failure: tuple[str, str] | None = None        # (exception class, detail)


class AuditWriter:
    def __init__(self, dsn: str = settings.postgres_dsn):
        self._conn = psycopg.connect(dsn, autocommit=False)

    def record(
        self,
        envelope: Envelope,
        source_topic: str,
        findings: list[Finding],
        action: str,                                  # 'clean' | 'redacted' | 'quarantined'
        latency_ms: float,
        failure: tuple[str, str] | None = None,       # (exception class, detail)
    ) -> None:
        self.record_batch(
            [AuditRecord(envelope, source_topic, findings, action, latency_ms, failure)]
        )

    def record_batch(self, records: list[AuditRecord]) -> None:
        if not records:
            return

        message_params = []
        message_ids = []
        finding_params = []
        for rec in records:
            max_tier = max((f.tier for f in rec.findings), default=0)
            failure_class, failure_detail = rec.failure if rec.failure else (None, None)
            message_params.append(
                (
                    rec.envelope.message_id,
                    rec.source_topic,
                    rec.envelope.event_ts,
                    int(max_tier),
                    rec.action,
                    rec.latency_ms,
                    rec.envelope.schema_version,
                    failure_class,
                    failure_detail,
                )
            )
            message_ids.append(rec.envelope.message_id)

            per_finding_action = "quarantined" if rec.action == "quarantined" else "redacted"
            for f in rec.findings:
                finding_params.append(
                    (
                        rec.envelope.message_id,
                        f.entity_type,
                        f.field,
                        f.span_start,
                        f.span_end,
                        int(f.tier),
                        f.confidence,
                        per_finding_action,
                    )
                )

        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.executemany(_UPSERT_MESSAGE, message_params)
            cur.execute(_DELETE_FINDINGS, (message_ids,))
            if finding_params:
                cur.executemany(_INSERT_FINDING, finding_params)

    def close(self) -> None:
        self._conn.close()
