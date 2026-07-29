# PipelineGuard — Handoff Brief

Written 2026-07-29 for a fresh session. Factual state of the project: what
exists, what has been measured, what is decided, what is open. Proposals are
labelled as proposals; nothing here should be read as settled unless it says
so.

Repo: `github.com/AffanBasra/PipelineGuard` (private). Last updated
2026-07-29 on branch `governance-report`, which is ahead of `master` by the
corpus survey and the governance report. 202 tests passing.

---

## 1. What the project is

A streaming PII / data-quality firewall for Kafka, with a Pakistani-locale
focus. Records enter a raw topic; a stream processor detects PII, redacts it
in place, routes uncertain or malformed records to a quarantine topic, and
writes an audit trail to Postgres from which a governance report will be
generated.

Built by Affan Ahmad Basra as a portfolio project. Target audiences:
Pakistani product companies working in data governance (Securiti AI in
particular) and remote data-engineering roles. Positioning is **data
engineering** — pipelines, delivery semantics, operations — rather than ML.
Available effort is roughly 5–10 hours/week.

Standing constraint agreed from the outset: **only interview-defensible
claims.** No invented metrics, no overselling, and READMEs state honest scope.

---

## 2. What exists and works

~1,400 lines of source, ~1,800 lines of tests, 202 tests. No broker or
database is required for 190 of them; the 12 integration tests skip when no
Postgres is reachable.

**Infrastructure.** `docker-compose.yml` runs Kafka 3.8 (KRaft, no ZooKeeper)
and Postgres 16. Topics are created explicitly by `scripts/create_topics.py`
because broker auto-create is disabled. Postgres is published on host port
**5433** (5432 is commonly occupied by a native install). Kafka currently has
no named volume, so its log does not survive `docker compose down`.

**Synthetic stream.** `generator/transactions.py` produces Pakistani bank
transactions — CNIC, PK IBAN (with valid ISO 7064 mod-97 check digits), PK
mobile numbers, Roman-Urdu names, plus a free-text memo field. ~2% of IBANs
are corrupted deliberately so the quarantine path is exercised.
`producer.py` is a rate-controlled producer with delivery callbacks and a
progress bar.

**Tier 1 detection.** `detectors/tier1_rules.py` detects four entity types —
CNIC (with province-digit validation), PK IBAN (mod-97), PK phone (format
normalisation), and email. Findings carry entity type, field, character span,
tier and confidence. Detections that match structurally but fail their
checksum are emitted at reduced confidence (0.5) rather than dropped, which
routes them to quarantine.

**Processor.** `processor.py` consumes in micro-batches, runs Tier 1, redacts
spans right-to-left, routes to clean or quarantine, writes a batched audit
transaction, produces, flushes once, confirms every delivery, then commits the
highest offset per partition. Rebalance-related commit rejections are
tolerated; everything else propagates.

**Audit.** `audit.py` upserts on `message_id` and delete-reinserts findings,
so redelivery overwrites rather than duplicates. The audit trail records
entity type, field, span, tier and confidence — **never the matched value.**
Fail-closed quarantines additionally record `failure_class` and
`failure_detail`.

**Observability.** `observability.py` provides console logging and a periodic
stats line reporting throughput and detection-latency percentiles over a
bounded rolling window.

**Governance report.** `report.py` renders the audit trail as Markdown for a
compliance reviewer — scope of scan, disposition, tiers, personal data
observed with its regulatory basis, where it was found, items requiring
review, failures, system properties, limitations. It reads the audit trail and
nothing else, so it inherits the property that no PII value can appear in it.
Quarantine is split into two worklists (fail-closed = engineering, uncertain =
human decision). `compliance.py` maps entity types to data categories and the
regime that makes them matter. A generated example is committed at
`docs/sample-report.md`.

Its SQL is tested against a real Postgres in `tests/integration/`, skipped
when none is reachable — the first integration tests in the project, added
because the report's logic *is* its SQL and a fake connection would have
tested the renderer against its own fixtures.

---

## 3. What has been measured

**Batch-size sweep**, 20,000 synthetic messages per run, fresh consumer group
each run, audit truncated between runs, producer unthrottled.

| `--batch-size` | throughput | per-record | detection p50 |
|---:|---:|---:|---:|
| 1 | 8 /s | 125 ms | 0.28 ms |
| 8 | 57 /s | 17.5 ms | 0.14 ms |
| 32 | 291 /s | 3.44 ms | 0.10 ms |
| 64 | 368 /s | 2.72 ms | 0.10 ms |
| 128 | 892 /s | 1.12 ms | 0.10 ms |
| 256 | 898 /s | 1.11 ms | 0.11 ms |
| 512 | 1,267 /s | 0.79 ms | 0.10 ms |
| 1024 | 1,361 /s | 0.735 ms | 0.10 ms |
| 2048 | 1,450 /s | 0.69 ms | 0.11 ms |

Batching is worth roughly **180×** on this workload. The default is 512, at
87% of best observed throughput, chosen because the marginal gain past that
point stops justifying the widened replay window.

Detection latency held at **0.10–0.11 ms across all nine runs and both sweep
orderings**, against a per-record cost of 0.69 ms at the best batch size — so
roughly **85% of per-record time is not detection**. It is produce, audit, and
broker/database round trips.

Correctness held throughout: 19,227 redacted and 773 quarantined of 20,000
(3.87% quarantine rate against 3.96% predicted), zero fail-closed failures,
exactly 40,000 IBAN detections (two per message).

**Known measurement caveat.** The sweep was run twice, forward and reverse.
Results at N ≥ 256 agreed within 4%, but N=128 measured 307 /s forward and
892 /s reverse. The forward run degraded monotonically within itself; the
reverse run held flat. Index growth in `findings` was eliminated as a cause
(the table grows identically in both). Attributed to the measurement
environment — a laptop running broker, database and processor together, where
sustained load degrades over tens of minutes. Documented in the README rather
than smoothed away.

Hardware: i7-11800H (8C/16T), 31.7 GB RAM, Windows + Docker Desktop (WSL2, 16
CPUs / 15.5 GB allocated). All three components on one machine, so these are
relative comparisons between configurations, not absolute capacity claims.

---

## 4. Design decisions

`docs/decisions.md` records every non-obvious choice with its reasoning,
marked settled / provisional / open. The load-bearing ones:

- **At-least-once with idempotent audit writes**, not exactly-once, because
  outputs cross into Postgres which Kafka cannot roll back.
- **Audit before emit** — no record is emitted that has not already been
  recorded. A Postgres outage halts the pipeline rather than letting unlogged
  data through.
- **Three failure classes**: message-level failures fail closed to quarantine;
  infrastructure failures crash and replay; routine coordination events
  (rebalance) are tolerated and logged. Conflating any two produces either an
  infinite crash loop or silent data loss.
- **Every value crossing from an untrusted message into a typed column is
  validated and normalised at the boundary** — `message_id` canonicalised via
  `uuid.UUID`, `event_ts` `Z`-suffix normalised, `schema_version` int-but-not-
  bool.
- **Detection logic is pure functions**, which is why the test suite needs no
  infrastructure.
- **Tier assignment by cost, not capability** — rules for anything with a
  checkable format, encoder for contextual entities, LLM only for ambiguous
  spans.

---

## 5. Testing practice

Stubs and fakes, no mocks. `main()` is tested by patching
`Consumer`/`Producer`/`AuditWriter` on the module with in-memory fakes that
share one call log, so ordering (audit → emit → commit) can be asserted. Fakes
reproduce the real failure mode rather than a convenient proxy — `ErrorMessage`
carries a real topic/partition/offset specifically so that omitting the error
filter silently advances the offset rather than crashing.

**Mutation testing has been applied to every safety-critical path**:
deliberately break the code, confirm exactly the intended test fails, revert.
This has been done for the `+1` in offset commits, max-vs-last offset
selection, batched-vs-looped audit writes, transaction rollback, the
positional-zip bug, delivery-tracker coverage, commit ordering, error-entry
filtering, and the rebalance predicate. Coverage is 99% on `processor.py`,
100% on the pure modules; `producer.py`'s loop is untested because it is
broker-bound.

---

## 6. In flight: evaluation against a public corpus

**Motivation.** The project currently has *no* detection-quality numbers —
precision and recall have never been measured against labelled ground truth.
This is the most visible gap.

**Chosen corpus: `nvidia/Nemotron-PII`.** CC BY 4.0 (commercial use
explicit), 100k synthetic persona-grounded records, span-level annotations,
55+ PII types, structured and unstructured documents, US and international
locales. Paired with `nvidia/gliner-PII`, a model fine-tuned on it.

**Facts established about the data** (verified directly against the file):

- `spans` is a **Python repr string, not JSON** — `json.loads` fails,
  `ast.literal_eval` works, costs ~95 µs/row to parse.
- Span format is `{start, end, text, label}` with **character offsets**.
- **Offsets are correct; the `text` field is not verbatim.** Of 170,531 spans
  checked: 98.99% exact, 1.01% differ only by case, 0% otherwise. The case
  differences concentrate in categorical attributes (`race_ethnicity`,
  `gender`, `political_view`) where the annotation stores the canonical
  category rather than the surface form. **Use `start`/`end`; do not use
  `text` to locate spans.**
- Corpus splits evenly on both relevant axes: locale `us` 50,000 / `intl`
  50,000; format `unstructured` 50,578 / `structured` 49,422. Median length
  969 chars structured, 601 unstructured. Largest domains are Banking,
  Transaction Services, IT, Brokerage, Credit.
- **Resolved: the file is the complete test split.** `test-00000-of-00001.parquet`
  holds 100,000 rows; the dataset card lists train 100k + test 100k = 200k. The
  earlier "50k/50k" note was wrong.
- **Resolved: `us` and `intl` are the same documents rendered twice.** 49,987
  distinct `document_description` values, all present in both locales, Jaccard
  1.000; `document_format` and `domain` counts identical across locales. So this
  is 50,000 documents in a matched-pairs design, not 100,000 independent ones —
  which both caps the effective sample size and hands over a controlled
  locale comparison for free.
- **Measured: the corpus's identifiers largely fail their own checksums.**
  Credit cards pass Luhn 11.9% (1,528/12,867) and routing numbers pass ABA
  10.1% (843/8,354) — both at chance. BIC is 76.4% structurally valid. Cards
  and routing numbers are random digits with correct prefixes and lengths.
  This is the same defect this project fixed in its own generator, shipped in
  a public NVIDIA dataset, and it forces detection and validation to be
  reported as two separate numbers. See `docs/decisions.md` §11.

**Entity distribution** (850,340 mentions across 55 types, from the file as
downloaded):

| band | mentions | share |
|---|---|---|
| contextual (names, orgs, addresses, occupations) | 345,299 | 40.6% |
| strong-format (email, url, phone, IP, dates, SSN) | 294,829 | 34.7% |
| opaque IDs (customer_id, account_number, MRN) | 112,905 | 13.3% |
| sensitive attributes (race, religion, health, gender) | 65,966 | 7.8% |
| checksum-validatable (card, routing, BIC, VIN) | 31,341 | 3.7% |

The distribution is flat — top 5 types cover 38%, top 10 cover 56%, top 20
cover 75%. `first_name` + `last_name` alone are 16.9%.

**Current measurable coverage: `email` only, 53,930 of 850,340 mentions =
6.3%.** Verified by running the detector: the PK phone pattern requires a
`+92`/`03` prefix so US numbers score zero, and the corpus contains no IBAN or
CNIC types at all (its financial identifiers are `swift_bic`,
`bank_routing_number`, `account_number`).

---

## 7. Proposed plan for the evaluation work

Labelled as proposal — none of this is built.

**Phase 0 — measurement substrate.** Verify which split the parquet actually
is. Write a loader producing normalised `(text, locale, document_format,
domain, [(start, end, label)])` records with a parse cache. Write a span
matcher supporting two policies: exact-boundary match (comparable to published
NER results) and coverage match (was every character of the gold span
redacted). Per-type TP/FP/FN. Test the matcher itself under mutation, since an
off-by-one silently halves every downstream number. Write an explicit label
mapping from Nemotron types to PipelineGuard entity types. Produce the first
real number: recall and precision for `email`.

**Phase 1 — locale packs.** `RulesDetector` currently hardcodes Pakistani
patterns. Split into a `generic` pack (email, url, ipv4/6, mac, SSN, credit
card with Luhn, routing number with ABA checksum, SWIFT/BIC) and a `pk` pack
(CNIC, PK IBAN, PK phone), composed at construction. Raises attempted coverage
from 6.3% to roughly 20%.

**Phase 2 — Presidio baseline.** Run Microsoft Presidio over the same corpus
with the same matcher on the same machine, per entity type. Presidio is the
open-source default in this space and a rules engine like Tier 1, so it is the
apples-to-apples comparison. Decision recorded in advance: **publish the result
whichever way it goes.**

**Phase 3 — Tier 2 via GLiNER-PII**, off-the-shelf and micro-batched into the
existing loop. Measure recall, latency and escalation rate together.

**Phase 4 — Pakistani evaluation set.** No public corpus covers CNIC, PK IBAN
or Roman-Urdu names. A few hundred hand-built records with names sourced
independently (e.g. WikiANN-ur or a public name list rather than written by
the author), disjoint between generation and evaluation, labelled honestly as
self-constructed.

**The escalation-rate measurement**, which is the point of the tiering claim:
escalate a field to Tier 2 unless Tier 1's validated findings cover the entire
field content. Measure in **bytes**, not fields, since encoder cost scales with
sequence length. Run three configurations over the same corpus — Tier 2 on
everything (ceiling recall, worst cost), Tier 1 only (floor recall, best
cost), and tiered — then report recall retained versus model-only, latency
saved, and the escalation rate that explains it. Run separately on structured
and unstructured records, because the contrast is the finding.

---

## 8. Strategic context for the evaluation work

Recorded because it shaped the plan, and because it constrains what can
honestly be claimed.

`gliner-PII` was fine-tuned **on** Nemotron-PII, and NVIDIA report 92% recall
/ 64% F1 for it (implying ~49% precision — it over-detects roughly two-to-one,
which is the correct bias for redaction). Evaluating that model on that corpus
is evaluating it on its own training distribution. **If Tier 2 is GLiNER-PII,
the detection-quality ceiling on this corpus is NVIDIA's number, not an
improvement on it.**

The conclusion drawn: the project is unlikely to win on detection accuracy —
not against a purpose-built model on its home corpus, not on saturated
academic datasets, and not on self-generated data. The axis where nothing is
published is **streaming throughput, escalation rate and cost per record**.
That is where the measured 1,450 msg/s pipeline and the tiering architecture
constitute an original claim.

For context, an April 2026 preprint ([PIIBench, arXiv 2604.15776](https://arxiv.org/abs/2604.15776))
consolidated ten corpora into 48 canonical PII types and found all eight
evaluated systems below span-level F1 0.14, with Presidio best at 0.1385.
Single author, v1, unreplicated — cite as one study, not as settled fact, and
note that scoring systems on entity types they never claimed to detect may
account for part of the result.

---

## 9. What is left

**Toward MVP** (defined as: runs at volume under `docker compose up`, a real
benchmark, a governance report, and basic Tier 2 encoder support; then the
repo goes public):

1. **Dockerfile + processor service in compose.** Currently `docker compose up`
   starts infrastructure only; the processor runs on the host. The documented
   quick start is the first thing a reviewer tries. Scoped but not built — the
   main gotcha is that the containerised processor must use `kafka:19092` and
   `postgres:5432` (internal listeners), not the host-facing `localhost:9092`
   and `5433`. A topic-creation init container should gate the processor via
   `service_completed_successfully`, and `restart: unless-stopped` is what
   makes crash-and-replay real. `docker compose up --scale processor=3` then
   gives the multi-consumer demo in one command.
2. ~~**Governance report.**~~ **Built** — `report.py` + `compliance.py`, 45
   new tests, sample at `docs/sample-report.md`. Remaining: it is not yet
   reachable from `docker compose`, and the Pakistani statutory references in
   `compliance.py` are deliberately general and need verifying against primary
   sources before the repo goes public.
3. **Tier 2, off-the-shelf encoder**, honestly labelled as not locale-tuned.
   The locale fine-tune is post-MVP.
4. The evaluation work in §7.

**Post-MVP:** Tier 3 pluggable LLM escalation (Gemini first, then Ollama),
support-chat free-text topic, Airflow scheduled batch-scan mode, and a
conditional Go rewrite of one consumer.

---

## 10. Open questions

- ~~Which split the downloaded parquet actually is~~ — **closed**: the complete
  test split, 100,000 rows (§6).
- ~~Whether the `intl` half of Nemotron-PII contains IBAN-shaped or non-US phone
  data that would partly serve the locale evaluation~~ — **closed, negative**.
  Zero PK-IBAN shapes in 100,000 documents, no IBAN label in the taxonomy at
  all, `+92` absent from the top 20 dialling codes, Pakistan absent from the
  top 25 `country` surface forms. The one intl-only label, `national_id` (2,847
  mentions), mixes unrelated national formats with no country attribute on the
  span, so it is not a rules-detectable entity. **The hand-built Pakistani
  evaluation set in Phase 4 remains necessary.** See `docs/decisions.md` §11.
- Whether a checksum failure should still route to quarantine, now that
  detection and validation are separate measurements — this conflicts with the
  generator's deliberate 2% IBAN corruption, which exists to exercise the
  quarantine path. See §7.
- End-to-end latency is unmeasured. All published latency figures are
  detection-only — the timer starts immediately before `process_message` and
  excludes consume, linger, audit, produce and flush. Labelled as such in the
  README; a second measurement from `event_ts` remains to be added.
- Integration tests against a live stack: **partly closed.** The report's SQL
  is now tested against a real Postgres. Kafka remains untested against a live
  broker — `processor.main()` is covered by fakes, which proves wiring but not
  broker behaviour.
- Kafka has no named volume, so its log does not survive `docker compose down`.
  Convenient during benchmarking, incoherent for a system whose durability
  argument rests on that log.
- Topic retention is unconfigured; quarantine plausibly warrants longer
  retention than clean, for compliance.
- Quarantine is terminal — no reviewer workflow, and no distinction between a
  record that failed once and one that fails permanently.
- `schema_version` is validated and stored but nothing branches on it. Recorded
  as a deliberate forward-compatibility hook.
- Sensitive-attribute PII (GDPR Article 9 categories — race, religion,
  political view, sexuality, health) is 7.8% of the corpus and is format-free.
  Out of scope, but the reason should be stated rather than left implicit.

---

## 11. Working practice

`CLAUDE.md` in the repo root is gitignored and local-only. Its rules: ask
rather than assume; simplest working solution first; do not touch unrelated
code; flag uncertainty explicitly; **test every relevant change, and confirm
the new test actually fails against the pre-change code**; never add Claude to
commit history or contributor lists.

The pattern that has worked: agree the design in discussion, split the work so
the interesting parts are written by hand and the repetitive parts are
delegated, then review with mutation testing rather than trusting a green
suite. Branch per unit of work, PR into `master` (branch protection is on).
