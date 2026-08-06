"""Tier 2: zero-shot encoder NER over free text.

Finds names and addresses, which have no format for a rule to match. Batched,
because the cost is only tolerable in batches — 8.03 ms/record at batch 8
against 29.4 at batch 1 (tier2-detection-findings.md §11).

The model and torch are imported lazily inside load(), so importing this module
costs nothing and the processor still starts when Tier 2 is disabled.
"""
from __future__ import annotations

import logging

from pipelineguard.models import Finding, Tier

log = logging.getLogger("pipelineguard.tier2")

# Entity type recorded in the audit -> the GLiNER labels that produce it.
# PERSON_NAME matches what the schema rule emits, so the two arms of the
# dispatch stay comparable in the audit.
#
# One forward pass PER GROUP, never one pass over all labels at once: combining
# them halves the cost but drops PERSON coverage 99.4% -> 90.9%, because the
# labels compete for the same spans.
#
# ADDRESS labels (address, street_address, location) are deliberately absent.
# No memo template produces an address and there is no address field, so on this
# stream the pass could only ever be wrong -- measured at 30% false-positive
# rate over 200 generated memos, mostly re-tagging names it had already found.
# See tier2-detection-findings.md §13. Restoring it is one line, plus a corpus
# that actually contains addresses.
LABEL_GROUPS = {
    "PERSON_NAME": ["person", "first_name", "last_name"],
}

_WARMUP_TEXT = "Transfer to Ayesha Malik, House 12, Street 4, F-8/3 Islamabad"


def resolve_device(requested: str) -> str:
    """cpu | cuda | auto -> the device actually used, falling back to cpu."""
    import torch

    if requested == "cpu":
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if requested == "cuda":
        log.warning("device 'cuda' requested but torch reports no CUDA; using cpu")
    return "cpu"


class Tier2Detector:
    """Encoder NER with a batched API. Implements `detect` for the Detector
    protocol, but the processor should use `detect_batch` — per-call inference
    forfeits most of the speedup this tier depends on."""

    name = "tier2_encoder"

    def __init__(self, model_id: str, threshold: float = 0.25,
                 device: str = "auto", batch_size: int = 8) -> None:
        self.model_id = model_id
        self.threshold = threshold
        self.requested_device = device
        self.batch_size = max(1, batch_size)
        self.device = "cpu"
        self._model = None

    def load(self) -> None:
        """Fetch weights, move to the device and warm up. Called once at
        startup: a cold first batch pays for CUDA context creation and kernel
        autotuning, which would otherwise land on live traffic."""
        from gliner import GLiNER

        self.device = resolve_device(self.requested_device)
        log.info("loading %s on %s", self.model_id, self.device)
        self._model = GLiNER.from_pretrained(self.model_id, map_location=self.device)
        self._model.eval()
        self._predict([_WARMUP_TEXT] * self.batch_size)
        log.info("tier 2 ready (threshold %.2f, batch %d)",
                 self.threshold, self.batch_size)

    def _predict(self, texts: list[str]) -> list[list[tuple[dict, str]]]:
        """One batched pass per label group, zipped back per text."""
        per_text: list[list[tuple[dict, str]]] = [[] for _ in texts]
        for entity_type, labels in LABEL_GROUPS.items():
            batch = self._model.batch_predict_entities(
                texts, labels, threshold=self.threshold
            )
            for i, entities in enumerate(batch):
                per_text[i].extend((e, entity_type) for e in entities)
        return per_text

    def _to_findings(self, entities, field: str) -> list[Finding]:
        return [
            Finding(
                entity_type=entity_type,
                field=field,
                span_start=e["start"],
                span_end=e["end"],
                tier=Tier.ENCODER,
                confidence=float(e["score"]),
            )
            for e, entity_type in entities
        ]

    def detect_batch(
        self, inputs: dict[int, dict[str, str]]
    ) -> dict[int, dict[str, list[Finding]]]:
        """{msg index: {field: text}} -> {msg index: {field: [Finding]}}.

        Chunked to batch_size: throughput saturates at 8, so larger chunks only
        risk VRAM on a small card. Keys with no findings are omitted.
        """
        if self._model is None:
            raise RuntimeError("Tier2Detector.load() must be called before use")

        items = [(key, field, text)
                 for key, fields in inputs.items()
                 for field, text in fields.items()]
        if not items:
            return {}

        results: dict[int, dict[str, list[Finding]]] = {}
        for start in range(0, len(items), self.batch_size):
            chunk = items[start:start + self.batch_size]
            batch = self._predict([text for _, _, text in chunk])
            for (key, field, _text), entities in zip(chunk, batch):
                findings = self._to_findings(entities, field)
                if findings:
                    results.setdefault(key, {})[field] = findings
        return results

    def detect(self, text: str, field: str) -> list[Finding]:
        """Single-text path, for the Detector protocol and for tests."""
        return self.detect_batch({0: {field: text}}).get(0, {}).get(field, [])