"""Print the most recent messages on a topic.

Reads from the END of the log, not the beginning. Topics retain earlier runs,
so `auto.offset.reset=earliest` shows whatever the pipeline looked like days
ago -- which is a good way to convince yourself a fix did not work.

Uses a throwaway group and never commits, so it cannot disturb the processor's
offsets.

Usage:
    python scripts/peek_topic.py txn.clean
    python scripts/peek_topic.py txn.quarantine -n 3
    python scripts/peek_topic.py txn.clean --with-memo      # skip blank memos
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from confluent_kafka import Consumer, TopicPartition

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipelineguard.config import settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("topic")
    ap.add_argument("-n", type=int, default=5, help="messages to show")
    ap.add_argument("--with-memo", action="store_true",
                    help="only show records whose memo is non-empty")
    ap.add_argument("--field", default=None,
                    help="print just this payload field instead of the envelope")
    args = ap.parse_args(argv)

    consumer = Consumer({
        "bootstrap.servers": settings.kafka_bootstrap,
        "group.id": f"peek-{uuid.uuid4()}",
        "enable.auto.commit": False,
    })

    # Scan back further than requested: --with-memo filters, and partitions are
    # unevenly filled, so the last N offsets may not yield N matching records.
    lookback = max(args.n * 20, 100)
    parts = []
    total = 0
    for p in range(3):
        tp = TopicPartition(args.topic, p)
        try:
            low, high = consumer.get_watermark_offsets(tp, timeout=10)
        except Exception as exc:
            print(f"{args.topic} partition {p}: {exc}")
            continue
        total += high - low
        parts.append(TopicPartition(args.topic, p, max(low, high - lookback)))

    if not parts:
        print(f"no partitions readable on {args.topic}")
        return 1

    print(f"{args.topic}: {total} messages retained\n")
    consumer.assign(parts)

    shown = 0
    while shown < args.n:
        msg = consumer.poll(5.0)
        if msg is None:
            break
        if msg.error():
            continue
        env = json.loads(msg.value())
        if args.with_memo and not env["payload"].get("memo"):
            continue
        shown += 1
        if args.field:
            print(f"  p{msg.partition()}@{msg.offset()}  "
                  f"{env['payload'].get(args.field)!r}")
        else:
            print(f"--- partition {msg.partition()} offset {msg.offset()}")
            print(json.dumps(env, indent=2, ensure_ascii=False))
    consumer.close()

    if not shown:
        print("  (nothing matched -- topic may be empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())