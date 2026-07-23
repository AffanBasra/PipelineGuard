"""Stream processor: txn.raw -> detect/redact -> txn.clean | txn.quarantine + audit.

<<< AFFAN'S IMPLEMENTATION — learning task >>>

This is the heart of the project and the core Kafka learning. The scaffold
gives you every collaborator (config, Envelope, RulesDetector, AuditWriter);
you own the loop and its delivery semantics.

Target semantics: AT-LEAST-ONCE.
  The invariant: NEVER commit an offset before the message's outputs
  (produce to clean/quarantine + audit row) are durable. Crash at any point
  => the message is redelivered and reprocessed; AuditWriter upserts, so
  reprocessing is harmless. That pairing IS at-least-once + idempotency.

Consumer config that matters (understand each, don't cargo-cult):
  "enable.auto.commit": False   -> you commit manually, after processing
  "auto.offset.reset": "earliest"
  "group.id": settings.consumer_group

The loop, in order:
  1. msg = consumer.poll(1.0); handle None and msg.error().
  2. t0 = time.perf_counter()  (latency measurement starts before parsing).
  3. Envelope.from_bytes(msg.value()). Malformed JSON -> produce raw bytes to
     quarantine with a reason header, audit as 'quarantined', commit, continue.
     (A poison message must never wedge the pipeline — classic streaming trap.)
  4. Run RulesDetector over every STRING field in payload (iterate
     payload.items(), skip non-str values). Collect findings per field.
  5. Redact: replace each span with "[{entity_type}]", processing spans
     RIGHT-TO-LEFT within a field so earlier spans' indices stay valid.
     (Left-to-right corrupts every subsequent span — think about why.)
  6. Route:  no findings -> clean topic, action='clean'
             all findings confidence >= 1.0 -> redacted payload to clean,
                action='redacted'
             any finding confidence < 1.0 -> ORIGINAL payload to quarantine,
                action='quarantined'   (uncertain = human review, per design)
     Keep the same message_id in the outgoing envelope — it's the join key
     across topics and audit tables.
  7. audit.record(envelope, source_topic, findings, action, latency_ms).
  8. producer.flush() (or poll delivery callbacks) so the produce is confirmed
     durable, THEN consumer.commit(msg).   <- the ordering that makes step-0's
     invariant true. flush() per message is slow; that's fine for v1 — making
     it fast (batched commits) is a later, benchmarkable improvement.
  9. On KeyboardInterrupt: consumer.close() in a finally block (clean group
     leave => no rebalance delay on restart).

Run:  python -m pipelineguard.processor
Verify end-to-end:
  python scripts/create_topics.py
  python -m pipelineguard.producer --rate 50 --count 500
  python -m pipelineguard.processor
  -> then check Postgres:  SELECT action, count(*) FROM messages_processed GROUP BY action;
"""
from __future__ import annotations

import time

from confluent_kafka import Consumer, Producer

from pipelineguard.audit import AuditWriter
from pipelineguard.config import settings
from pipelineguard.detectors.tier1_rules import RulesDetector
from pipelineguard.models import Envelope, Finding


def main() -> None:
    # TODO(affan): build Consumer, Producer, RulesDetector, AuditWriter,
    # subscribe to settings.topic_txn_raw, and implement the loop above.
    raise NotImplementedError


if __name__ == "__main__":
    main()
