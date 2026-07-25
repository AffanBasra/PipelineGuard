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
- [x] Tier 1: regex/checksum rules (CNIC, PK IBAN, PK phone, email)
- [x] Processor: consume → detect → redact → route → audit
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
-- why were records quarantined without findings?
SELECT failure_class, count(*) FROM messages_processed
 WHERE failure_class IS NOT NULL GROUP BY failure_class;
```

> Postgres is published on host port **5433** (5432 is commonly taken by a
> native install). The container still listens on 5432 internally.

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

Offsets are committed asynchronously on purpose: safety comes from idempotent
audit upserts, not from commit synchrony, and a synchronous commit would add a
group-coordinator round trip (~ms) to a per-message budget measured in
microseconds. A commit is issued only after `flush()` has drained *and* the
per-message delivery callback reported no error — both checks are required,
since a message can leave the producer queue by failing.

**Two failure classes, handled oppositely.** Conflating them yields either an
infinite crash loop or silent loss:

| failure | nature | response |
|---|---|---|
| unparseable bytes, wrong payload shape, detector raised | deterministic — fails identically on replay | **fail closed**: quarantine with the reason recorded, commit, continue |
| broker unreachable, Postgres down, delivery unconfirmed | transient — likely succeeds on replay | **crash and replay**: don't commit, let the process die, restart replays |

The dividing line is literal: message-level work sits inside
`process_message()`'s try block; everything after it in the loop is
infrastructure and is allowed to propagate.

**Audit before emit.** The audit row is written *before* the message is
produced downstream, so no record is ever emitted that has not already been
recorded. A Postgres outage halts the pipeline rather than letting unlogged
data through — the correct posture for a governance tool.

**Audit trail stores no PII, but does store why.** `findings` records entity
type, field, span, tier and confidence — never the matched value.
`messages_processed.failure_class` / `failure_detail` explain fail-closed
quarantines, so "why was this quarantined" is answerable from SQL alone.

**Quarantine policy.** Confident detections are redacted in place and
forwarded; only *uncertain* records (sub-threshold confidence, malformed
messages) are quarantined — mirroring how production DLP systems avoid
blocking the happy path.

## Observability

The producer shows a progress bar (bounded work). The processor consumes an
unbounded stream, so it emits a periodic stats line instead — which doubles as
the source of the benchmark numbers below:

```
12:04:31 INFO    pipelineguard.processor  processed=12,480 clean=0 redacted=11,982
                 quarantined=495 failed=3 | rate=842/s p50=1.10ms p95=2.40ms p99=6.80ms
```

Per-message logging is DEBUG-only by design: at the rates this pipeline
reaches, logging every message makes stdout the bottleneck and the benchmark
measures the terminal rather than the pipeline.

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
  observability.py               console logging + rolling throughput/latency stats
```
