# PipelineGuard

A streaming PII / data-quality firewall for Kafka, with a Pakistani-locale focus.

Raw events flow through a tiered detection pipeline; PII is redacted in-stream,
uncertain records are quarantined for review, and every decision is written to
a Postgres audit trail from which a governance report is generated.

```
  ┌────────────┐
  │  producer  │   synthetic PK bank transactions, ~40% with no memo
  └─────┬──────┘
        ▼
    txn.raw ─────────────────────────────────── 24h · UNREDACTED input
        │
        ▼
┌───────────────────────────────────────────────────────────────────────┐
│ processor                                    consume(batch ≤ 500)     │
│                                                                       │
│  ── batched, once per Kafka batch ─────────────────────────────────   │
│   A. collect non-empty free text          (best-effort, decides none) │
│   B. ONE encoder call, chunked at 8   ──────────────► GPU / CPU       │
│                                                                       │
│  ── per message: DISPATCH BY FIELD, not escalation ────────────────   │
│   account_holder  →  schema rule      whole field, confidence 1.0     │
│   memo            →  T1 rules  +  T2 encoder     (spans unioned)      │
│   cnic iban phone email → T1 rules    regex + checksum                │
│                                                                       │
│  ── route ─────────────────────────────────────────────────────────   │
│   no findings ─────────────────► clean                                │
│   T1 finding < 1.0 ────────────► quarantine   (original bytes)        │
│   otherwise ─── redact ────────► redacted                             │
│                                                                       │
│   T3 LLM for ambiguous spans …………………………………………………… planned            │
│                                                                       │
│  audit BEFORE emit          ·          commit offsets LAST            │
└──────┬──────────────────────────────────────────┬─────────────────────┘
       ▼                                          ▼
  txn.clean ── 168h · redacted        txn.quarantine ── 72h · UNREDACTED
       │                                          │
       └──────────────► Postgres audit ◄──────────┘
                        type · field · span · tier · confidence
                        never the value
                                 │
                                 ▼
                        governance report (Markdown)
```

**Tier 1 → Tier 2 is dispatch, not escalation.** Tier 1 has no name rule, so a
memo is not a record it was *uncertain* about — it is one it has no opinion on.
The schema already says which fields are free text, so the routing is fixed
before either detector runs. Tier 2 → Tier 3 will be genuine escalation: same
span, promoted on uncertainty.

> **Honest scope:** the input streams are synthetic (Faker + hand-built
> Pakistani-locale generators — CNIC, PK IBAN, Pakistani names in Latin
> script). No real PII is processed or stored; the *audit trail* records entity
> types and spans, never values — though `txn.raw` and `txn.quarantine` do carry
> unredacted payloads, which is why their retention is set explicitly.
> Delivery semantics are **at-least-once with idempotent audit writes** — see
> [Design decisions](#design-decisions) for why not exactly-once.

## Status

- [x] Docker Compose stack (Kafka 3.8 KRaft, Postgres 16)
- [x] Synthetic Pakistani bank-transaction stream + producer
- [x] Tier 1: regex/checksum rules (CNIC, PK IBAN, PK phone, email)
- [x] Processor: consume → detect → redact → route → audit
- [x] Test suite (no broker or database required)
- [x] Micro-batched processing + batch-size benchmark
- [x] Governance report generator (Markdown, from the audit trail)
- [x] Dockerfile + processor service, so `docker compose up` runs the pipeline
- [x] Schema-based redaction of declared PII fields (`account_holder`)
- [x] Tier 2: encoder NER over free text, batched, GPU-optional
- [ ] Tier 2 locale fine-tune (needs an evaluation set of independent provenance)
- [ ] Flagging records whose redaction left nothing
- [ ] End-to-end latency measurement (current figures are detection-only)
- [ ] Tier 3: pluggable LLM escalation (Gemini, Ollama)
- [ ] Support-chat free-text topic
- [ ] Airflow batch-scan mode

## Quick start

```bash
docker compose up -d                                       # broker, database, topics, firewall
docker compose run --rm producer --rate 50 --count 1000    # feed txn.raw
docker compose logs -f processor                           # watch it work
```

That is the whole thing — no local Python needed. `up` brings the stack to a
running pipeline idling on an empty topic: Kafka and Postgres come up, a
one-shot `topics-init` container creates the topics (broker auto-create is
disabled), and the processor starts only once that container has *exited
successfully*, so "the topics probably exist by now" is an ordering guarantee
rather than a hope.

```bash
docker compose run --rm report                             # governance report to stdout
docker compose up -d --scale processor=3                   # three consumers, one per partition
```

### Demonstrating crash-and-replay

The [failure taxonomy](#design-decisions) below claims that an infrastructure
failure crashes the process and replays on restart. `restart: unless-stopped`
on the processor is what makes that real rather than asserted, and it can be
watched:

```bash
docker compose stop postgres                               # infrastructure fails
docker compose run --rm producer --rate 0 --count 500      # keep feeding it anyway
docker compose logs processor | grep OperationalError      # crashing, not swallowing
docker compose start postgres                              # infrastructure returns
```

The processor dies on the audit write, restarts, dies again, and keeps cycling
until Postgres answers — then drains the backlog. Measured over exactly that
sequence: 5 restarts while down, and afterwards **2,500 audit rows for 2,500
produced messages — no loss and no duplication**, with consumer lag back to
zero. Uncommitted offsets are what prevent the loss; the idempotent audit
upsert is what prevents the duplication.

Note that `docker kill` on the container will *not* trigger a restart — Docker
treats an explicit kill as a manual stop. The behaviour above is about the
process exiting, which is the case the design is actually about.

<details>
<summary>Running on the host instead</summary>

```bash
docker compose up -d kafka postgres
pip install -e .                                           # src layout: editable install puts pipelineguard on the path
python scripts/create_topics.py
python -m pipelineguard.producer --rate 50 --count 1000
python -m pipelineguard.processor
```

The host defaults in `config.py` point at `localhost:9092` and `localhost:5433`;
the compose services override them to `kafka:19092` and `postgres:5432`. `.env`
is excluded from the image so it cannot reinstate host values inside a
container.
</details>

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

### Governance report

```bash
python -m pipelineguard.report                              # Markdown to stdout
python -m pipelineguard.report --since 2026-07-01 --output report.md
```

Written for a compliance reviewer: what personal data flowed through, over
what period, what was done about it, and what needs a human. It reads the
audit trail and nothing else — no Kafka, no message payloads — so it cannot
disclose a value the audit trail declined to store. A generated example is
committed at [docs/sample-report.md](docs/sample-report.md).

Its two quarantine queues are deliberately separate: **failed closed** are
deterministic defects that will fail identically on replay and need an
engineering fix, while **uncertain** records are judgements the pipeline
declined to make and need a person. Section 9 states what the report *cannot*
show — chiefly that it describes what entered the pipeline, not what existed,
which is a question about consumer lag that no audit table can answer.

| flag | default | purpose |
|---|---|---|
| `--since` / `--until` | unbounded | ISO-8601 window on `processed_ts`, half-open `[since, until)` so adjacent reports partition exactly |
| `--max-queue` | 50 | max review items listed per queue; totals are counted independently, so truncation is always disclosed |
| `--output` | stdout | write to a file instead |

### Processor flags

| flag | default | purpose |
|---|---|---|
| `--batch-size` | 500 | max messages per `consume()` call — see [Benchmarks](#benchmarks) for why |
| `--batch-timeout-ms` | 1000 | how long to wait for a batch to fill before processing what arrived |
| `--flush-timeout` | 10 | seconds to wait for a batch's produces to be confirmed durable |
| `--stats-every` | 5 | seconds between throughput/latency stats lines |
| `--exit-after` | 0 | stop after N messages (0 = run until interrupted); makes benchmark runs reproducible |
| `--log-level` | INFO | `DEBUG` logs every message — never use it while benchmarking |

## Tier 2 — the encoder

Tier 1 cannot find a name. No rule can: a name has no format to match. That is
the whole reason Tier 2 exists — **capability, not cost.** Before it, a memo
reading `Transfer to Ayesha Malik` reached the clean topic verbatim, and so did
`account_holder`, because Tier 1 produced no findings on either.

Measured on 2,000 records ([full findings](docs/tier2-detection-findings.md)):

| | Tier 1 only | + Tier 2 (GPU) |
|---|---:|---:|
| throughput | 439 rec/s | **159 rec/s** |
| p50 detection | 0.09 ms | 6.43 ms |
| names found in `memo` | **0** | **892** |

`urchade/gliner_multi_pii-v1` at threshold 0.25, **99.4%** character coverage on
PERSON. Four results worth knowing before changing anything:

- **The threshold is not a probability.** It is an uncalibrated sigmoid cutoff
  and does not transfer between checkpoints — `nvidia/gliner-PII` at this
  model's 0.25 fires on 68% of clean Pakistani text. Swapping `TIER2_MODEL`
  means re-running `scripts/probe_ner_sweep.py`; the detector logs a warning if
  you don't.
- **Labels are run one group per forward pass.** Folding them into a single
  pass halves the cost and drops PERSON coverage to 90.9% — the labels compete
  for the same spans.
- **Batching is the whole speedup.** 7.2 ms/record at batch 8 against 29.4 at
  batch 1, which is why inference happens in `main()` and findings are injected
  into `process_message` rather than detected there.
- **ADDRESS was measured, then removed.** Nothing in this pipeline contains an
  address, so the pass could only ever be wrong — it fired on 30% of memos,
  mostly re-tagging names. Restoring it is one line, plus a corpus.

Off by default. Enable with `TIER2_ENABLED=true`, `TIER2_DEVICE=cuda|cpu|auto`.

**Over-redaction is the accepted cost.** At this threshold ~38% of clean
Roman-Urdu memos fire, and some are destroyed whole (`Kiraya jama karwa diya` →
`[PERSON_NAME]`). Coverage was deliberately not traded away to fix it, and the
false positives score up to 0.98 — high enough that no threshold change removes
them. Flagging fully-saturated redactions is the open mitigation.

## Design decisions

**Tiered detection under a latency budget.** Tier 1 (compiled regex +
checksum validation, µs) handles structured PII; Tier 2 (fine-tuned encoder,
ms) handles names and contextual PII in free text; Tier 3 (LLM) is invoked
only for spans where Tier 2 confidence falls in an uncertainty band. The
tiering is a throughput/cost tradeoff, not just an accuracy ladder — the
benchmark table [below](#benchmarks) quantifies it — and shows detection is
currently only ~15% of per-record cost, so the plumbing dominates the rules.

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

**Three failure classes, handled differently.** Conflating message-level and
infrastructure failures yields either an infinite crash loop or silent loss;
conflating either with routine coordination events does the same in a
narrower way:

| failure | nature | response |
|---|---|---|
| unparseable bytes, wrong payload shape, detector raised | deterministic — fails identically on replay | **fail closed**: quarantine with the reason recorded, commit, continue |
| broker unreachable, Postgres down, delivery unconfirmed | transient — likely succeeds on replay | **crash and replay**: don't commit, let the process die, restart replays |
| commit rejected because the consumer group rebalanced under it (`ILLEGAL_GENERATION`, `UNKNOWN_MEMBER_ID`, `REBALANCE_IN_PROGRESS`) | routine — not a sign the coordinator is unreachable | **tolerated**: log and move on, uncommitted; the new partition owner reprocesses the batch, harmlessly, via the same idempotent audit upsert |

The dividing line for the first two is literal: message-level work sits
inside `process_message()`'s try block; everything after it in the loop is
infrastructure and is allowed to propagate. The third is narrower still — it
wraps only the `consumer.commit()` call, and only that exact, enumerated set
of codes. Deliberately **not** tolerated: `_MAX_POLL_EXCEEDED` means *we* were
too slow and should be loud, not silenced; `UNKNOWN_TOPIC_OR_PART` is a
deployment error; auth and coordinator failures are genuine infrastructure. A
blanket `except KafkaException` would convert all of those into silent loss —
exactly the failure mode this taxonomy exists to prevent.

**Batching widens the replay window from 1 message to N.** Offsets are now
committed once per batch (up to `--batch-size` messages, or whenever
`--batch-timeout-ms` expires) instead of once per message. A crash between
processing a batch and committing it therefore replays the whole batch on
restart — still at-least-once, still safe because the audit upserts, but the
amount of repeated work on failure now scales with batch size. This is the
honest price of the throughput win.

Error entries from `consume()` (broker notices — rebalance, EOF) are filtered
out before offsets are ever computed from a batch: they carry a topic,
partition and offset but no payload, and letting one through would silently
advance the commit position past a record that was never processed — the one
failure mode in the whole pipeline that loses data permanently rather than
just repeating work.

**Audit before emit.** The audit row is written *before* the message is
produced downstream, so no record is ever emitted that has not already been
recorded. A Postgres outage halts the pipeline rather than letting unlogged
data through — the correct posture for a governance tool.

**Audit trail stores no PII, but does store why.** `findings` records entity
type, field, span, tier and confidence — never the matched value.
`messages_processed.failure_class` / `failure_detail` explain fail-closed
quarantines, so "why was this quarantined" is answerable from SQL alone.

**The topics are a different story, deliberately.** That property belongs to the
audit database, not the system. `txn.raw` carries unredacted input by
definition, and `txn.quarantine` carries the *original* bytes on purpose — a
reviewer has to see what actually arrived. So the highest-risk store in the
system is the one nobody thinks of as a store. Retention is therefore set as a
data-protection control rather than a capacity one, in `scripts/create_topics.py`:

| topic | contents | retention |
|---|---|---|
| `txn.raw` | unredacted input | **24h** — enough to replay a day-long outage |
| `txn.clean` | redacted | 168h |
| `txn.quarantine` | **unredacted originals** | **72h** — this value *is* the reviewer SLA |

Shortening quarantine retention cuts exposure and raises the chance a record
expires unreviewed; that tradeoff is the reason the number is explicit rather
than inherited. The compose stack runs PLAINTEXT with no ACLs, which is normal
for local development and **is not a production posture** — transport security
and authorization are deployment concerns this repo does not configure.

**Quarantine policy.** Confident detections are redacted in place and
forwarded; only *uncertain* records (sub-threshold confidence, malformed
messages) are quarantined — mirroring how production DLP systems avoid
blocking the happy path.

The decisions above are the highlights; for the reasoning behind every
non-obvious choice in the project — including provisional values still
pending measurement and open questions not yet decided — see
[docs/decisions.md](docs/decisions.md).

## Tests

```bash
pip install -e ".[dev]"
pytest                      # no Kafka or Postgres needed
pytest -m integration       # the report's SQL, against a real Postgres
pytest --cov=pipelineguard  # coverage report
```

The detection and routing logic is written as pure functions — `RulesDetector.detect`,
`processor.process_message`, `processor.redact` — specifically so it can be tested
without infrastructure. Kafka messages are stubbed; Postgres is replaced by a fake
connection that records the SQL it was handed, which is how the audit tests assert
that **no payload value ever reaches the database**.

Coverage is 100% on the detection, routing, audit and generator modules, and
99% on `processor.py` — including its `main()` loop, tested by patching
`Consumer`/`Producer`/`AuditWriter` on the module with in-memory fakes rather
than running against a real broker. That proves the wiring (batching, error
filtering, commit ordering, rebalance tolerance); it is not a substitute for
integration testing against a live stack, which this suite doesn't attempt.
`producer.py`'s loop is untested for the same broker-bound reason — that gap
is real and not yet filled.

**The governance report is the one place that reasoning doesn't hold.** Its
logic *is* its SQL — the `GROUP BY`s, the window boundaries, the join that
distinguishes a quarantined record with no findings from one with several.
Handing canned rows to a fake connection would have tested the renderer
formatting its own fixtures while every query stayed unexercised and the suite
went green. So the report is split: rendering and classification are pure
functions tested offline, and the queries are tested against a real Postgres
in `tests/integration/`, skipped automatically when no database is reachable.
Each of those tests targets a specific way a query could be wrong and still
look right, and all six mutations tried against them — inclusive upper bound,
inner join for the uncertain queue, `avg` for `min` confidence, non-`DISTINCT`
record counts, a queue total collapsed to its capped listing, and silently
dropped unclassified entity types — were caught by exactly the intended test.

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

Batch size sweep, 20,000 synthetic transactions per run. Every run reprocesses
the same messages from offset 0 under a fresh consumer group.

| `--batch-size` | throughput | per-record | detection p50 / p95 / p99 |
|---:|---:|---:|---:|
| 1 | 8 /s | 125 ms | 0.28 / 0.50 / 0.64 ms |
| 8 | 57 /s | 17.5 ms | 0.14 / 0.35 / 0.50 ms |
| 32 | 291 /s | 3.44 ms | 0.10 / 0.19 / 0.26 ms |
| 64 | 368 /s | 2.72 ms | 0.10 / 0.18 / 0.26 ms |
| 128 | 892 /s | 1.12 ms | 0.10 / 0.15 / 0.22 ms |
| 256 | 898 /s | 1.11 ms | 0.11 / 0.18 / 0.23 ms |
| **512** | **1,267 /s** | **0.79 ms** | 0.10 / 0.16 / 0.21 ms |
| 1024 | 1,361 /s | 0.735 ms | 0.10 / 0.13 / 0.19 ms |
| 2048 | 1,450 /s | 0.69 ms | 0.11 / 0.15 / 0.20 ms |

**Batching is worth ~180× on this workload** — 8 msg/s committing per message,
1,450 msg/s at a 2048-message batch. That is the entire justification for the
added complexity, and it comes from amortising three fixed costs (a Postgres
WAL fsync, a producer flush waiting on broker acknowledgement, and an offset
commit round trip) across N records instead of paying them per record.

**512 is the default because the curve flattens there.** It reaches 87% of the
best observed throughput; 1024 buys 7 more points for double the replay window
and 2048 another 6 for quadruple. Since a failed batch replays in full, batch
size is bought with replay cost, and the marginal return collapses past 512.

**Detection is not the bottleneck, and that is the interesting result.** p50
detection latency sat at 0.10–0.11 ms in every configuration across both
sweeps — nine runs, two orderings — while per-record cost at the best batch
size is 0.69 ms. **Roughly 85% of per-record time is spent somewhere other
than detection**: producing, auditing, and moving bytes across the broker and
database boundaries. Tier 1's rules are effectively free relative to the
plumbing around them, which is worth knowing before Tier 2 adds ~20 ms of
model inference to that budget.

### Methodology and caveats

Hardware: Intel i7-11800H (8 cores / 16 threads), 31.7 GB RAM, Windows +
Docker Desktop (WSL2 backend, 16 CPUs / 15.5 GB allocated). **Kafka, Postgres
and the processor all ran on the same laptop**, so these are relative
comparisons between configurations, not absolute capacity claims — a
dedicated broker over a real network would look different in both directions.

Message shape: 9-field synthetic bank transaction, ~500 bytes, averaging 5.4
detected entities each (2 IBAN, 1.2 phone, 1.1 email, 1.1 CNIC). The producer
ran unthrottled (`--rate 0`) so that batch size, not arrival rate, was the
binding constraint — throttling below `batch_size / batch_timeout` would close
batches on the clock and flatten the curve for the wrong reason.

Runs used `--exit-after 20000` for a deterministic stopping point; timing a
long-running consumer by hand biases the rate downward by however long it
idles after draining. The audit tables were truncated between runs.

**The latency columns measure detection only.** The timer starts immediately
before `process_message` and stops after it, so consume, batch linger, audit,
produce and flush are all excluded. End-to-end latency is not yet measured.

**Low-batch-size numbers are not stable on this hardware.** The sweep was run
twice, forward and in reverse. Results at 256 and above agreed within 4%, but
`--batch-size 128` measured 307 /s in the forward sweep and 892 /s in reverse —
2.9× apart. In the forward run its throughput degraded monotonically within
the run (586 → 431 → 327 → … → 174 /s); in reverse it held steady at ~1,050 /s.
The forward sweep had spent ~47 minutes under sustained load before reaching
that point, the reverse sweep 70 seconds.

Two hypotheses were tested by re-running the sweep in reverse order. Index
growth in `findings` was ruled out — the table grows identically within every
run, including the reverse one that showed no degradation. The remaining
explanation is the measurement environment: a laptop running all three
components, where sustained load degrades over tens of minutes. The reverse
sweep is reported above because it completed in 3.6 minutes with flat interval
rates throughout. The anomaly is documented rather than smoothed away, and it
does not affect the design conclusion.

Correctness held across every run: 19,227 redacted and 773 quarantined of
20,000 (3.87% quarantine rate, against 3.96% predicted from the generator's
2%-per-IBAN corruption and two IBANs per message), zero fail-closed failures,
and exactly 40,000 IBAN detections — precisely two per message.

## Project layout

```
Dockerfile                       one image, four entry points (processor, topics, producer, report)
docker-compose.yml               broker, database, topic init, processor, on-demand tools
db/init.sql                      audit schema (idempotent upserts by message_id)
scripts/create_topics.py         explicit topic creation + retention (auto-create disabled)
scripts/peek_topic.py            print recent messages on a topic (reads from the END)
scripts/probe_ner_*.py           the Tier 2 measurement suite — model, threshold,
                                 runtime, precision, redaction damage
src/pipelineguard/
  config.py                      env-driven settings
  models.py                      Envelope, Finding, Tier
  generator/transactions.py      synthetic PK-locale transaction generator
  producer.py                    rate-controlled producer with delivery callbacks
  detectors/base.py              Detector protocol
  detectors/tier1_rules.py       Tier 1 rules engine
  detectors/schema_rules.py      declared-PII fields + the free-text registry
  detectors/tier2_encoder.py     Tier 2 encoder, batched, GPU-optional
  processor.py                   the stream processor (consume→detect→route→audit)
  audit.py                       idempotent Postgres audit writer
  compliance.py                  entity type → regulatory classification
  report.py                      governance report (SQL over the audit trail → Markdown)
  observability.py               console logging + rolling throughput/latency stats
docs/sample-report.md            a generated report, committed as an example
```
