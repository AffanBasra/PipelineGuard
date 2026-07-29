"""Shared fixtures and test doubles.

Everything here exists to keep the test suite fast and offline. No test in this
suite talks to Kafka or Postgres: the pipeline's decision logic was deliberately
written as pure functions (`RulesDetector.detect`, `processor.process_message`,
`processor.redact`) precisely so it can be tested without infrastructure.

Test doubles used, named precisely:
  * StubMessage  — a stand-in that returns canned values (a *stub*: it answers
                   queries, it has no behaviour of its own).
  * RecordingConnection — a *fake*: a working in-memory implementation of the
                   psycopg interface we use, which also records what it was
                   asked to do so tests can assert on the SQL parameters.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from confluent_kafka import KafkaException

from pipelineguard.detectors.tier1_rules import RulesDetector
from pipelineguard.models import Envelope


# --------------------------------------------------------------------------- #
# Kafka message stub
# --------------------------------------------------------------------------- #
class StubMessage:
    """Minimal stand-in for confluent_kafka.Message.

    Only the four accessors the processor actually calls are implemented. If
    the processor starts using another one, these tests fail loudly — which is
    the desired behaviour, not an inconvenience.
    """

    def __init__(
        self,
        value: bytes,
        topic: str = "txn.raw",
        partition: int = 0,
        offset: int = 0,
    ) -> None:
        self._value = value
        self._topic = topic
        self._partition = partition
        self._offset = offset

    def value(self) -> bytes:
        return self._value

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def error(self):
        return None


def make_message(payload: dict[str, Any], **kwargs: Any) -> StubMessage:
    """A well-formed message carrying `payload`."""
    return StubMessage(Envelope(payload=payload).to_bytes(), **kwargs)


def raw_message(body: dict[str, Any], **kwargs: Any) -> StubMessage:
    """A message built from a raw dict, so tests can produce malformed envelopes."""
    return StubMessage(json.dumps(body).encode(), **kwargs)


class ErrorMessage:
    """A confluent_kafka.Message carrying a broker-side notice (partition EOF,
    rebalance) rather than a record — error() returns a message instead of
    None, and value() returns None, matching the real Message API (an error
    event carries no payload but is still a valid Message otherwise). It has
    a real topic/partition/offset — the point of the fake — so that skipping
    the msg.error() filter doesn't crash on a missing attribute; it quietly
    quarantines the notice and advances the commit offset past it, which is
    the actual silent-data-loss failure mode the filter exists to prevent."""

    def __init__(
        self,
        topic: str = "txn.raw",
        partition: int = 0,
        offset: int = 0,
        error: str = "fake broker error",
    ) -> None:
        self._topic, self._partition, self._offset, self._error = topic, partition, offset, error

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def value(self):
        return None

    def error(self):
        return self._error


# --------------------------------------------------------------------------- #
# processor.main() loop fakes
#
# main() builds its own Consumer/Producer/AuditWriter, so testing the loop
# means patching those three names on the processor module rather than
# injecting anything — see patch_main_deps(). All three fakes append to one
# shared `log` list so tests can assert call ORDER (audit before emit,
# commit last) across a batch, not just that each call happened.
# --------------------------------------------------------------------------- #
class FakeConsumer:
    """Feeds processor.main()'s loop a scripted sequence of consume() batches.
    Once the script is exhausted it raises KeyboardInterrupt so main() exits
    through its normal shutdown path — the same role Ctrl-C plays for real.

    commit_errors scripts commit() the same way batches scripts consume():
    one entry popped per call, None for a clean commit, a KafkaError to raise
    KafkaException(error) instead. The error is raised BEFORE anything is
    appended to self.commits — a test asserting a rejected commit's offsets
    are absent from self.commits would be meaningless otherwise, since it
    couldn't distinguish "rejected" from "never recorded for some other
    reason"."""

    def __init__(
        self,
        batches: list[list],
        log: list[str] | None = None,
        commit_errors: list | None = None,
    ) -> None:
        self._batches = list(batches)
        self._commit_errors = list(commit_errors) if commit_errors is not None else []
        self._log = log if log is not None else []
        self.subscribed: list[str] = []
        self.commits: list[list] = []
        self.consume_calls = 0
        self.closed = False

    def subscribe(self, topics) -> None:
        self.subscribed = list(topics)

    def consume(self, num_messages, timeout):
        self.consume_calls += 1
        if not self._batches:
            raise KeyboardInterrupt
        return self._batches.pop(0)

    def commit(self, offsets=None, message=None, asynchronous=True) -> None:
        self._log.append("commit")
        error = self._commit_errors.pop(0) if self._commit_errors else None
        if error is not None:
            raise KafkaException(error)
        self.commits.append(list(offsets) if offsets is not None else [message])

    def close(self) -> None:
        self.closed = True


class FakeProducer:
    """Records every produce() call in order. A test can mark specific calls
    (by 0-based index in produce order) as failed deliveries, and control how
    many messages flush() reports still queued — the two independent ways a
    batch can fail to become durable."""

    def __init__(
        self,
        failing_indices: frozenset[int] = frozenset(),
        flush_remaining: int = 0,
        log: list[str] | None = None,
    ) -> None:
        self.produced: list[dict] = []
        self._failing_indices = failing_indices
        self.flush_remaining = flush_remaining
        self.flush_calls = 0
        self._log = log if log is not None else []

    def produce(self, topic, key, value, headers=None, on_delivery=None) -> None:
        idx = len(self.produced)
        self._log.append("produce")
        self.produced.append({"topic": topic, "key": key, "value": value, "headers": headers})
        if on_delivery is not None:
            error = "fake delivery error" if idx in self._failing_indices else None
            on_delivery(error, None)

    def flush(self, timeout) -> int:
        self.flush_calls += 1
        return self.flush_remaining

    def close(self) -> None:
        pass


class FakeAuditWriter:
    """Records every record_batch() call's argument list; close() is a no-op."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.batches: list[list] = []
        self.closed = False
        self._log = log if log is not None else []

    def record_batch(self, records) -> None:
        self._log.append("audit")
        self.batches.append(list(records))

    def close(self) -> None:
        self.closed = True


def patch_main_deps(
    monkeypatch: pytest.MonkeyPatch,
    batches: list[list],
    failing_indices: frozenset[int] = frozenset(),
    flush_remaining: int = 0,
    commit_errors: list | None = None,
):
    """Wires processor.main() to the three fakes above instead of real
    Kafka/Postgres. Returns (consumer, producer, audit, log) so a test can
    assert on what happened and in what order. commit_errors defaults to no
    errors, so every pre-existing loop test is unaffected by its addition."""
    from pipelineguard import processor as P

    log: list[str] = []
    consumer = FakeConsumer(batches, log=log, commit_errors=commit_errors)
    producer = FakeProducer(failing_indices=failing_indices, flush_remaining=flush_remaining, log=log)
    audit = FakeAuditWriter(log=log)
    monkeypatch.setattr(P, "Consumer", lambda *a, **k: consumer)
    monkeypatch.setattr(P, "Producer", lambda *a, **k: producer)
    monkeypatch.setattr(P, "AuditWriter", lambda *a, **k: audit)
    return consumer, producer, audit, log


# --------------------------------------------------------------------------- #
# Postgres fake
# --------------------------------------------------------------------------- #
class RecordingCursor:
    """execute() logs one statement; executemany() logs one entry per row so
    call-count-based assertions (e.g. "3 deletes for 3 messages") read the
    same regardless of which one the code under test used — the `calls` log
    on the connection is what distinguishes a real batched round trip from a
    loop of individual calls."""

    def __init__(self, log: list[tuple[str, tuple]], calls: list[str]) -> None:
        self._log = log
        self._calls = calls

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._calls.append("execute")
        self._log.append((sql, params or ()))

    def executemany(self, sql: str, params_seq) -> None:
        self._calls.append("executemany")
        for params in params_seq:
            self._log.append((sql, params))


class RecordingConnection:
    """In-memory stand-in for a psycopg connection that records every statement."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.calls: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.statements, self.calls)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    @contextmanager
    def transaction(self):
        """Mirrors psycopg.Connection.transaction(): commits on a clean exit,
        rolls back and re-raises on exception."""
        try:
            yield
        except Exception:
            self.rollback()
            raise
        else:
            self.commit()

    def close(self) -> None:
        self.closed = True

    # -- helpers for assertions ------------------------------------------- #
    def statements_starting(self, verb: str) -> list[tuple[str, tuple]]:
        needle = verb.strip().upper()
        return [(s, p) for s, p in self.statements if s.strip().upper().startswith(needle)]

    def all_params_flat(self) -> list[object]:
        return [p for _, params in self.statements for p in params]


@pytest.fixture
def recording_conn(monkeypatch: pytest.MonkeyPatch) -> RecordingConnection:
    """Patch psycopg.connect so AuditWriter builds against the fake."""
    import psycopg

    conn = RecordingConnection()
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    return conn


# --------------------------------------------------------------------------- #
# Domain fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def detector() -> RulesDetector:
    """Session-scoped: RulesDetector compiles patterns in __init__ and is
    stateless afterwards, so one instance is safely shared across tests."""
    return RulesDetector()


@pytest.fixture
def clean_payload() -> dict[str, Any]:
    return {"note": "Zakat contribution", "channel": "branch", "amount_pkr": 500.0}


@pytest.fixture
def pii_payload() -> dict[str, Any]:
    """Every entity type Tier 1 knows about, with a valid mod-97 IBAN."""
    return {
        "account_holder": "Ayesha Malik",
        "cnic": "35202-1234567-1",
        "iban_from": "PK68MEZN5748718428058488",   # mod-97 valid
        "phone": "+92 300 1234567",
        "email": "ayesha@example.com",
        "amount_pkr": 1500.0,
    }
