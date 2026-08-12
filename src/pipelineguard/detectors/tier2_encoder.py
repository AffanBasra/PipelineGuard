"""Tier 2: zero-shot encoder NER over free text.

Finds names and addresses, which have no format for a rule to match. Batched,
because the cost is only tolerable in batches — 8.03 ms/record at batch 8
against 29.4 at batch 1 (tier2-detection-findings.md §11).

The model and torch are imported lazily inside load(), so importing this module
costs nothing and the processor still starts when Tier 2 is disabled.
"""
from __future__ import annotations

import logging
import re

from pipelineguard.models import Finding, Tier

log = logging.getLogger("pipelineguard.tier2")

# Entity type recorded in the audit -> the GLiNER labels that produce it.
# PERSON_NAME matches what the schema rule emits, so the two arms of the
# dispatch stay comparable in the audit.
#
# One forward pass PER GROUP. ADDRESS was absent between §13 and §18 because no
# memo produced an address; generator/addresses.py ended that, and §18 measured
# what restoring it costs on the shipped path:
#
#                       ADDRESS   PERSON   incremental over-redaction   cost
#   PERSON only           24.1%    99.2%                            -   8.1 ms
#   + ADDRESS group       98.5%   100.0%                         0.0%  16.2 ms
#   all labels, one pass  98.0%    98.4%                         0.0%   8.9 ms
#
# §13.2's "30% false-positive rate" is withdrawn as a reason to leave it out.
# The ADDRESS labels do fire on half of all address-free memos, but every
# character they claim was already claimed by PERSON or is intended PII, so
# merge_spans() unions them and the redacted output is byte-identical. A false
# positive that changes no output is not a cost.
#
# The third row is the cheap option and it is NOT what ships. §13 recorded
# combining as dropping PERSON to 90.9%; at this checkpoint it only drops to
# 98.4%, so the old number does not replicate -- but PERSON is the entity this
# pipeline exists for, and 1.6 points of it is not worth 7 ms.
LABEL_GROUPS = {
    "PERSON_NAME": ["person", "first_name", "last_name"],
    "ADDRESS": ["address", "street_address", "location"],
}

_WARMUP_TEXT = "Transfer to Ayesha Malik, House 12, Street 4, F-8/3 Islamabad"

# --------------------------------------------------------------------------- #
# Span extension for ADDRESS findings
#
# §17 measured where the encoder's missed characters sit. Of the identifying
# characters it drops from an address, 26.8% are LEADING -- the house or plot
# number, which is the single most identifying part of an address and the part
# whose loss turns '[ADDRESS], Karachi' into a redaction that looks complete and
# is not. A further 2.0% are the trailing city.
#
# Those two are recoverable without a model, because they are positional. The
# remaining 68% are interior and no extension rule can reach them; that residual
# is the honest case for fine-tuning and this does not pretend to address it.
# --------------------------------------------------------------------------- #

# A house or plot number immediately before a detected address, optionally
# introduced by a dwelling word. The digit requirement is what keeps this from
# eating prose: 'Cheque posted to' has no digit, 'R1525' does. Anchored to the
# end of the text preceding the span.
# The leading lookbehind is load-bearing. Without it the optional [A-Za-z]
# (which exists for 'R1525' and 'D 5') matched the LAST LETTER of the preceding
# word: 'Statement to 14, Islamabad' extended over 'o 14, '. The token has to
# begin at a word boundary, not merely somewhere before a digit.
#
# The trailing [,\s]* rather than ,?\s* is load-bearing on real data too: OSM
# addresses carry doubled separators ('V2HP+3PP, , Imam Khumani Library Rd'),
# and a single-comma anchor could not reach back over them.
_LEADING_NUMBER = re.compile(
    r"(?:^|(?<=[\s,(]))"
    r"(?:(?:House|Flat|Plot|Shop|Ghar|Makan)\s*(?:No\.?\s*)?)?"
    r"[A-Za-z]?[-\s]?\d[\w/+-]*[,\s]*$"
)

# Cities the corpus and the generator use. Locale knowledge, the same kind Tier 1
# already encodes in its CNIC province digits and PK bank codes. A trailing city
# is only ever extended over when the encoder already found the address in front
# of it, so this cannot fire on a bare city name in ordinary prose.
CITIES = frozenset({
    "Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad",
    "Multan", "Peshawar", "Quetta", "Hyderabad", "Sialkot", "Gujranwala",
})
#
# The city must be the WHOLE trailing component, not a prefix of one. A bare
# word boundary extended 'Model Town, Sialkot Road' over 'Sialkot', because a
# city name is also a road name in Pakistan more often than not.
#
# But requiring end-of-string or punctuation after the city was too strict, and
# a live Kafka run found what it costs (§24): Roman-Urdu memos continue past the
# city -- 'PECHS Block 2, Karachi par bhej dein' -- so the city was left in the
# clear. The test is not "is anything after it" but "is what follows a ROAD
# word", which is the only thing the original guard was protecting against.
_ROAD_WORD = (
    r"Road|Rd|Street|St|Highway|Motorway|Expressway|Bypass|Chowk|Colony|"
    r"Town|Bazaar|Market|Cantt|Cantonment"
)
_TRAILING_CITY = re.compile(
    rf"^,?\s*({'|'.join(sorted(CITIES))})"
    rf"(?=$|[,.;]|\s+(?!(?i:{_ROAD_WORD})\b))"
)

# One structural component: 'Block J', 'Sector 16/A', 'Phase 1', 'Blk 4-A'.
#
# §21 found the residual's worst well-formed cases all share one shape --
# 'C-21, Block J North Nazimabad Town, Karachi'. The encoder finds the town, and
# a single-step rule stops at 'Block J' because it carries no digit, so the plot
# number two components out is never reached. Stepping over the structural
# component and trying the number again recovers it.
_STRUCTURAL = re.compile(
    r"(?:^|(?<=[\s,(]))(?:Block|Blk|Sector|Sec|Phase|Ph)"
    r"\s*[-\s]?[A-Za-z0-9/-]{1,6}[,\s]*$",
    re.I,
)

# Bounded because the walk is now a loop. Three is what the corpus needs
# ('C-21, Block J, <town>' is two); an unbounded walk would cross the sentence
# into 'Deliver statement to' given enough comma-separated fragments.
_MAX_EXTENSIONS = 3

# Two ADDRESS spans this close together are one address with a hole in it.
#
# §23 measured the residual left by the extension rules and found it is mostly
# not a boundary at all: the encoder returns 'C-21' and 'Karachi' and drops the
# locality between them. No outward walk can reach an interior gap; joining the
# spans can. 32 is where the gain flattens -- see §23.2 for the sweep.
_MAX_BRIDGE = 32


def bridge_address_spans(
    spans: list[tuple[int, int, float]]
) -> list[tuple[int, int, float]]:
    """Join ADDRESS spans separated by at most _MAX_BRIDGE characters.

    Takes and returns (start, end, score); a joined span carries the highest
    score of its members, because the group is one address and the confidence
    that matters is the best evidence for it.
    """
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [list(ordered[0])]
    for start, end, score in ordered[1:]:
        if start - merged[-1][1] <= _MAX_BRIDGE:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = max(merged[-1][2], score)
        else:
            merged.append([start, end, score])
    return [(s, e, c) for s, e, c in merged]


def extend_address_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen one ADDRESS span over its house number and trailing city.

    Walks left at most _MAX_EXTENSIONS components, taking a house or plot number
    where it finds one and stepping over a structural component ('Block J') to
    look behind it. Bounded on purpose: an unbounded walk would cross into
    'Payment against order #4821, House 12, Model Town, Lahore' and swallow the
    invoice number, which is comma-adjacent in exactly the same shape.

    Measured at 0.0% over-redaction on 400 address-free memos on its own; 0.2%
    of characters once bridging is applied on top (§23.3, and all of it is the
    word 'contact' between a name and a phone number).
    """
    for _ in range(_MAX_EXTENSIONS):
        prefix = text[:start]
        match = _LEADING_NUMBER.search(prefix)
        # '#4821' is an invoice number, not a house number, and it is the one
        # shape that reaches here looking like both. rstrip() because '# 4821'
        # is written both ways and only the spaced form survives the lookbehind.
        if match and not prefix[:match.start()].rstrip().endswith("#"):
            start = match.start()
            continue
        match = _STRUCTURAL.search(prefix)
        if match:
            start = match.start()
            continue
        break

    match = _TRAILING_CITY.match(text[end:])
    if match:
        end += match.end()
    return start, end

# The checkpoint the default threshold was swept against (findings §6.1, §16.2).
# The revision is half of that identity, not decoration: a HuggingFace model repo
# is a git repo, and this one gained fp16/bf16 variants on 2026-04-28 without its
# name changing. A name-only check cannot see that, so anyone tracking `main`
# would silently get different weights under an unchanged threshold.
#
# The pin is NOT total, and saying so matters more than the pin does. GLiNER
# resolves its base tokenizer from microsoft/deberta-v3-base at `main`, a
# different repo this revision cannot reach. Pinning here fixes the weights;
# only a warm cache plus HF_HUB_OFFLINE=1 fixes everything (see docker-compose).
_TUNED_FOR = "gliner-community/gliner_medium-v2.5"
_TUNED_FOR_REVISION = "88c3b98b57ad5e7d66fb209ed61c53f4b1fd05da"


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
                 device: str = "auto", batch_size: int = 8,
                 label_groups: dict[str, list[str]] | None = None,
                 extend_addresses: bool = True,
                 revision: str | None = None) -> None:
        self.model_id = model_id
        self.revision = revision
        self.threshold = threshold
        self.requested_device = device
        self.batch_size = max(1, batch_size)
        self.label_groups = label_groups or LABEL_GROUPS
        # Off only for the before/after measurement in probe_address_residual.py.
        # A rule measured against itself proves nothing, so the comparison has to
        # run the same detector twice rather than a copy of the logic.
        self.extend_addresses = extend_addresses
        self.device = "cpu"
        self._model = None

    def load(self) -> None:
        """Fetch weights, move to the device and warm up. Called once at
        startup: a cold first batch pays for CUDA context creation and kernel
        autotuning, which would otherwise land on live traffic."""
        from gliner import GLiNER

        self.device = resolve_device(self.requested_device)
        revision = self.resolved_revision()
        log.info("loading %s@%s on %s", self.model_id, revision or "main", self.device)
        self._model = GLiNER.from_pretrained(
            self.model_id, revision=revision, map_location=self.device
        )
        self._model.eval()
        self._predict([_WARMUP_TEXT] * self.batch_size)
        # Model, revision and threshold are logged together on purpose. The
        # threshold is an uncalibrated sigmoid cutoff, not a probability, so it
        # does not transfer between checkpoints -- nvidia/gliner-PII at this
        # model's 0.25 fires on 68% of clean Pakistani text. Swapping either the
        # model or its revision means re-running scripts/probe_ner_sweep.py, not
        # reusing this number.
        log.info(
            "tier 2 ready: model=%s revision=%s threshold=%.2f labels=%s "
            "batch=%d device=%s",
            self.model_id, revision or "main (UNPINNED)", self.threshold,
            list(self.label_groups), self.batch_size, self.device,
        )
        if self.model_id != _TUNED_FOR:
            log.warning(
                "threshold %.2f was tuned for %s, not %s -- re-run "
                "scripts/probe_ner_sweep.py or results will not be comparable",
                self.threshold, _TUNED_FOR, self.model_id,
            )
        elif revision != _TUNED_FOR_REVISION:
            log.warning(
                "threshold %.2f was tuned for %s@%s, not @%s -- a model repo is "
                "a git repo and its weights can change under an unchanged name",
                self.threshold, _TUNED_FOR, _TUNED_FOR_REVISION,
                revision or "main (UNPINNED)",
            )

    def resolved_revision(self) -> str | None:
        """The revision actually passed to the hub, or None to track the branch.

        A commit hash belongs to ONE repo. Carrying the default pin over to a
        different model would ask the hub for a commit that does not exist there
        and fail the load outright -- so an explicitly chosen model drops the pin
        and says so, rather than crashing on a config the operator never set.
        """
        if self.revision and self.model_id != _TUNED_FOR:
            if self.revision == _TUNED_FOR_REVISION:
                log.warning(
                    "revision %s belongs to %s, not %s -- loading unpinned; set "
                    "TIER2_MODEL_REVISION to a commit in the model you chose",
                    self.revision, _TUNED_FOR, self.model_id,
                )
                return None
        # "main" is a branch, not a pin. Passed through as None so the log line
        # can say UNPINNED rather than implying a fixed checkpoint.
        return None if self.revision in ("", "main", None) else self.revision

    def _predict(self, texts: list[str]) -> list[list[tuple[dict, str]]]:
        """One batched pass per label group, zipped back per text."""
        per_text: list[list[tuple[dict, str]]] = [[] for _ in texts]
        for entity_type, labels in self.label_groups.items():
            batch = self._model.batch_predict_entities(
                texts, labels, threshold=self.threshold
            )
            for i, entities in enumerate(batch):
                per_text[i].extend((e, entity_type) for e in entities)
        return per_text

    def _to_findings(self, entities, field: str, text: str) -> list[Finding]:
        findings = []
        # Held back rather than emitted in place: bridging is a decision about
        # the whole set of ADDRESS spans in this field, not about one span.
        addresses: list[tuple[int, int, float]] = []
        for entity, entity_type in entities:
            start, end = entity["start"], entity["end"]
            score = float(entity["score"])
            if entity_type == "ADDRESS" and self.extend_addresses:
                addresses.append((*extend_address_span(text, start, end), score))
                continue
            findings.append(
                Finding(
                    entity_type=entity_type,
                    field=field,
                    span_start=start,
                    span_end=end,
                    tier=Tier.ENCODER,
                    confidence=score,
                )
            )
        for start, end, score in bridge_address_spans(addresses):
            findings.append(
                Finding(
                    entity_type="ADDRESS",
                    field=field,
                    span_start=start,
                    span_end=end,
                    tier=Tier.ENCODER,
                    confidence=score,
                )
            )
        return findings

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
            for (key, field, text), entities in zip(chunk, batch):
                findings = self._to_findings(entities, field, text)
                if findings:
                    results.setdefault(key, {})[field] = findings
        return results

    def detect(self, text: str, field: str) -> list[Finding]:
        """Single-text path, for the Detector protocol and for tests."""
        return self.detect_batch({0: {field: text}}).get(0, {}).get(field, [])