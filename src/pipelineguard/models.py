"""Core data models: the message envelope and detection findings.

Every message on every topic uses the same envelope so downstream consumers
(and the audit writer) never need topic-specific parsing:

    {
      "message_id": "<uuid4>",        # assigned by producer; idempotency key everywhere
      "event_ts": "<iso8601 utc>",
      "schema_version": 1,
      "payload": { ... domain fields ... }
    }
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any


class Tier(IntEnum):
    RULES = 1      # regex / checksum validators (µs)
    ENCODER = 2    # fine-tuned NER encoder (ms)
    LLM = 3        # LLM escalation for ambiguous spans (API-bound)


@dataclass(frozen=True)
class Finding:
    """One detected PII entity. Never stores the entity value itself."""
    entity_type: str      # e.g. "CNIC", "IBAN_PK", "PHONE_PK", "EMAIL", "PERSON_NAME"
    field: str            # payload field the span was found in
    span_start: int
    span_end: int
    tier: Tier
    confidence: float     # 1.0 for validated rule hits


@dataclass
class Envelope:
    payload: dict[str, Any]
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema_version: int = 1

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Envelope":
        d = json.loads(raw.decode("utf-8"))
        return cls(
            payload=d["payload"],
            message_id=d["message_id"],
            event_ts=d["event_ts"],
            schema_version=d.get("schema_version", 1),
        )
