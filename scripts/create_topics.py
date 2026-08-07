"""Create PipelineGuard topics explicitly (auto-create is disabled in compose).

Also sets retention, which is a data-protection control here rather than a
capacity one. Two of these topics hold unredacted personal data:

    txn.raw          the input, necessarily unredacted
    txn.quarantine   ORIGINAL bytes, deliberately (processor.py forwards `raw`
                     so a reviewer sees exactly what arrived)

Left unset they inherit the broker default of 168h, which is how long PII would
sit on an unauthenticated broker. The audit trail's "never stores values"
property does not extend to the topics; see docs/handoff.md.

Re-running is safe and is how an existing cluster gets the retention: topic
creation no-ops once a topic exists, so the config is applied separately via
incremental_alter_configs.

Usage:  python scripts/create_topics.py
"""
from confluent_kafka.admin import (
    AdminClient,
    AlterConfigOpType,
    ConfigEntry,
    ConfigResource,
    NewTopic,
)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipelineguard.config import settings  # noqa: E402

HOUR_MS = 60 * 60 * 1000

# Retention per topic, chosen against exposure rather than disk.
RETENTION_MS = {
    # Unredacted input. Long enough to replay a day-long processor outage,
    # short enough to bound how long raw PII is retrievable.
    settings.topic_txn_raw: 24 * HOUR_MS,
    # Redacted output, so the lowest-risk topic. Downstream consumers get the
    # broker default week.
    settings.topic_txn_clean: 168 * HOUR_MS,
    # Unredacted, awaiting human review. This value IS the reviewer SLA: after
    # it expires the record is gone unreviewed. Shortening it reduces exposure
    # and increases the chance a quarantined record is never looked at.
    settings.topic_txn_quarantine: 72 * HOUR_MS,
}

TOPICS = list(RETENTION_MS)


def main() -> None:
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap})

    # 3 partitions on raw: enough to demonstrate consumer-group scaling later.
    futures = admin.create_topics(
        [
            NewTopic(
                t,
                num_partitions=3,
                replication_factor=1,
                config={"retention.ms": str(RETENTION_MS[t])},
            )
            for t in TOPICS
        ]
    )
    for topic, fut in futures.items():
        try:
            fut.result()
            print(f"created: {topic}  retention={RETENTION_MS[topic] // HOUR_MS}h")
        except Exception as e:  # TopicExistsError etc.
            print(f"{topic}: {e}")

    # Existing topics keep whatever they were created with, so apply retention
    # explicitly. Incremental, not alter_configs: the latter replaces the whole
    # config set and would silently reset anything else already tuned.
    alters = admin.incremental_alter_configs(
        [
            ConfigResource(
                ConfigResource.Type.TOPIC,
                topic,
                incremental_configs=[
                    ConfigEntry(
                        "retention.ms",
                        str(ms),
                        incremental_operation=AlterConfigOpType.SET,
                    )
                ],
            )
            for topic, ms in RETENTION_MS.items()
        ]
    )
    for resource, fut in alters.items():
        try:
            fut.result()
            print(f"retention set: {resource.name} -> "
                  f"{RETENTION_MS[resource.name] // HOUR_MS}h")
        except Exception as e:
            print(f"retention FAILED on {resource.name}: {e}")


if __name__ == "__main__":
    main()