# Data Governance Report

**Period covered:** all recorded activity (no window specified)  
**Generated:** 2026-07-29 12:47:31Z  
**Source:** PipelineGuard audit trail (`messages_processed`, `findings`)

> This report describes what the pipeline observed and what it did. It is not a compliance determination and not legal advice. Regulatory references indicate why a category of data is significant, not that any obligation has been discharged.

---

## 1. Scope of scan

A finding count means nothing without knowing what was scanned to produce it.

| measure | value |
|---|---|
| Records scanned | 20,000 |
| Earliest event | 2026-07-29 07:04:29Z |
| Latest event | 2026-07-29 07:04:35Z |
| First processed | 2026-07-29 08:18:13Z |
| Last processed | 2026-07-29 08:19:18Z |
| Observed rate | 305 records/second observed |

The observed rate is wall-clock across the span these records actually cover, including any time the pipeline sat idle waiting for input. It is a description of this period, **not a capacity measurement**, and will read lower than a saturated benchmark of the same system.

**Sources**

| topic | records |
|---|---|
| txn.raw | 20,000 |

## 2. Disposition

| outcome | records | share |
|---|---|---|
| quarantined | 773 | 3.87% |
| redacted | 19,227 | 96.14% |

`clean` -- no personal data detected. `redacted` -- personal data detected and masked in place before the record was forwarded. `quarantined` -- withheld from the clean stream for review; see section 6.

## 3. Detection tiers

| highest tier reached | records |
|---|---|
| Tier 1 (rules) | 20,000 |

## 4. Personal data observed

| entity type | category | mentions | records | detected by | min confidence |
|---|---|---|---|---|---|
| IBAN_PK | Financial account identifier | 40,000 | 20,000 | Tier 1 (rules) | 0.50 |
| PHONE_PK | Contact identifier | 23,933 | 20,000 | Tier 1 (rules) | 1.00 |
| EMAIL | Contact identifier | 21,991 | 20,000 | Tier 1 (rules) | 1.00 |
| CNIC | National identity number | 21,955 | 20,000 | Tier 1 (rules) | 1.00 |

Counts are of *detections*, not of distinct individuals -- the same person appearing in two records is counted twice, because the audit trail stores no value with which to link them. That is a deliberate consequence of not retaining personal data.

### Regulatory basis

**IBAN_PK** -- Financial account identifier

- *Pakistan:* Customer account information, subject to the banking confidentiality expectations that apply to institutions regulated by the State Bank of Pakistan.
- *GDPR:* Personal data under Art. 4(1) where it identifies a natural person; account identifiers are the clearest cross-border case, since an IBAN exists to be used internationally.

**PHONE_PK** -- Contact identifier

- *Pakistan:* Subscriber contact data. Pakistani mobile numbers are biometrically registered to a CNIC, so a number is more closely bound to a verified identity than in most jurisdictions.
- *GDPR:* An identifier under Art. 4(1).

**EMAIL** -- Contact identifier

- *Pakistan:* Subscriber contact data.
- *GDPR:* An identifier under Art. 4(1).

**CNIC** -- National identity number

- *Pakistan:* NADRA-issued national identifier. Unauthorised access to or disclosure of identity data held in an information system engages the offences created by PECA 2016.
- *GDPR:* An identifier under Art. 4(1); a national identification number is expressly contemplated as identifying data.

> GDPR is relevant only where the data subject is in the EU -- for this pipeline, principally inbound remittances and EU-resident account holders. It does not attach to purely domestic transactions.


## 5. Where it was found

| entity type | field | mentions |
|---|---|---|
| CNIC | `cnic` | 20,000 |
| CNIC | `memo` | 1,955 |
| EMAIL | `email` | 20,000 |
| EMAIL | `memo` | 1,991 |
| IBAN_PK | `iban_from` | 20,000 |
| IBAN_PK | `iban_to` | 20,000 |
| PHONE_PK | `phone` | 20,000 |
| PHONE_PK | `memo` | 3,933 |

## 6. Items requiring review

773 record(s) were quarantined: 0 failed closed and 773 were withheld as uncertain. The two are separate worklists.

### 6.1 Failed closed -- engineering

Deterministic failures: unparseable bytes, wrong payload shape, or a detector that raised. These will fail identically on replay, so they need a fix rather than a decision.

None in this period.

### 6.2 Uncertain -- human decision

The pipeline detected something it was not confident enough to act on, and declined to guess. Each needs a person to confirm or clear it.

| message id | processed | reason | detail |
|---|---|---|---|
| `1f30a46d-e7a0-484c-9436-16a294fad83e` | 2026-07-29 08:19:18Z | sub-threshold confidence | CNIC, EMAIL, IBAN_PK, PHONE_PK (lowest confidence 0.50) |
| `fcdff858-c9b0-4153-bcc4-3af7cae3e17c` | 2026-07-29 08:19:18Z | sub-threshold confidence | CNIC, EMAIL, IBAN_PK, PHONE_PK (lowest confidence 0.50) |
| `7a2f91a7-4c8a-4a8e-993c-6ba9ba6d58ab` | 2026-07-29 08:19:18Z | sub-threshold confidence | CNIC, EMAIL, IBAN_PK, PHONE_PK (lowest confidence 0.50) |
| `6ad83651-a4dd-4529-aab6-19ba155bf912` | 2026-07-29 08:19:18Z | sub-threshold confidence | CNIC, EMAIL, IBAN_PK, PHONE_PK (lowest confidence 0.50) |
| `20559e8f-2cae-4f98-a65c-bb8a4994db49` | 2026-07-29 08:19:18Z | sub-threshold confidence | CNIC, EMAIL, IBAN_PK, PHONE_PK (lowest confidence 0.50) |

*Showing 5 of 773.*

## 7. Processing failures

No processing failures were recorded in this period.

## 8. System properties

**Records of processing (GDPR Art. 30)**  
The audit trail records, per message, which categories of personal data were detected, in which field, at what time, and what was done about it. That is the substance of a record of processing activities, produced automatically rather than maintained by hand.

**Pseudonymisation (GDPR Art. 32(1)(a))**  
Detected values are redacted in-stream before the record is forwarded, so downstream consumers receive data from which the identifiers have been removed.

**Data minimisation (GDPR Art. 5(1)(c))**  
The audit trail stores entity type, field, character span, tier and confidence. It never stores the matched value. The governance record is therefore not itself a store of personal data.

**Erasure (GDPR Art. 17)**  
Because no values are retained, there is nothing in the audit trail to erase in response to a request. This is a property of the design, not an outstanding gap.

## 9. Limitations

Stated because a governance document that overstates its own coverage is worse than none.

- **This report describes what entered the pipeline, not what existed.** Whether every record reached it is a question about Kafka offsets and consumer lag, which this audit trail cannot answer. It is not evidence that a source system was scanned completely.
- **Detection is not exhaustive.** Only entity types the configured detectors recognise can appear here. Personal data of a type no detector covers passes through unrecorded and is indistinguishable, in this report, from its absence.
- **Counts are of detections, not of people.** No value is stored, so records referring to the same individual cannot be linked.
- **Latency figures elsewhere in this project measure detection only**, not end-to-end processing time.
- **Delivery is at-least-once.** A record processed twice is upserted on its message id, so it is counted once here -- but the guarantee is idempotent recording, not exactly-once processing.
