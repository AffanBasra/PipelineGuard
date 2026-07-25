"""Idempotent Postgres audit writer.

Idempotency contract (this is what lets us claim at-least-once safely):
  * messages_processed is UPSERTed on message_id.
  * findings for that message_id are deleted and reinserted in the same
    transaction, so a redelivered Kafka message overwrites rather than
    duplicates its audit trail.
"""
from __future__ import annotations

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

_DELETE_FINDINGS = "DELETE FROM findings WHERE message_id = %s;"

_INSERT_FINDING = """
INSERT INTO findings
    (message_id, entity_type, field, span_start, span_end, tier, confidence, action)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
"""


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
        max_tier = max((f.tier for f in findings), default=0)
        failure_class, failure_detail = failure if failure else (None, None)
        with self._conn.cursor() as cur:
            cur.execute(
                _UPSERT_MESSAGE,
                (
                    envelope.message_id,
                    source_topic,
                    envelope.event_ts,
                    int(max_tier),
                    action,
                    latency_ms,
                    envelope.schema_version,
                    failure_class,
                    failure_detail,
                ),
            )
            cur.execute(_DELETE_FINDINGS, (envelope.message_id,))
            per_finding_action = "quarantined" if action == "quarantined" else "redacted"
            for f in findings:
                cur.execute(
                    _INSERT_FINDING,
                    (
                        envelope.message_id,
                        f.entity_type,
                        f.field,
                        f.span_start,
                        f.span_end,
                        int(f.tier),
                        f.confidence,
                        per_finding_action,
                    ),
                )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
