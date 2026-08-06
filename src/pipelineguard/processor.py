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

Commit invariant: the offset is committed only after every produce in the
batch is confirmed durable (flush drained AND no delivery error, checked
across the whole batch, not just the last message). commit() is asynchronous
by design: safety comes from idempotent audit upserts, not from commit
synchrony, and a synchronous commit would add a coordinator round trip (~ms)
to a per-message budget measured in microseconds.

Batching: the loop fetches up to --batch-size messages per consume() call,
runs process_message over each, then hands the whole batch to
audit.record_batch() and produces every outcome before a single flush().
This only changes the granularity the two invariants above apply at — audit
still precedes emit, and commit still comes last, now for the batch instead
of one message. Messages carrying msg.error() (broker-side notices: EOF,
rebalance) are filtered out before they reach process_message,
record_batch(), or highest_offsets() — an error entry still carries a real
offset, and letting it through would silently advance the commit position
past a message that was never processed.

Run:
    python -m pipelineguard.processor [--stats-every 5] [--log-level INFO]
    [--batch-size 500] [--batch-timeout-ms 1000] [--exit-after 0]
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
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition

from pipelineguard.audit import AuditRecord, AuditWriter
from pipelineguard.config import settings
from pipelineguard.detectors.schema_rules import (
    DECLARED_PII,
    FREE_TEXT,
    SchemaDetector,
)
from pipelineguard.detectors.tier1_rules import RulesDetector
from pipelineguard.models import Envelope, Finding, Tier
from pipelineguard.observability import StatsReporter, setup_logging

log = logging.getLogger("pipelineguard.processor")

_FAILURE_DETAIL_MAXLEN = 500

# Stateless and policy-only: no compiled patterns, no model, nothing to warm
# up. A module-level instance keeps process_message's signature unchanged and
# costs nothing per message.
_SCHEMA = SchemaDetector()

# The group coordinator moved this consumer's assignment under it mid-commit —
# routine during a rebalance, not a sign the coordinator is unreachable.
# Everything else on a commit (including a plain timeout) means it might be,
# and must still crash-and-replay rather than be swallowed here.
_TOLERATED_COMMIT_ERRORS = frozenset(
    {
        KafkaError.ILLEGAL_GENERATION,       # 22
        KafkaError.UNKNOWN_MEMBER_ID,        # 25
        KafkaError.REBALANCE_IN_PROGRESS,    # 27
    }
)


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


def highest_offsets(messages) -> list[TopicPartition]:
    """The commit position for a batch that may span multiple partitions.

    consumer.commit(msg) only knows how to position one (topic, partition);
    a batch from consume() can contain messages from all of them, so each
    partition needs its own commit position: the highest offset seen in that
    partition, plus one (the position Kafka should resume from on restart).

    Takes the max explicitly rather than the last message seen per partition.
    Ordering within a partition holds today, but relying on that instead of
    computing the max is an unstated assumption — it would fail silently if
    anything upstream (batching, retries) ever violated it.
    """
    highest: dict[tuple[str, int], int] = {}
    for msg in messages:
        key = (msg.topic(), msg.partition())
        highest[key] = max(highest.get(key, msg.offset()), msg.offset())
    return [
        TopicPartition(topic, partition, offset + 1)
        for (topic, partition), offset in highest.items()
    ]


def is_rebalance_error(exc: KafkaException) -> bool:
    """True if a commit failed for the routine reason that group membership
    moved under it (rebalance in progress, this member's id or generation
    already superseded) rather than because the coordinator is genuinely
    unreachable. Pulled out as its own predicate — rather than inlined as a
    branch in main() — so the classification itself gets exhaustive, cheap
    tests, and main() only needs a couple proving it calls this correctly.
    """
    return exc.args[0].code() in _TOLERATED_COMMIT_ERRORS


def merge_spans(findings: list[Finding]) -> list[tuple[int, int, str]]:
    """Overlapping findings unioned into disjoint spans, ascending.

    Two tiers scan a memo and can claim overlapping characters — Tier 2 flags a
    name inside a span Tier 1 already claims as EMAIL. Dropping the narrower one
    would leak: Tier 1 [10,30] with Tier 2 [5,15] leaves [5,10] in the clear.

    Collapses spans for the rewrite only; the findings themselves stay intact
    for the audit.
    """
    if not findings:
        return []
    merged: list[tuple[int, int, set[str]]] = []
    for f in sorted(findings, key=lambda x: (x.span_start, x.span_end)):
        # Strict overlap only, so touching spans stay as "[EMAIL][PHONE]".
        if merged and f.span_start < merged[-1][1]:
            start, end, types = merged[-1]
            merged[-1] = (start, max(end, f.span_end), types | {f.entity_type})
        else:
            merged.append((f.span_start, f.span_end, {f.entity_type}))
    return [(s, e, "+".join(sorted(t))) for s, e, t in merged]


def redact(text: str, findings: list[Finding]) -> str:
    """Replace each finding's span with [ENTITY_TYPE].

    Right-to-left: each replacement changes the string length, so applying them
    left-to-right would invalidate every span after the first. merge_spans()
    returns spans ascending, so iterating in reverse preserves that property
    while guaranteeing no span is rewritten twice.
    """
    for start, end, label in reversed(merge_spans(findings)):
        text = text[:start] + f"[{label}]" + text[end:]
    return text


def free_text_fields(raw: bytes | None) -> dict[str, str]:
    """Free-text values in one raw message, for the batched Tier 2 pre-pass.

    Best-effort, never authoritative: anything unreadable yields {} and is absent
    from the model batch. process_message still quarantines that message through
    the existing fail-closed path, so this needs no error policy of its own.

    Empty values are skipped — a record with no free text costs Tier 2 nothing.
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))["payload"]
        return {
            field: value
            for field, value in payload.items()
            if field in FREE_TEXT and isinstance(value, str) and value.strip()
        }
    except Exception:  # noqa: BLE001 — see docstring: unreadable means absent
        return {}


@dataclass
class Outcome:
    envelope: Envelope
    findings: list[Finding]
    action: str                          # 'clean' | 'redacted' | 'quarantined'
    out_bytes: bytes
    failure: tuple[str, str] | None      # (exception class, detail) if fail-closed


def process_message(
    msg,
    detector: RulesDetector,
    tier2_findings: dict[str, list[Finding]] | None = None,
) -> Outcome:
    """Parse, detect, redact and route one message. Never raises.

    `tier2_findings` maps field -> encoder findings already computed for this
    message by main()'s batched pre-pass. Injected rather than detected here
    because the encoder is only worth its cost in batches (8.03 ms/record at
    batch 8 against 29.4 at batch 1), and that keeps this function pure.

    Any unexpected error routes to quarantine rather than propagating: fail
    closed, never toward the clean topic. This is what makes crash-and-replay
    safe for the infrastructure layer above — a poisonous record can never
    trigger the replay path.
    """
    raw = msg.value()
    try:
        envelope = Envelope.from_bytes(raw)
        if not isinstance(envelope.message_id, str):
            raise TypeError(
                f"message_id is {type(envelope.message_id).__name__}, expected str"
            )
        # Canonicalize rather than just validate: uuid.UUID() accepts forms
        # (urn:uuid: prefix, braces, no hyphens) that Postgres's uuid input
        # does not. message_id is the join key across every topic and table,
        # so it must be uniform everywhere it's used downstream, not just at
        # this check.
        envelope.message_id = str(uuid.UUID(envelope.message_id))
        if not isinstance(envelope.event_ts, str):
            raise TypeError(
                f"event_ts is {type(envelope.event_ts).__name__}, expected str"
            )
        # Normalize the trailing "Z" some producers (JS, Go) emit by default:
        # datetime.fromisoformat() only learned "Z" in Python 3.11, and this
        # project's floor is 3.10, so on the floor a valid Z-suffixed
        # timestamp would otherwise quarantine as a parsing failure.
        if envelope.event_ts.endswith("Z"):
            envelope.event_ts = envelope.event_ts[:-1] + "+00:00"
        datetime.fromisoformat(envelope.event_ts)
        # bool is a subclass of int, so isinstance(True, int) alone would pass
        # a boolean through; psycopg then adapts it to a Postgres boolean,
        # which SMALLINT won't implicitly cast — an insert-time failure below
        # the fail-closed boundary.
        if not isinstance(envelope.schema_version, int) or isinstance(envelope.schema_version, bool):
            raise TypeError(
                f"schema_version is {type(envelope.schema_version).__name__}, expected int"
            )
        if not isinstance(envelope.payload, dict):
            raise TypeError(
                f"payload is {type(envelope.payload).__name__}, expected object"
            )

        # Dispatch by field. A declared field is redacted on the schema alone —
        # exclusive of rules, since its span already covers the whole value.
        findings_by_field: dict[str, list[Finding]] = {}
        for field, value in envelope.payload.items():
            if not isinstance(value, str):
                continue
            if field in DECLARED_PII:
                findings_by_field[field] = _SCHEMA.detect(value, field)
            elif field in FREE_TEXT:
                # Both tiers: rules for the embedded identifiers, encoder for
                # the names. merge_spans() resolves any overlap at redaction.
                findings_by_field[field] = detector.detect(value, field) + (
                    tier2_findings or {}
                ).get(field, [])
            else:
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

        # Tier 1 only: a sub-1.0 rule finding means a checksum failed, which is a
        # discrete defect worth a human. Tier 2 scores are continuous, so
        # including them would quarantine every record containing a name.
        if any(f.tier == Tier.RULES and f.confidence < 1.0 for f in all_findings):
            # Forward the ORIGINAL bytes so the reviewer sees what arrived.
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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stats-every", type=float, default=5.0, help="stats line interval (s)")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--flush-timeout", type=float, default=10.0, help="flush timeout per batch (s)")
    ap.add_argument("--batch-size", type=int, default=500, help="max messages fetched per consume() call")
    ap.add_argument(
        "--batch-timeout-ms", type=float, default=1000.0,
        help="max time to wait for a batch to fill before processing what arrived (ms)",
    )
    ap.add_argument(
        "--exit-after", type=int, default=0,
        help="stop after processing at least N messages, for benchmarking (0 = run forever)",
    )
    args = ap.parse_args(argv)

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

    processed_total = 0

    try:
        while True:
            messages = consumer.consume(args.batch_size, timeout=args.batch_timeout_ms / 1000.0)
            stats.maybe_report()
            if not messages:
                # Linger expired with no traffic — the normal idle case, not
                # an error. Nothing to process, nothing to commit.
                continue

            good_messages = []
            for msg in messages:
                if msg.error():
                    # Broker-side notice (rebalance, EOF, etc), not a record.
                    # It still carries a topic/partition/offset, so it must
                    # never reach process_message, record_batch, or
                    # highest_offsets — including it would advance the commit
                    # position past a message that was never processed.
                    log.error("consumer error: %s", msg.error())
                    continue
                good_messages.append(msg)

            if not good_messages:
                continue

            processed_total += len(good_messages)

            processed = []   # (msg, outcome, latency_ms)
            for msg in good_messages:
                t0 = time.perf_counter()
                outcome = process_message(msg, detector)
                latency_ms = (time.perf_counter() - t0) * 1000
                processed.append((msg, outcome, latency_ms))

            # ---- infrastructure layer: exceptions below here kill the process ----
            audit.record_batch(
                [
                    AuditRecord(
                        outcome.envelope, msg.topic(), outcome.findings,
                        outcome.action, latency_ms, outcome.failure,
                    )
                    for msg, outcome, latency_ms in processed
                ]
            )

            trackers = []   # (msg, target, outcome, latency_ms, tracker)
            for msg, outcome, latency_ms in processed:
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
                trackers.append((msg, target, outcome, latency_ms, tracker))

            # Both checks are needed: `remaining` catches "still queued when the
            # timeout expired"; tracker.error catches "left the queue by failing".
            # Neither implies the other, and either means a message is not durable.
            # The check spans every tracker in the batch — one failed delivery
            # among N must block the commit for all N, not just the last one.
            remaining = producer.flush(args.flush_timeout)
            failed = [(msg, target, tracker) for msg, target, _, _, tracker in trackers if tracker.error]
            if remaining or failed:
                raise DeliveryFailed(
                    f"batch of {len(trackers)}: unflushed={remaining} "
                    f"failed={[(m.topic(), m.partition(), m.offset(), t.error) for m, _, t in failed]}"
                )

            try:
                consumer.commit(offsets=highest_offsets(good_messages))
            except KafkaException as exc:
                if not is_rebalance_error(exc):
                    raise
                # The group moved on without us between processing this batch
                # and committing it. The work itself is not lost or wrong: it
                # was already audited and produced above, so the new owner of
                # this partition will simply reprocess the same messages, and
                # the audit upsert (keyed on message_id) makes that a no-op
                # rather than a duplicate. Only the bookmark failed to move.
                log.warning(
                    "commit skipped, rebalance in progress (%s): batch of %d not committed, will be reprocessed",
                    exc.args[0].name(), len(good_messages),
                )

            for msg, target, outcome, latency_ms, _ in trackers:
                stats.record(outcome.action, latency_ms, failed=outcome.failure is not None)
                log.debug(
                    "%s %s/%s@%s -> %s (%d findings, %.2fms)",
                    outcome.action, msg.topic(), msg.partition(), msg.offset(),
                    target, len(outcome.findings), latency_ms,
                )

            # After the commit and the stats loop, deliberately — breaking
            # earlier would leave this batch's offsets uncommitted despite it
            # having already been audited and produced. Harmless given
            # idempotency, but it would desync the benchmark's message count
            # from what the audit table holds.
            if args.exit_after and processed_total >= args.exit_after:
                break

    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        stats.report(final=True)
        producer.flush(5)
        consumer.close()   # clean group leave => no rebalance delay on restart
        audit.close()


if __name__ == "__main__":
    main()
