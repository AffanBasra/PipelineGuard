# Data Governance Summary

**Period:** all recorded activity (no window specified)  
**Generated:** 2026-08-14 13:23:40Z  
**Source:** PipelineGuard audit trail (`messages_processed`, `findings`)

> This is a description of what the pipeline saw and did. It is not a compliance determination and not legal advice.

## 1. Processing scope

| measure | value |
|---|---|
| Records scanned | 5,000 |
| Period covered | all recorded activity (no window specified) |
| First record processed | 2026-08-07 09:16:48Z |
| Last record processed | 2026-08-12 12:33:12Z |
| Observed rate | 0 records/second observed |

### What happened to those records

| outcome | records | share | meaning |
|---|---|---|---|
| Quarantined | 209 | 4.2% | Held back from the clean stream for a person to look at. |
| Redacted | 4,791 | 95.8% | Personal data found and masked before the record moved on. |

## 2. Which tier caught what

| caught by | records | share |
|---|---|---|
| Tier 1 -- rules | 2,714 | 54.3% |
| Tier 2 -- encoder | 2,286 | 45.7% |

Tier 1 matches fixed formats -- identity, account and contact details -- and is exact. Tier 2 reads free text for names and addresses, which have no fixed format, and reports a confidence score with each find.

<!-- PAGE BREAK -->

## 3. Personal data inventory

| data type | category | detections | records | found in | detected by |
|---|---|---|---|---|---|
| IBAN_PK | Financial account identifier | 10,000 | 5,000 | iban_from, iban_to | Tier 1 -- rules |
| PERSON_NAME | Name | 8,053 | 5,000 | account_holder, memo | tiers 1-2 |
| PHONE_PK | Contact identifier | 5,487 | 5,000 | phone, memo | Tier 1 -- rules |
| CNIC | National identity number | 5,200 | 5,000 | cnic, memo | Tier 1 -- rules |
| EMAIL | Contact identifier | 5,187 | 5,000 | email, memo | Tier 1 -- rules |
| ADDRESS | Residential or business address | 1,343 | 1,343 | memo | Tier 2 -- encoder |

Detections, not people. The same person in two records counts twice, because no value is stored that could link them.

## 4. Quarantine and review worklist

**209 record(s)** were held back for review: 0 could not be processed at all, and 209 were withheld because a rule check did not pass cleanly.

The most common trigger, across a sample of 50 of the withheld records, is **IBAN_PK** failing its validation check (50 of them).

Message ids are not printed here. They identify records, and this document is meant to be shareable. The queue itself is held in the `txn.quarantine` topic, where a reviewer can work through it.

## 5. Limitations

- **This covers what reached the pipeline, not what exists.** Whether every record arrived is a question this audit trail cannot answer.
- **Detection is not exhaustive.** Only data types the configured detectors know about can appear here.
- **Counts are detections, not individuals.** No value is stored, so records about the same person cannot be linked.
- **Address masking is partial by nature.** A fragment can remain in a record that is otherwise masked.
- **Delivery is at-least-once.** A record seen twice is counted once, but that is idempotent recording, not exactly-once processing.

<!-- PAGE BREAK -->

## 6. Compliance framework mapping

Why the pipeline behaves the way it does, and what each regime makes of it. These explain significance. They are not claims that any obligation has been met.

> Pakistan has no enacted general data protection statute. PECA 2016 creates criminal offences for unauthorised access to an information system and for misuse of identity information, but imposes no record-of-processing or data-subject-rights obligations on a controller. The Personal Data Protection Bill, which would supply that framework, remains a draft and is not in force. The Pakistani basis cited below is therefore a combination of the offences PECA does create, the sectoral expectations the State Bank of Pakistan places on regulated financial institutions, and the direction of the draft Bill -- not a single controlling statute. GDPR is cited where cross-border processing brings it into scope, and its articles are numbered because they exist.

### 6.1 How the pipeline is built

**Automatic record of processing.** The audit trail records, per message, which categories of personal data were detected, in which field, at what time, and what was done about it -- produced automatically as a consequence of processing rather than maintained by hand.

- *Pakistan:* No enacted Pakistani statute requires this today. PECA 2016 creates offences and imposes no record-keeping duty; the draft Personal Data Protection Bill moves in this direction. For a bank, SBP's technology governance expectations require auditable control over customer data, which this satisfies in substance.
- *GDPR:* Art. 30 -- records of processing activities. This is the strongest alignment the project has, and it is structural rather than a claim: the record is a by-product of the pipeline running.

**Redaction in stream.** Detected values are masked in place before the record is forwarded, so downstream consumers receive data from which the identifiers have been removed.

- *Pakistan:* Directly reduces the surface for the PECA 2016 offences: a downstream system that never receives a CNIC cannot become the point at which identity information is unlawfully accessed or copied. For SBP-regulated institutions it also narrows who holds customer account data.
- *GDPR:* Art. 32(1)(a) -- pseudonymisation as a security measure.

**The audit trail stores no values.** It stores entity type, field, character span, tier and confidence -- never the matched text. The governance record is therefore not itself a store of personal data. This is a property of the audit database only: txn.raw carries unredacted input and txn.quarantine deliberately carries original bytes so reviewers see what arrived, both retained on the broker (24h and 72h respectively) under whatever access controls the deployment provides.

- *Pakistan:* Keeps the compliance artifact from enlarging the institution's own exposure. A governance log that accumulated CNICs would become precisely the concentrated identity store that PECA 2016 exists to protect, defeating its purpose.
- *GDPR:* Art. 5(1)(c) -- data minimisation.

**Nothing to erase.** Because no values are retained, there is nothing in the audit trail to erase in response to a request. This is a property of the design, not an outstanding gap.

- *Pakistan:* Anticipates the data-subject rights the draft Personal Data Protection Bill would introduce. No such enforceable right exists under current Pakistani law, so this is forward positioning rather than a discharged obligation.
- *GDPR:* Art. 17 -- right to erasure.

### 6.2 Why each data type matters

**IBAN_PK** -- Financial account identifier

- *Pakistan:* Customer account information. Institutions regulated by the State Bank of Pakistan are subject to banking secrecy expectations and to SBP's technology governance and risk management framework for financial institutions, which requires auditable controls over customer data. This is the entity type where the Pakistani obligation is most concrete, because it is sectoral regulation rather than general law.
- *GDPR:* Personal data under Art. 4(1) where it identifies a natural person; account identifiers are the clearest cross-border case, since an IBAN exists to be used internationally.

**PERSON_NAME** -- Name

- *Pakistan:* A name is not an identifier issued by any authority, so no Pakistan-specific instrument attaches to it the way the NADRA Ordinance attaches to a CNIC. It is personal data under the draft Personal Data Protection Bill's definition, and PECA 2016 reaches it only through the general unauthorised-access and identity-misuse offences. Its practical significance here is combinatorial: a name beside an account number in the same record is what turns a transaction into an identified one, which is why it is redacted rather than tolerated.
- *GDPR:* Personal data under Art. 4(1) -- the canonical example, since a name is the paradigm case of information relating to an identified natural person.

**PHONE_PK** -- Contact identifier

- *Pakistan:* More tightly bound to a verified identity than in most jurisdictions: SIM issuance is subject to biometric verification against NADRA records under the Pakistan Telecommunication Authority's registration regime, so a mobile number resolves to an identified person. PECA 2016 separately criminalises unauthorised SIM issuance. Treating a Pakistani mobile number as low-sensitivity contact data would therefore understate it.
- *GDPR:* An identifier under Art. 4(1).

**CNIC** -- National identity number

- *Pakistan:* Issued by NADRA under the National Database and Registration Authority Ordinance 2000, which governs the identity database and restricts use of the records it holds. PECA 2016 separately criminalises unauthorised use of identity information. The CNIC is the join key across Pakistani financial, telecom and government systems, so its exposure is the highest-consequence single event in this pipeline.
- *GDPR:* An identifier under Art. 4(1); a national identification number is expressly contemplated as identifying data.

**EMAIL** -- Contact identifier

- *Pakistan:* No Pakistan-specific instrument attaches to an email address as such. It is personal data under the draft Personal Data Protection Bill's definition, and is protected today only by the general unauthorised-access and unauthorised-copying offences in PECA 2016 -- which bite on how the data is obtained, not on how a controller handles it.
- *GDPR:* An identifier under Art. 4(1).

**ADDRESS** -- Residential or business address

- *Pakistan:* Like a name, an address is not an identifier issued by any authority, and no Pakistan-specific instrument attaches to it directly. It is personal data under the draft Personal Data Protection Bill's definition. Its significance here is that it is a LOCATION: a name beside an account number identifies a person, while an address beside either says where to find them, which is a different order of harm and the reason a partial redaction of one is worse than none. It is also the only entity type in this pipeline whose redaction is routinely incomplete rather than binary -- see docs/tier2-detection-findings.md sections 17 and 18.
- *GDPR:* Personal data under Art. 4(1) where it relates to an identified or identifiable natural person. A business address on its own generally does not; a home address does, and this pipeline cannot tell them apart at detection time.

> GDPR is relevant only where the data subject is in the EU -- for this pipeline, principally inbound remittances and EU-resident account holders. It does not attach to purely domestic transactions.
