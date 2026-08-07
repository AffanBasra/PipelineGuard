"""What the schema itself says about each field, and the dispatch it implies.

    identifier fields (cnic, iban, phone, email)  -> Tier 1 rules
    declared PII (account_holder)                 -> here, no model
    free text (memo)                              -> Tier 1 + Tier 2 encoder

`account_holder` holds a name in every record by contract, so detecting it would
be strictly worse than declaring it: the best encoder measured 99.4% character
coverage on PERSON, which against a field that is always a name is a leak rate
rather than an accuracy — and it misses the rare names systematically.

Findings are recorded as `Tier.RULES`; `db/init.sql` constrains tier to (1,2,3)
and reserves 0 for "no findings", so a distinct schema tier needs a migration.
"""
from __future__ import annotations

from pipelineguard.models import Finding, Tier

# field name -> entity type recorded for it. Whole-field redaction, always.
DECLARED_PII: dict[str, str] = {
    "account_holder": "PERSON_NAME",
}

# No format to match, so an encoder is the only thing that finds a name here.
# Still goes through Tier 1 too: memo templates embed phones, CNICs and emails,
# which have checksums an encoder cannot verify.
FREE_TEXT: frozenset[str] = frozenset({"memo"})


class SchemaDetector:
    """Redacts declared-PII fields whole. Implements the `Detector` protocol
    from base.py; stateless, with nothing to compile or warm up."""

    name = "schema"

    def detect(self, text: str, field: str) -> list[Finding]:
        entity_type = DECLARED_PII.get(field)
        # An empty field would otherwise get a marker where no name ever was.
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