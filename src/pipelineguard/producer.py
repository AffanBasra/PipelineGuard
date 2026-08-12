"""Produce synthetic transactions to txn.raw.

Bounded work, so a real progress bar applies here — unlike the processor, which
consumes an unbounded stream and reports periodic stats instead.

Usage:  python -m pipelineguard.producer --rate 50 --count 1000
        python -m pipelineguard.producer --rate 0 --count 50000   # unthrottled
"""
from __future__ import annotations

import argparse
import logging
import random
import time

from confluent_kafka import Producer
from faker import Faker
from tqdm import tqdm

from pipelineguard.config import settings
from pipelineguard.generator.transactions import (
    ADDRESS_MEMO_RATE,
    BLANK_MEMO_RATE,
    make_transaction,
)
from pipelineguard.models import Envelope
from pipelineguard.observability import setup_logging

log = logging.getLogger("pipelineguard.producer")


class DeliveryCounter:
    """Aggregates delivery outcomes and advances the bar as messages are actually
    acknowledged, not merely enqueued — produce() is asynchronous, so counting at
    the call site would show progress the broker hasn't confirmed."""

    def __init__(self, bar: tqdm) -> None:
        self.bar = bar
        self.delivered = 0
        self.failed = 0

    def __call__(self, err, msg) -> None:
        if err is not None:
            self.failed += 1
            log.error("delivery failed: %s", err)
        else:
            self.delivered += 1
        self.bar.update(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthetic transaction producer")
    ap.add_argument("--rate", type=float, default=50, help="target msgs/sec (0 = unthrottled)")
    ap.add_argument("--count", type=int, default=1000, help="total messages")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument(
        "--blank-memo-rate", type=float, default=BLANK_MEMO_RATE,
        help=f"share of transactions with no narration (default {BLANK_MEMO_RATE}); "
             "sets how much of the stream reaches Tier 2",
    )
    ap.add_argument(
        "--address-memo-rate", type=float, default=ADDRESS_MEMO_RATE,
        help=f"share of NON-BLANK memos carrying an address (default "
             f"{ADDRESS_MEMO_RATE}); sets how much of the stream exercises "
             "Tier 2's hardest entity",
    )
    ap.add_argument(
        "--seed", type=int, default=None,
        help="RNG seed (default: random, and logged so any run can be replayed)",
    )
    args = ap.parse_args()

    setup_logging(args.log_level)

    # Random by default so no two runs are identical, but logged so a run whose
    # numbers look odd can be reproduced exactly. Throughput depends on how many
    # memos come out blank, so an unreproducible mix makes measurements
    # uninterpretable.
    seed = args.seed if args.seed is not None else random.randrange(2**32)
    random.seed(seed)
    Faker.seed(seed)
    log.info("generator seed=%d blank_memo_rate=%.2f address_memo_rate=%.2f",
             seed, args.blank_memo_rate, args.address_memo_rate)

    producer = Producer(
        {
            "bootstrap.servers": settings.kafka_bootstrap,
            "enable.idempotence": True,   # no producer-side duplicates on retry
            "linger.ms": 5,
        }
    )

    interval = 1.0 / args.rate if args.rate > 0 else 0
    start = time.perf_counter()

    with tqdm(total=args.count, unit="msg", desc=settings.topic_txn_raw) as bar:
        counter = DeliveryCounter(bar)
        blank = 0
        for _ in range(args.count):
            payload = make_transaction(args.blank_memo_rate,
                                       args.address_memo_rate)
            blank += not payload["memo"]
            env = Envelope(payload=payload)
            # Key by message_id: even distribution now, stable partitioning later.
            producer.produce(
                settings.topic_txn_raw,
                key=env.message_id.encode(),
                value=env.to_bytes(),
                on_delivery=counter,
            )
            producer.poll(0)   # serve delivery callbacks
            if interval:
                time.sleep(interval)
        producer.flush(30)

    elapsed = time.perf_counter() - start
    log.info(
        "produced=%s delivered=%s failed=%s elapsed=%.1fs effective_rate=%.0f/s",
        f"{args.count:,}", f"{counter.delivered:,}", f"{counter.failed:,}",
        elapsed, counter.delivered / max(elapsed, 1e-9),
    )
    # The REALISED rate, not the requested one. Throughput downstream depends on
    # how many memos actually came out blank, and with a random seed that is not
    # knowable from the arguments alone.
    log.info(
        "seed=%d blank_memos=%s (%.1f%% realised, %.0f%% requested)",
        seed, f"{blank:,}", 100 * blank / max(args.count, 1),
        100 * args.blank_memo_rate,
    )
    if counter.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
