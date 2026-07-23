# PipelineGuard

A streaming PII / data-quality firewall for Kafka, with a Pakistani-locale focus.

Raw events flow through a tiered detection pipeline; PII is redacted in-stream,
uncertain records are quarantined for review, and every decision is written to
a Postgres audit trail from which a governance report is generated.

```
                        ┌──────────────────┐
 txn.raw ──────────────►│  processor        │──► txn.clean       (redacted)
 (synthetic bank txns)  │  T1 rules (µs)    │──► txn.quarantine  (uncertain)
                        │  T2 encoder (ms)  │
                        │  T3 LLM (escalate)│──► Postgres audit ──► governance report
                        └──────────────────┘
```

> **Honest scope:** the input streams are synthetic (Faker + hand-built
> Pakistani-locale generators — CNIC, PK IBAN, Urdu/Roman-Urdu names). No real
> PII is processed or stored; the audit trail records entity *types and spans*,
> never values. Delivery semantics are **at-least-once with idempotent audit
> writes** — see [Design decisions](#design-decisions) for why not exactly-once.

## Status

- [x] Docker Compose stack (Kafka 3.8 KRaft, Postgres 16)
- [x] Synthetic Pakistani bank-transaction stream + producer
- [ ] Tier 1: regex/checksum rules (CNIC, PK IBAN, PK phone, email) — in progress
- [ ] Processor: consume → detect → redact → route → audit — in progress
- [ ] Tier 2: fine-tuned encoder NER (Urdu/Roman-Urdu names)
- [ ] Tier 3: pluggable LLM escalation (Gemini, Ollama)
- [ ] Benchmarks: throughput + p50/p99 latency per tier
- [ ] Support-chat free-text topic
- [ ] Governance report generator
- [ ] Airflow batch-scan mode

## Quick start

```bash
docker compose up -d
pip install -r requirements.txt
python scripts/create_topics.py
python -m pipelineguard.producer --rate 50 --count 1000   # feed txn.raw
python -m pipelineguard.processor                          # run the firewall
```

Inspect results:

```sql
SELECT action, count(*) FROM messages_processed GROUP BY action;
SELECT entity_type, tier, count(*) FROM findings GROUP BY 1, 2 ORDER BY 3 DESC;
```

## Design decisions

**Tiered detection under a latency budget.** Tier 1 (compiled regex +
checksum validation, µs) handles structured PII; Tier 2 (fine-tuned encoder,
ms) handles names and contextual PII in free text; Tier 3 (LLM) is invoked
only for spans where Tier 2 confidence falls in an uncertainty band. The
tiering is a throughput/cost tradeoff, not just an accuracy ladder — the
benchmark tables (below, TBD) quantify it.

**At-least-once, not exactly-once.** Kafka transactions can make the
consume→produce→commit leg exactly-once, but this pipeline's outputs cross a
transaction boundary into Postgres, which Kafka cannot roll back. Rather than
claim EOS with a caveat, the pipeline commits offsets only after produce +
audit are durable, and the audit writer upserts on `message_id` — so
redelivery is harmless. Enabling transactions on the Kafka→Kafka leg is a
documented stretch item.

**Quarantine policy.** Confident detections are redacted in place and
forwarded; only *uncertain* records (sub-threshold confidence, malformed
messages) are quarantined — mirroring how production DLP systems avoid
blocking the happy path.

**Audit trail stores no PII.** `findings` records entity type, field, span,
tier, confidence — never the matched value.

## Benchmarks

TBD. Methodology will state hardware, message-size distribution, and measure
Tier 3 separately (API-bound latency is not comparable to local tiers).

## Project layout

```
db/init.sql                      audit schema (idempotent upserts by message_id)
scripts/create_topics.py         explicit topic creation (auto-create disabled)
src/pipelineguard/
  config.py                      env-driven settings
  models.py                      Envelope, Finding, Tier
  generator/transactions.py      synthetic PK-locale transaction generator
  producer.py                    rate-controlled producer with delivery callbacks
  detectors/base.py              Detector protocol
  detectors/tier1_rules.py       Tier 1 rules engine
  processor.py                   the stream processor (consume→detect→route→audit)
  audit.py                       idempotent Postgres audit writer
```
