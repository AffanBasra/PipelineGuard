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
from typing import Any

import pytest

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


def make_message(payload: dict[str, Any], **kwargs: Any) -> StubMessage:
    """A well-formed message carrying `payload`."""
    return StubMessage(Envelope(payload=payload).to_bytes(), **kwargs)


def raw_message(body: dict[str, Any], **kwargs: Any) -> StubMessage:
    """A message built from a raw dict, so tests can produce malformed envelopes."""
    return StubMessage(json.dumps(body).encode(), **kwargs)


# --------------------------------------------------------------------------- #
# Postgres fake
# --------------------------------------------------------------------------- #
class RecordingCursor:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self._log = log

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._log.append((sql, params or ()))


class RecordingConnection:
    """In-memory stand-in for a psycopg connection that records every statement."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.commits = 0
        self.closed = False

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.statements)

    def commit(self) -> None:
        self.commits += 1

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
