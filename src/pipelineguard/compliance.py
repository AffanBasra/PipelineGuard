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

**Citations are deliberately general, and were cross-checked on 2026-07-29**
against independent references, which found no inconsistency. Named
instruments -- PECA 2016, the NADRA Ordinance 2000, PTA SIM registration, SBP
technology-governance expectations, the draft Personal Data Protection Bill --
are cited by name and effect rather than by section number, which is the level
this text can support.

A cross-check is not primary-source verification. Anything client-facing or
formal should be checked against the primary texts, and a section-level
citation should not be added here without one. The distinction is recorded
rather than assumed: quietly upgrading a cross-check into a verification is
exactly how an accuracy constraint erodes.
"""
from __future__ import annotations

from dataclasses import dataclass

# Applies to every classification below rather than being repeated in each.
CROSS_BORDER_NOTE = (
    "GDPR is relevant only where the data subject is in the EU -- for this "
    "pipeline, principally inbound remittances and EU-resident account "
    "holders. It does not attach to purely domestic transactions."
)

# The two regimes below differ in character, not merely in detail, and a
# reader comparing them will notice that the Pakistani column cites no
# equivalent of Art. 30. Stating why is more credible than padding it to
# match: the asymmetry is a fact about the legal landscape, and a Pakistani
# governance audience will recognise it immediately.
LEGAL_LANDSCAPE_NOTE = (
    "Pakistan has no enacted general data protection statute. PECA 2016 "
    "creates criminal offences for unauthorised access to an information "
    "system and for misuse of identity information, but imposes no "
    "record-of-processing or data-subject-rights obligations on a controller. "
    "The Personal Data Protection Bill, which would supply that framework, "
    "remains a draft and is not in force. The Pakistani basis cited below is "
    "therefore a combination of the offences PECA does create, the sectoral "
    "expectations the State Bank of Pakistan places on regulated financial "
    "institutions, and the direction of the draft Bill -- not a single "
    "controlling statute. GDPR is cited where cross-border processing brings "
    "it into scope, and its articles are numbered because they exist."
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
            "Issued by NADRA under the National Database and Registration "
            "Authority Ordinance 2000, which governs the identity database "
            "and restricts use of the records it holds. PECA 2016 separately "
            "criminalises unauthorised use of identity information. The CNIC "
            "is the join key across Pakistani financial, telecom and "
            "government systems, so its exposure is the highest-consequence "
            "single event in this pipeline."
        ),
        gdpr_basis=(
            "An identifier under Art. 4(1); a national identification number "
            "is expressly contemplated as identifying data."
        ),
    ),
    "IBAN_PK": Classification(
        data_category="Financial account identifier",
        pk_basis=(
            "Customer account information. Institutions regulated by the "
            "State Bank of Pakistan are subject to banking secrecy "
            "expectations and to SBP's technology governance and risk "
            "management framework for financial institutions, which requires "
            "auditable controls over customer data. This is the entity type "
            "where the Pakistani obligation is most concrete, because it is "
            "sectoral regulation rather than general law."
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
            "More tightly bound to a verified identity than in most "
            "jurisdictions: SIM issuance is subject to biometric verification "
            "against NADRA records under the Pakistan Telecommunication "
            "Authority's registration regime, so a mobile number resolves to "
            "an identified person. PECA 2016 separately criminalises "
            "unauthorised SIM issuance. Treating a Pakistani mobile number as "
            "low-sensitivity contact data would therefore understate it."
        ),
        gdpr_basis="An identifier under Art. 4(1).",
    ),
    "EMAIL": Classification(
        data_category="Contact identifier",
        # Deliberately the weakest entry here, and said plainly rather than
        # padded to match the others. Inventing a Pakistan-specific basis for
        # an email address would be exactly the overclaiming this module's
        # docstring warns against.
        pk_basis=(
            "No Pakistan-specific instrument attaches to an email address as "
            "such. It is personal data under the draft Personal Data "
            "Protection Bill's definition, and is protected today only by the "
            "general unauthorised-access and unauthorised-copying offences in "
            "PECA 2016 -- which bite on how the data is obtained, not on how "
            "a controller handles it."
        ),
        gdpr_basis="An identifier under Art. 4(1).",
    ),
    "PERSON_NAME": Classification(
        data_category="Name",
        # The only entity type here that is not a structured identifier, and
        # the only one detected by two different mechanisms. Both facts change
        # how a reviewer should read a count of it, so both are stated.
        pk_basis=(
            "A name is not an identifier issued by any authority, so no "
            "Pakistan-specific instrument attaches to it the way the NADRA "
            "Ordinance attaches to a CNIC. It is personal data under the "
            "draft Personal Data Protection Bill's definition, and PECA 2016 "
            "reaches it only through the general unauthorised-access and "
            "identity-misuse offences. Its practical significance here is "
            "combinatorial: a name beside an account number in the same "
            "record is what turns a transaction into an identified one, "
            "which is why it is redacted rather than tolerated."
        ),
        gdpr_basis=(
            "Personal data under Art. 4(1) -- the canonical example, since a "
            "name is the paradigm case of information relating to an "
            "identified natural person."
        ),
    ),
}

# Entity types found by more than one detector, and what that means for a
# count. PERSON_NAME comes from a schema rule on declared fields at confidence
# 1.0, and from the Tier 2 encoder over free text at whatever it scored -- so
# its total mixes a contractual guarantee with a probabilistic judgement, and
# its minimum confidence describes only the latter.
MIXED_PROVENANCE: dict[str, str] = {
    "PERSON_NAME": (
        "Detected two ways: declared fields are redacted whole from the "
        "schema at confidence 1.0, while names in free text are found by the "
        "Tier 2 encoder and carry its score. A minimum confidence below 1.0 "
        "therefore reflects the encoder's uncertainty about free text, not "
        "any doubt about the declared field."
    ),
}

@dataclass(frozen=True)
class SystemProperty:
    """A thing the pipeline does, and why each regime cares.

    `behaviour` is regime-neutral and is the only part asserting fact about
    this system; the two bases explain significance. Splitting them this way
    is what stops the section from reading as a GDPR compliance checklist
    with Pakistani decoration, which is what it was before -- all four
    entries cited GDPR articles and no Pakistani instrument at all, inverting
    the scope decision in docs/decisions.md section 1.
    """

    title: str
    behaviour: str
    pk_basis: str
    gdpr_basis: str


SYSTEM_PROPERTIES: list[SystemProperty] = [
    SystemProperty(
        title="Automatic record of processing",
        behaviour=(
            "The audit trail records, per message, which categories of "
            "personal data were detected, in which field, at what time, and "
            "what was done about it -- produced automatically as a "
            "consequence of processing rather than maintained by hand."
        ),
        pk_basis=(
            "No enacted Pakistani statute requires this today. PECA 2016 "
            "creates offences and imposes no record-keeping duty; the draft "
            "Personal Data Protection Bill moves in this direction. For a "
            "bank, SBP's technology governance expectations require auditable "
            "control over customer data, which this satisfies in substance."
        ),
        gdpr_basis=(
            "Art. 30 -- records of processing activities. This is the "
            "strongest alignment the project has, and it is structural rather "
            "than a claim: the record is a by-product of the pipeline running."
        ),
    ),
    SystemProperty(
        title="Redaction in stream",
        behaviour=(
            "Detected values are masked in place before the record is "
            "forwarded, so downstream consumers receive data from which the "
            "identifiers have been removed."
        ),
        pk_basis=(
            "Directly reduces the surface for the PECA 2016 offences: a "
            "downstream system that never receives a CNIC cannot become the "
            "point at which identity information is unlawfully accessed or "
            "copied. For SBP-regulated institutions it also narrows who "
            "holds customer account data."
        ),
        gdpr_basis="Art. 32(1)(a) -- pseudonymisation as a security measure.",
    ),
    SystemProperty(
        title="The audit trail stores no values",
        behaviour=(
            "It stores entity type, field, character span, tier and "
            "confidence -- never the matched text. The governance record is "
            "therefore not itself a store of personal data. This is a "
            "property of the audit database only: txn.raw carries unredacted "
            "input and txn.quarantine deliberately carries original bytes so "
            "reviewers see what arrived, both retained on the broker (24h and "
            "72h respectively) under whatever access controls the deployment "
            "provides."
        ),
        pk_basis=(
            "Keeps the compliance artifact from enlarging the institution's "
            "own exposure. A governance log that accumulated CNICs would "
            "become precisely the concentrated identity store that PECA 2016 "
            "exists to protect, defeating its purpose."
        ),
        gdpr_basis="Art. 5(1)(c) -- data minimisation.",
    ),
    SystemProperty(
        title="Nothing to erase",
        behaviour=(
            "Because no values are retained, there is nothing in the audit "
            "trail to erase in response to a request. This is a property of "
            "the design, not an outstanding gap."
        ),
        pk_basis=(
            "Anticipates the data-subject rights the draft Personal Data "
            "Protection Bill would introduce. No such enforceable right "
            "exists under current Pakistani law, so this is forward "
            "positioning rather than a discharged obligation."
        ),
        gdpr_basis="Art. 17 -- right to erasure.",
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