"""Create PipelineGuard topics explicitly (auto-create is disabled in compose).

Usage:  python scripts/create_topics.py
"""
from confluent_kafka.admin import AdminClient, NewTopic

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipelineguard.config import settings  # noqa: E402

TOPICS = [
    settings.topic_txn_raw,
    settings.topic_txn_clean,
    settings.topic_txn_quarantine,
]


def main() -> None:
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap})
    # 3 partitions on raw: enough to demonstrate consumer-group scaling later.
    futures = admin.create_topics(
        [NewTopic(t, num_partitions=3, replication_factor=1) for t in TOPICS]
    )
    for topic, fut in futures.items():
        try:
            fut.result()
            print(f"created: {topic}")
        except Exception as e:  # TopicExistsError etc.
            print(f"{topic}: {e}")


if __name__ == "__main__":
    main()
