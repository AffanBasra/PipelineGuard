"""Console logging and rolling throughput/latency stats.

Why a periodic stats line rather than a progress bar: the processor consumes an
unbounded stream, so there is no total to progress against. A stats tick is both
more informative and produces exactly the numbers the README benchmark table
needs (records/sec, p50/p95/p99) as a by-product of normal operation.

Throughput rule: never log per message at INFO. At the rates this pipeline
reaches, stdout becomes the bottleneck and the benchmark ends up measuring the
terminal rather than the pipeline. Per-message detail belongs at DEBUG.
"""
from __future__ import annotations

import logging
import time
from collections import Counter, deque

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
    )


def percentile(values: list[float], p: float) -> float:
    """Index-based percentile, no interpolation: idx = round(p/100 * (n-1)).

    Dependency-free on purpose: statistics.quantiles needs >= 2 points and
    interpolates between them, which invents latency values that were never
    measured — not what you want in a number you intend to publish.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[idx]


class StatsReporter:
    """Counts actions and keeps a rolling latency window; emits a line on a timer.

    The latency window is bounded (`window`) so memory stays flat on a stream
    that never ends — percentiles describe recent behaviour, not all of history.
    """

    def __init__(
        self,
        logger: logging.Logger,
        interval_s: float = 5.0,
        window: int = 10_000,
    ) -> None:
        self._log = logger
        self._interval = interval_s
        self._latencies: deque[float] = deque(maxlen=window)
        self._actions: Counter[str] = Counter()
        self._failures = 0
        self._total = 0
        self._started = time.perf_counter()
        self._last_report = self._started
        self._last_total = 0

    def record(self, action: str, latency_ms: float, failed: bool = False) -> None:
        self._actions[action] += 1
        self._latencies.append(latency_ms)
        self._failures += failed
        self._total += 1

    def maybe_report(self) -> None:
        if time.perf_counter() - self._last_report >= self._interval:
            self.report()

    def report(self, final: bool = False) -> None:
        now = time.perf_counter()
        if final:
            elapsed = max(now - self._started, 1e-9)
            delta = self._total
        else:
            elapsed = max(now - self._last_report, 1e-9)
            delta = self._total - self._last_total

        if self._total == 0:
            if final:
                self._log.info("no messages processed")
            self._last_report = now
            return

        lat = list(self._latencies)
        self._log.info(
            "%sprocessed=%s clean=%s redacted=%s quarantined=%s failed=%s "
            "| rate=%.0f/s p50=%.2fms p95=%.2fms p99=%.2fms",
            "FINAL " if final else "",
            f"{self._total:,}",
            f"{self._actions['clean']:,}",
            f"{self._actions['redacted']:,}",
            f"{self._actions['quarantined']:,}",
            f"{self._failures:,}",
            delta / elapsed,
            percentile(lat, 50),
            percentile(lat, 95),
            percentile(lat, 99),
        )
        self._last_report = now
        self._last_total = self._total
