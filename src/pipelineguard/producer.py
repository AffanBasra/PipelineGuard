"""Produce synthetic transactions to txn.raw.

Usage:  python -m pipelineguard.producer --rate 50 --count 1000
"""
from __future__ import annotations

import argparse
import sys
import time

from confluent_kafka import Producer

from pipelineguard.config import settings
from pipelineguard.generator.transactions import make_transaction
from pipelineguard.models import Envelope

_delivered = 0
_failed = 0


def _on_delivery(err, msg):
    global _delivered, _failed
    if err is not None:
        _failed += 1
        print(f"DELIVERY FAILED: {err}", file=sys.stderr)
    else:
        _delivered += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=50, help="target msgs/sec")
    ap.add_argument("--count", type=int, default=1000, help="total messages")
    args = ap.parse_args()

    producer = Producer(
        {
            "bootstrap.servers": settings.kafka_bootstrap,
            "enable.idempotence": True,   # no producer-side duplicates on retry
            "linger.ms": 5,
        }
    )

    interval = 1.0 / args.rate if args.rate > 0 else 0
    start = time.perf_counter()
    for i in range(args.count):
        env = Envelope(payload=make_transaction())
        # Key by message_id: even distribution now, stable partitioning later.
        producer.produce(
            settings.topic_txn_raw,
            key=env.message_id.encode(),
            value=env.to_bytes(),
            on_delivery=_on_delivery,
        )
        producer.poll(0)  # serve delivery callbacks
        if interval:
            time.sleep(interval)

    producer.flush(30)
    elapsed = time.perf_counter() - start
    print(
        f"produced={args.count} delivered={_delivered} failed={_failed} "
        f"elapsed={elapsed:.1f}s effective_rate={_delivered / elapsed:.1f}/s"
    )


if __name__ == "__main__":
    main()
