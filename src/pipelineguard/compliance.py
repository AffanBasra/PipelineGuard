"""Regulatory classification of the entity types the pipeline detects.

What this module is: a mapping from PipelineGuard's entity types to the data
category each one falls into, plus the regime that makes it matter. It exists
so the governance report can say *why* a finding is worth a reviewer's
attention rather than only how many there were.

What this module is NOT: legal advice, or a compliance determination.
Compliance is a property of an organisation and its processing, not something
a detector can assert about itself. Every string below describes **what the
system observed**; the report is worded as *designed to support* obligations,
never *compliant with* them.

Grounding follows the project's scope decision (docs/decisions.md section 1):
Pakistani law first, because a Pakistani bank pipeline mapped to Pakistani
obligations is the distinctive artifact; GDPR only where it genuinely applies,
principally cross-border transactions, where a Pakistani institution
processing the personal data of EU data subjects falls within the
extraterritorial scope of Article 3(2).

**Citations are deliberately general.** Section numbers, SBP circular
references and the status of the draft Personal Data Protection Bill must be
verified against primary sources before this text appears in any public or
client-facing document. The structure of the mapping is defensible; the
precise references are not yet, and overstating them would violate the
project's standing "only interview-defensible claims" constraint.
"""
from __future__ import annotations

from dataclasses import dataclass

# Applies to every classification below rather than being repeated in each.
CROSS_BORDER_NOTE = (
    "GDPR is relevant only where the data subject is in the EU -- for this "
    "pipeline, principally inbound remittances and EU-resident account "
    "holders. It does not attach to purely domestic transactions."
)

DISCLAIMER = (
    "This report describes what the pipeline observed and what it did. It is "
    "not a compliance determination and not legal advice. Regulatory "
    "references indicate why a category of data is significant, not that any "
    "obligation has been discharged."
)


@dataclass(frozen=True)
class Classification:
    """Why a detected entity type matters, and to whom."""

    data_category: str
    pk_basis: str
    gdpr_basis: str
    # GDPR Article 9 special categories: racial or ethnic origin, political
    # opinions, religious belief, trade union membership, genetic and
    # biometric data, health, sex life and sexual orientation.
    special_category: bool = False


CLASSIFICATIONS: dict[str, Classification] = {
    "CNIC": Classification(
        data_category="National identity number",
        pk_basis=(
            "NADRA-issued national identifier. Unauthorised access to or "
            "disclosure of identity data held in an information system "
            "engages the offences created by PECA 2016."
        ),
        gdpr_basis=(
            "An identifier under Art. 4(1); a national identification number "
            "is expressly contemplated as identifying data."
        ),
    ),
    "IBAN_PK": Classification(
        data_category="Financial account identifier",
        pk_basis=(
            "Customer account information, subject to the banking "
            "confidentiality expectations that apply to institutions "
            "regulated by the State Bank of Pakistan."
        ),
        gdpr_basis=(
            "Personal data under Art. 4(1) where it identifies a natural "
            "person; account identifiers are the clearest cross-border case, "
            "since an IBAN exists to be used internationally."
        ),
    ),
    "PHONE_PK": Classification(
        data_category="Contact identifier",
        pk_basis=(
            "Subscriber contact data. Pakistani mobile numbers are "
            "biometrically registered to a CNIC, so a number is more closely "
            "bound to a verified identity than in most jurisdictions."
        ),
        gdpr_basis="An identifier under Art. 4(1).",
    ),
    "EMAIL": Classification(
        data_category="Contact identifier",
        pk_basis="Subscriber contact data.",
        gdpr_basis="An identifier under Art. 4(1).",
    ),
}

# Article 30 (records of processing activities) is the strongest alignment this
# project has, and it is structural rather than a claim: the audit trail
# records categories of personal data and what was done with them, which is
# most of what Art. 30(1) asks a controller to maintain.
SYSTEM_PROPERTIES: list[tuple[str, str]] = [
    (
        "Records of processing (GDPR Art. 30)",
        "The audit trail records, per message, which categories of personal "
        "data were detected, in which field, at what time, and what was done "
        "about it. That is the substance of a record of processing "
        "activities, produced automatically rather than maintained by hand.",
    ),
    (
        "Pseudonymisation (GDPR Art. 32(1)(a))",
        "Detected values are redacted in-stream before the record is "
        "forwarded, so downstream consumers receive data from which the "
        "identifiers have been removed.",
    ),
    (
        "Data minimisation (GDPR Art. 5(1)(c))",
        "The audit trail stores entity type, field, character span, tier and "
        "confidence. It never stores the matched value. The governance "
        "record is therefore not itself a store of personal data.",
    ),
    (
        "Erasure (GDPR Art. 17)",
        "Because no values are retained, there is nothing in the audit trail "
        "to erase in response to a request. This is a property of the "
        "design, not an outstanding gap.",
    ),
]


def classify(entity_type: str) -> Classification | None:
    """Return the classification for `entity_type`, or None if unmapped."""
    return CLASSIFICATIONS.get(entity_type)


def unclassified(entity_types: list[str]) -> list[str]:
    """Entity types with no classification, in the order first seen.

    Surfaced prominently in the report rather than silently omitted. A
    governance document that quietly drops a category of personal data it
    could not classify is worse than one that admits the gap -- the reader
    has no way to tell the difference between "none found" and "found and
    not understood".
    """
    seen: dict[str, None] = {}
    for entity_type in entity_types:
        if entity_type not in CLASSIFICATIONS:
            seen.setdefault(entity_type, None)
    return list(seen)