"""Stream processor: txn.raw -> detect/redact -> txn.clean | txn.quarantine + audit.

Delivery semantics: AT-LEAST-ONCE with idempotent audit writes.

Two classes of failure, handled deliberately differently — conflating them
produces either an infinite crash loop or silent data loss:

  message-level (bad bytes, wrong payload shape, detector raised)
      Deterministic: it will fail identically on replay. Crashing would wedge
      the pipeline forever on one record. So we FAIL CLOSED — route to
      quarantine with the reason recorded, commit the offset, move on.

  infrastructure (broker unreachable, Postgres down, delivery unconfirmed)
      Transient: likely to succeed on replay. We do NOT commit; the exception
      is allowed to kill the process, and a restart replays from the last
      committed offset. Crash-and-replay.

The dividing line is literal: message-level work happens inside
process_message()'s try block; everything below it in the loop is
infrastructure and is left to propagate.

Ordering invariant: AUDIT BEFORE EMIT. The audit row is written before the
message is produced downstream, so no record is ever emitted that has not
already been recorded. A Postgres outage therefore halts the pipeline rather
than letting unlogged data through — the correct fail-closed behaviour for a
governance tool.

Commit invariant: the offset is committed only after the produce is confirmed
durable (flush drained AND no delivery error). commit() is asynchronous by
design: safety comes from idempotent audit upserts, not from commit synchrony,
and a synchronous commit would add a coordinator round trip (~ms) to a
per-message budget measured in microseconds.

Run:
    python -m pipelineguard.processor [--stats-every 5] [--log-level INFO]
Verify end-to-end:
    python scripts/create_topics.py
    python -m pipelineguard.producer --rate 50 --count 500
    python -m pipelineguard.processor
    -> SELECT action, count(*) FROM messages_processed GROUP BY action;
    -> SELECT failure_class, count(*) FROM messages_processed
       WHERE failure_class IS NOT NULL GROUP BY failure_class;
"""
from __future__ import annotations

import argparse
import logging
import time
import uuid
from dataclasses import dataclass

from confluent_kafka import Consumer, Producer

from pipelineguard.audit import AuditWriter
from pipelineguard.config import settings
from pipelineguard.detectors.tier1_rules import RulesDetector
from pipelineguard.models import Envelope, Finding
from pipelineguard.observability import StatsReporter, setup_logging

log = logging.getLogger("pipelineguard.processor")

_FAILURE_DETAIL_MAXLEN = 500


class DeliveryFailed(RuntimeError):
    """A produce could not be confirmed durable. Infrastructure-class: raised so
    it kills the process, leaving the offset uncommitted for replay."""


class DeliveryTracker:
    """One instance per produced message. librdkafka invokes it from
    poll()/flush() with the delivery result."""

    __slots__ = ("error",)

    def __init__(self) -> None:
        self.error = None

    def __call__(self, err, msg) -> None:
        if err is not None:
            self.error = err


def poison_id(msg) -> str:
    """Deterministic audit id for a message whose own id we cannot read.

    (topic, partition, offset) uniquely and permanently identifies one physical
    record: offsets are assigned at append time and never reused — compaction
    and retention delete records but never reassign offsets to survivors. So
    the same bad message replayed ten times upserts one audit row, while two
    genuinely distinct messages with identical bytes remain two rows.

    A uuid4() here would key the row on WHEN the code ran rather than on WHICH
    message it was, and every replay would insert a duplicate.
    """
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"kafka://{msg.topic()}/{msg.partition()}/{msg.offset()}",
        )
    )


def redact(text: str, findings: list[Finding]) -> str:
    """Replace each finding's span with [ENTITY_TYPE].

    Right-to-left: each replacement changes the string length, so applying them
    left-to-right would invalidate every span after the first.
    """
    for finding in sorted(findings, key=lambda x: x.span_start, reverse=True):
        text = (
            text[: finding.span_start]
            + f"[{finding.entity_type}]"
            + text[finding.span_end :]
        )
    return text


@dataclass
class Outcome:
    envelope: Envelope
    findings: list[Finding]
    action: str                          # 'clean' | 'redacted' | 'quarantined'
    out_bytes: bytes
    failure: tuple[str, str] | None      # (exception class, detail) if fail-closed


def process_message(msg, detector: RulesDetector) -> Outcome:
    """Parse, detect, redact and route one message. Never raises.

    Any unexpected error routes to quarantine rather than propagating: fail
    closed, never toward the clean topic. This is what makes crash-and-replay
    safe for the infrastructure layer above — a poisonous record can never
    trigger the replay path.
    """
    raw = msg.value()
    try:
        envelope = Envelope.from_bytes(raw)
        if not isinstance(envelope.payload, dict):
            raise TypeError(
                f"payload is {type(envelope.payload).__name__}, expected object"
            )

        findings_by_field: dict[str, list[Finding]] = {}
        for field, value in envelope.payload.items():
            if isinstance(value, str):
                findings_by_field[field] = detector.detect(value, field)

        all_findings = [f for fs in findings_by_field.values() for f in fs]

        if not all_findings:
            out = Envelope(
                payload=envelope.payload,
                message_id=envelope.message_id,
                event_ts=envelope.event_ts,
                schema_version=envelope.schema_version,
            )
            return Outcome(envelope, [], "clean", out.to_bytes(), None)

        if any(f.confidence < 1.0 for f in all_findings):
            # Uncertain detection -> human review. Forward the ORIGINAL bytes so
            # the reviewer sees exactly what arrived.
            return Outcome(envelope, all_findings, "quarantined", raw, None)

        redacted_payload = {
            field: redact(value, findings_by_field[field])
            if field in findings_by_field
            else value
            for field, value in envelope.payload.items()
        }
        out = Envelope(
            payload=redacted_payload,
            message_id=envelope.message_id,
            event_ts=envelope.event_ts,
            schema_version=envelope.schema_version,
        )
        return Outcome(envelope, all_findings, "redacted", out.to_bytes(), None)

    except Exception as exc:  # noqa: BLE001 — deliberate fail-closed bucket
        log.warning(
            "fail-closed quarantine at %s/%s@%s: %s",
            msg.topic(), msg.partition(), msg.offset(), exc,
            exc_info=log.isEnabledFor(logging.DEBUG),
        )
        return Outcome(
            envelope=Envelope(payload={}, message_id=poison_id(msg)),
            findings=[],
            action="quarantined",
            out_bytes=raw if raw is not None else b"",
            failure=(type(exc).__name__, str(exc)[:_FAILURE_DETAIL_MAXLEN]),
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stats-every", type=float, default=5.0, help="stats line interval (s)")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--flush-timeout", type=float, default=10.0, help="per-message flush timeout (s)")
    args = ap.parse_args()

    setup_logging(args.log_level)

    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap,
            "group.id": settings.consumer_group,
            "enable.auto.commit": False,   # offsets committed manually, post-processing
            "auto.offset.reset": "earliest",
        }
    )
    producer = Producer(
        {
            "bootstrap.servers": settings.kafka_bootstrap,
            "enable.idempotence": True,    # no producer-side duplicates on retry
        }
    )
    detector = RulesDetector()
    audit = AuditWriter()
    stats = StatsReporter(log, interval_s=args.stats_every)

    consumer.subscribe([settings.topic_txn_raw])
    log.info(
        "processor started | %s -> {%s, %s} | group=%s",
        settings.topic_txn_raw,
        settings.topic_txn_clean,
        settings.topic_txn_quarantine,
        settings.consumer_group,
    )

    try:
        while True:
            msg = consumer.poll(1.0)
            stats.maybe_report()
            if msg is None:
                continue
            if msg.error():
                # Broker-side notice (rebalance, EOF, etc). Nothing to audit.
                log.error("consumer error: %s", msg.error())
                continue

            t0 = time.perf_counter()
            outcome = process_message(msg, detector)
            latency_ms = (time.perf_counter() - t0) * 1000

            # ---- infrastructure layer: exceptions below here kill the process ----
            audit.record(
                outcome.envelope,
                msg.topic(),
                outcome.findings,
                outcome.action,
                latency_ms,
                failure=outcome.failure,
            )

            target = (
                settings.topic_txn_quarantine
                if outcome.action == "quarantined"
                else settings.topic_txn_clean
            )
            tracker = DeliveryTracker()
            headers = (
                [("failure_class", outcome.failure[0].encode())] if outcome.failure else None
            )
            producer.produce(
                target,
                key=outcome.envelope.message_id.encode(),
                value=outcome.out_bytes,
                headers=headers,
                on_delivery=tracker,
            )

            # Both checks are needed: `remaining` catches "still queued when the
            # timeout expired"; tracker.error catches "left the queue by failing".
            # Neither implies the other, and either means the message is not durable.
            remaining = producer.flush(args.flush_timeout)
            if remaining or tracker.error:
                raise DeliveryFailed(
                    f"{target} <- {msg.topic()}/{msg.partition()}@{msg.offset()}: "
                    f"unflushed={remaining} error={tracker.error}"
                )

            consumer.commit(msg)
            stats.record(outcome.action, latency_ms, failed=outcome.failure is not None)
            log.debug(
                "%s %s/%s@%s -> %s (%d findings, %.2fms)",
                outcome.action, msg.topic(), msg.partition(), msg.offset(),
                target, len(outcome.findings), latency_ms,
            )

    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        stats.report(final=True)
        producer.flush(5)
        consumer.close()   # clean group leave => no rebalance delay on restart
        audit.close()


if __name__ == "__main__":
    main()
