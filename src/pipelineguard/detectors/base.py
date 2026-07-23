"""Detector interface. All three tiers implement this protocol; the processor
composes them and decides escalation. Keeping the interface text-in/findings-out
means tiers are swappable and individually benchmarkable."""
from __future__ import annotations

from typing import Protocol

from pipelineguard.models import Finding


class Detector(Protocol):
    name: str

    def detect(self, text: str, field: str) -> list[Finding]:
        """Return findings for one payload field's text. Must be side-effect free."""
        ...
