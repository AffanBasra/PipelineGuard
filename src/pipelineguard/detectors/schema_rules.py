"""Fields the schema already declares to be PII.

Some fields do not need detecting. `account_holder` holds a person's name in
every record by contract — that is what the column IS. Running a model over it
to discover what the schema already states would be strictly worse on every
axis: slower, and wrong some of the time.

That last part is the point. `docs/tier2-detection-findings.md` §2 measured the
best available encoder at **99.4%** character coverage on PERSON. Applied to a
field that is a name 100% of the time, a 99.4% detector is a leak rate, not an
accuracy — and it fails on exactly the names it was always going to fail on
(§2's rare and ambiguous bands), which is to say, systematically rather than
randomly. A declared field is redacted whole at confidence 1.0 or the contract
is broken; there is no third outcome for a model to be uncertain about.

This is the first arm of the dispatch that replaces the old "escalate to Tier 2"
framing (`docs/decisions.md`, "There is no Tier 1 -> Tier 2 escalation"):

    identifier fields (cnic, iban, phone, email)  -> Tier 1 rules
    declared PII (account_holder)                 -> here, no model
    free text (memo)                              -> Tier 2 encoder

Recorded as `Tier.RULES` rather than a tier of its own. `db/init.sql:33`
constrains `tier IN (1, 2, 3)` and `max_tier` already uses 0 to mean "no
findings", so a distinct schema tier would need a migration and would collide
with that sentinel. A schema rule is a rule -- keyed on field name instead of
on a pattern -- and `entity_type` plus `field` already separate it from a
regex hit in the audit trail. Revisit if the distinction ever has to be
queryable on its own.
"""
from __future__ import annotations

from pipelineguard.models import Finding, Tier

# field name -> entity type recorded for it. Whole-field redaction, always.
DECLARED_PII: dict[str, str] = {
    "account_holder": "PERSON_NAME",
}


class SchemaDetector:
    """Redacts declared-PII fields whole, on the strength of the schema alone.

    Implements the same `Detector` protocol as the other tiers (`base.py`), so
    the processor composes it identically and it stays independently testable.
    Stateless: no compiled patterns, no model, nothing to warm up.
    """

    name = "schema"

    def detect(self, text: str, field: str) -> list[Finding]:
        entity_type = DECLARED_PII.get(field)
        # An empty declared field has nothing to redact. Emitting a zero-width
        # finding would put a [PERSON_NAME] marker where no name ever was, and
        # would count toward the audit's per-record finding totals.
        if entity_type is None or not text:
            return []
        return [
            Finding(
                entity_type=entity_type,
                field=field,
                span_start=0,
                span_end=len(text),
                tier=Tier.RULES,
                confidence=1.0,
            )
        ]