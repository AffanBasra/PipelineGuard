# Design decisions

Every non-obvious choice in PipelineGuard, with the reasoning. Written so that
"why is it like this?" is answerable without archaeology, and so that a change
that contradicts one of these is visibly a change rather than a drift.

Status: **settled** = built and tested. **provisional** = built, but the value
is a placeholder pending measurement. **open** = not yet decided.

---

## 1. Scope and product

**Streaming PII firewall, not a batch scanner.** *(settled)*
Kafka in, tiered detection, redacted output plus a quarantine lane, and a
Postgres audit trail the governance report is generated from. The streaming
shape is the point: batch PII scanning is a solved, unremarkable problem.

**Synthetic input, stated plainly.** *(settled)*
No real personal data is generated, processed or stored, and the README says
so rather than implying otherwise. Throughput benchmarking needs volume that
no real corpus can ethically supply, and a privacy tool trained on leaked data
is self-refuting.

**Public repo, never a hosted service accepting uploads.** *(settled)*
Accepting third-party documents would make this a data controller with
retention, breach-notification and PECA/GDPR obligations attached to a student
side project. A public repo is also the artifact a reviewer can actually read.

**A local inspection UI, not a metrics dashboard, and not deployed.**
*(settled)*
`src/pipelineguard/ui.py` is a Streamlit app with three tabs: scan one memo,
scan an uploaded file, and render the governance report from the audit trail.
It exists because the detectors' behaviour was previously only reachable
through a CLI or a SQL query, and the interesting cases — a bridged Karachi
plot number, a city trailing an address, an Urdu term read as a name — are
positional facts about a span that a table of aggregate scores cannot show.

Two earlier decisions constrain it, and both are honoured rather than
sidestepped:

* *Never a hosted service accepting uploads.* The file uploader is why this
  matters. `.streamlit/config.toml` sets `server.address = "localhost"`, so the
  app is not reachable off the machine even by accident; without it Streamlit
  binds every interface and prints a LAN URL. The rule is enforced by
  configuration, not by convention. **Superseded for a separate demo build --
  see "A public demo build, and what it actually costs" below.** The default
  build is unchanged and still binds loopback.
* *A metrics dashboard demonstrates plumbing rather than domain understanding.*
  So the UI invents no charts and no counters. Tabs 1 and 2 show spans, tiers
  and confidences — the detectors' actual output. Tab 3 renders
  `report.render()` unchanged. Every number on screen is one the pipeline
  already computes.

The UI holds no logic. `scan.py` and `batch_report.py` carry everything
assertable and are covered by the suite; `ui.py` is `st.*` calls. It composes
the shipped `RulesDetector`, `Tier2Detector` and `processor.redact()` rather
than reimplementing any of them, because a second copy of span arithmetic would
drift and the UI would then show a redaction the pipeline does not perform.

**Sections are a keyed nav, not `st.tabs`.** *(settled)*
`st.tabs` keeps its selection client-side and springs back to the first tab
when a widget triggers a rerun. Pressing "Load report" therefore built the
report correctly and then returned the reader to the playground, so the result
was never seen -- a bug that looked like the query silently failing. Any keyed
control stores the selection in session state and survives the rerun; this
started as `st.segmented_control` and is now `streamlit-option-menu`, which
carries icons and an explicit active style. The keying is the load-bearing
part, not the widget. The same reasoning applies to the results themselves: a
button is `True` for exactly one run, so anything rendered inside its branch
disappears on the next one. Outcomes are written to session state and rendered
outside the branch, errors included.

Slow work shows a centre-screen spinner that can be dismissed, leaving a corner
pill until it finishes. This is not decoration: `psycopg.connect` against a
stopped database waits out its default timeout, which is ten seconds of a UI
that looks broken. The connect timeout is now explicit, and the failure is
reported with its cause and a pointer to `docker compose`.

**A public demo build, and what it actually costs.** *(settled)*
This supersedes "it must not be deployed" for one build, and the honest version
of the reasoning matters more than the outcome.

The original rule was written against *uploads*: accepting third-party
documents brings retention and breach-notification duties. Two things about
that turned out to be true and one turned out to be overstated.

True: the app stores nothing. Streamlit hands an upload over as bytes in
memory, the UI writes nothing to disk, and it writes nothing to the audit
database. `ScanResult.without_text()` now drops the scanned rows from session
state once the figures are computed, so "gone when the scan finishes" is a
property rather than a promise.

True: the guarantee stops being ours at the container boundary. The demo runs
on infrastructure we do not own, on a free tier with no data processing
agreement. If a visitor uploads something real and it reaches a platform log,
our no-storage claim was true of our code and false of the system delivering
it. That risk is accepted, not solved.

Overstated: "would make this a data controller with retention obligations". No
storage removes the retention and most of the breach exposure, but not
controller status -- processing under GDPR Art. 4(2) includes consultation and
use. What survives is small: a lawful basis, a privacy notice, and being
contactable. All three are now in the app. Separately, GDPR's territorial scope
(Art. 3) reaches a Pakistan-based operator only when offering services to
people in the Union or monitoring them, which a demo aimed at neither does; the
instruments that actually apply are the ones `compliance.py` already cites.

The demo is a *different build*, not the same one exposed. `PG_DEMO=1` caps the
batch at 100 rows, removes the threshold and widener controls, swaps the live
governance tab for a stored run, and shows the privacy notice. The two removed
controls are not cosmetic: `tier2_settings` takes a global lock only when a
setting is overridden, so leaving them alone is what stops one visitor's scan
queueing behind another's.

Rejected: keeping the uploader but relying on a "do not upload real PII"
banner alone. A disclaimer is a wish, not a control. It is still shown, because
saying so is better than not, but the retention properties above are what the
decision rests on.

**The encoder never reads what a rule already claimed.** *(settled)*
Tier 2 used to run on the raw value, so it read an email's local part as a name
and its domain as a place. Both sat inside a span Tier 1 already owned, which
made the label read `[ADDRESS+EMAIL+PERSON_NAME]`, inflated the audit's entity
counts, and -- once `bridge_address_spans` used a mail domain as an anchor --
masked 59 characters of ordinary text. `shield()` blanks rule-claimed
characters before the encoder sees them, length-preserving so offsets still
index the original; `combine()` drops encoder findings that sit entirely inside
a rule span. Containment must be total: a partial overlap is kept, because
dropping it would leave its outer part in the clear. Applied at both join
points, so the UI cannot disagree with the pipeline. See findings §26.

**Two governance reports, one dataset.** *(settled)*
`render()` stays the technical artefact the CLI writes and `docs/sample-report.md`
records. `render_summary()` is a second pure view of the same `ReportData` for
a reader who needs the numbers rather than the internals: three pages, every
regulatory passage moved into one appendix, and the review worklist condensed
to a count and a trigger. The message-id table is deliberately absent -- it
identifies records, and this is the version meant to be shareable. Two
renderers rather than one rewritten renderer, because the audiences want
genuinely different documents and the technical one is still the ground truth.

**The batch report is a file scan, and says so.** *(settled)*
Exporting a governance report for an uploaded file reuses `report.render()`,
which is pure. That created one problem worth naming: `render()` hardcoded
`**Source:** PipelineGuard audit trail`, which is false for a file scan.
`ReportData` gained a defaulted `source` field rather than the UI printing a
correction underneath a line that contradicts it. The same reasoning rejected
labelling the export "Audit Cleared" — `compliance.DISCLAIMER` says the report
is not a compliance determination, and a button asserting otherwise would
contradict the document it sits on. It is "Download Redaction Report".

The export carries redacted row text as well as aggregate counts, which the
audit-trail report never does. That is a deliberate widening for a local tool
whose input the operator already holds, and it is bounded: original text and
matched values are never displayed or exported in the batch tab, and a test
asserts a sentinel identifier cannot reach the output. Because ADDRESS
redaction is routinely incomplete rather than binary, the report carries that
caveat next to the rows rather than presenting them as cleared data.

**Pakistani-locale focus.** *(settled)*
CNIC, PK IBAN, PK mobile formats, Roman-Urdu names. No existing tool serves
this, which makes it the memorable differentiator rather than a generic
regex-plus-BERT demo.

**The Pakistani stream is the product; external corpora are instruments.**
*(settled)*
Reaffirmed after the scope of the evaluation work started to widen on its own.
The pipeline, generator, entity types, governance report and compliance framing
are Pakistani. `nvidia/Nemotron-PII` is used for exactly two narrow purposes —
an independent sanity floor for Tier 1 on text this project did not author, and
the escalation-rate measurement on realistic prose — and is described as an
instrument, never as a supported locale.

The rule that keeps this stable: **a dataset earns its place by answering a
question, not by adding coverage.** Broad international entity coverage answers
no question this project asks, and competing on detection breadth puts the
project against Presidio and GLiNER on their own ground, which §11 records as
unwinnable. Consequences: the generic rules pack is capped at the few
high-frequency types the escalation measurement actually needs (email, phone,
card), and TAB is dropped — it was a good answer to "how do we get real data,"
which turned out to be the wrong question.

**Compliance grounding: Pakistani law first, GDPR where it genuinely applies.**
*(settled)*
The governance report is grounded in the Pakistani regime, because a PK bank
pipeline mapped to PK obligations is the distinctive artifact. GDPR is invoked
only where it actually bites rather than decoratively — principally
**cross-border transactions**, where a Pakistani institution processing the
personal data of EU data subjects (inbound remittances, EU-resident account
holders) falls within GDPR's extraterritorial scope.

Framing discipline: the report states what the system **does**, and describes
itself as *designed to support* obligations — never as *compliant with* them.
Compliance is a determination about an organisation, not a property a detector
can assert about itself.

Citations name instruments and their effect rather than section numbers, and
were **cross-checked against independent references on 2026-07-29 with no
inconsistency found**. That is not primary-source verification, and a
section-level citation should not be added without one. Recorded explicitly so
the distinction cannot quietly erode.

The asymmetry between the regimes is stated in the report rather than
smoothed over: Pakistan has no enacted general data protection statute, so
there is no domestic equivalent of GDPR Art. 30 to cite. Padding the Pakistani
column to match the GDPR one would have been the overclaiming this project
exists to avoid.

**The governance report is written for a compliance reviewer, with provenance
that also reads as engineering.** *(settled)*
Its notional reader is someone answering "what personal data flowed through
this pipeline, what was done about it, and what needs a human?" — so it
summarises findings by entity type over a period, lists the quarantine queue
with reasons, and surfaces failure classes. Building it for that reader is what
makes it survive "who would actually use this?"; optimising directly for an
interviewer produces a metrics dashboard, which demonstrates plumbing rather
than domain understanding.

The second audience is served by **provenance**, not by a separate section.
Every report states the window it covers, how many records were scanned, the
throughput achieved, and the breakdown of detections by tier. A compliance
officer needs that to know the scan's coverage — a finding count is meaningless
without knowing what was scanned. An engineer reads the same lines as volume,
lineage and tier economics. One set of facts, legible to both, rather than a
compliance report with a dashboard bolted on.

**A report says which of the two systems produced it.** *(settled)*
`ReportData` carries `source` and `from_stream`. `source` fixed the header line;
`from_stream` fixes the prose. The renderers were written against a Kafka run
and asserted things a file scan does not do — that delivery is at-least-once,
that the review queue sits in `txn.quarantine`, that the audit trail recorded
what happened. All of that is false about a CSV dragged into the UI, and it was
shipping in the batch report before this flag existed.

The compliance passages are kept for a file scan rather than dropped: they
explain why detection behaves as it does, and the scan runs the same detectors.
What changes is that they are introduced as a description of the pipeline, not
of the scan. The distinction is the whole point — a governance document that
overstates its own coverage is worse than none, which its own Limitations
section says.

**The demo's stored governance report is stamped as an example inside the
file.** *(settled)*
The demo has no database, so its governance tab renders a stored run over 5,000
synthetic records. A caption on the page is not enough: the download leaves the
browser and is read later, out of context, by someone who never saw the caption.
`report.stamp_example()` marks the title, the source line and adds a banner
above the first figure. The session-scoped artefact — the one that *is* about
the visitor's own upload — is the Batch scan tab, which now offers the same
executive summary over their file.

---

## 2. Data generation

**Faker plus hand-built locale generators.** *(settled)*
`generator/transactions.py`. Faker covers generic fields; CNIC, IBAN, phone
and names are hand-built because Faker has no Pakistani locale for them.

**IBANs carry real mod-97 check digits.** *(settled)*
Originally random, which meant ~99.5% of generated IBANs failed the detector's
checksum and the entire stream quarantined. Check digits are now computed.

**~2% of IBANs are corrupted on purpose.** *(settled)*
`CORRUPT_IBAN_RATE`. Without it the quarantine path never fires on a synthetic
stream and the routing logic is never exercised end to end. Two IBANs per
transaction means ~4% of messages quarantine.

**CNICs use valid province digits (1–7).** *(settled)*
So generated identifiers pass the same validation the detector applies —
otherwise the generator manufactures false negatives.

---

## 3. Detection

**Tier 2 starts as an off-the-shelf pretrained NER model, not a fine-tune.**
*(settled)*
The architectural claim this project makes is that tiering is a cost/throughput
tradeoff. Proving it needs a model *in the batched loop* being measured — it
does not need a model trained by us. A pretrained encoder delivers person-name
detection, micro-batched inference, the escalation path and the latency gap
between ~0.4 µs rules and ~20 ms encoder, at a fraction of the cost of the
fine-tune.

The locale fine-tune then becomes an upgrade with a measured before/after,
which is a far stronger claim than an F1 number in isolation — and it needs
the pipeline in place to measure against anyway. Ships labelled honestly as
not locale-tuned.

**Correction (2026-07-29): the before/after is about addresses, not names.**
This entry previously proposed the framing *"off-the-shelf NER missed X% of
Roman-Urdu names; locale-substituted training took it to Y%"*. That premise
was tested and is false. `nvidia/gliner-PII` scores identically on Pakistani
names in English, code-switched and Roman-Urdu sentences — 100% any-hit in
every difficulty band, flat to the percentage point. Only a weak English-only
model degrades.

Addresses are where it breaks: the same model covers 96% of an English-form
address in an English sentence and **51%** of a Roman-Urdu-form address in a
Roman-Urdu sentence, leaving the house number and street in the clear while
finding the city. Both variables contribute independently. Full numbers and
method in [tier2-detection-findings.md](tier2-detection-findings.md).

**Tier assignment by cost, not by capability.**
*(mislabelled — corrected below, 2026-08-06)*
Tier 1 rules handle anything with a checkable format (CNIC, IBAN, phone,
email) at ~0.4 µs per pattern. Tier 2 (encoder) is reserved for entities with
no format to match — names, addresses. Tier 3 (LLM) only for ambiguous spans.
Using a 110M-parameter model to find an email address would be four orders of
magnitude slower for a worse answer.

**Tier 2 exists for capability. Cost decides deployment, not existence.**
*(settled — replaces the title above)*
The entry above argues its own opposite: "reserved for entities with **no
format to match**" is a capability claim, and only the last sentence (don't use
an encoder for an email) is about cost. The correct statement is narrower than
the old title and stronger:

- **Tier 2 exists because free text has no format to match.** No rule finds
  `Ayesha Malik` in a memo. This is not an optimisation — it is the only thing
  that does the job at all, and it is the redaction firewall's actual product.
- **Cost decides how it is deployed**, not whether it is needed. 40.5 ms on
  CPU, 31.0 ONNX, 8.03 on GPU (§7, §11 of the findings) set the throughput
  envelope. They cannot argue Tier 2 out of the design, because nothing else
  covers free text.
- **Within the set of tools that CAN do a job, pick the cheapest.** That is all
  the original entry's last sentence ever claimed, and it stays true.

Why the mislabel mattered: it let cost measurements be read as evidence about
whether the architecture was right. §6.3's "65× the budget" was recorded as
though it threatened the tiering claim. It never did — it only ever bounded
throughput. The two questions are separate, and conflating them made a
deployment constraint look like a refutation.

**There is no Tier 1 → Tier 2 escalation.** *(settled)*
Escalation means running something cheap and promoting on uncertainty. Tier 1
has no name rule, so a memo is not a record Tier 1 was uncertain about — it is
a record Tier 1 has no opinion on. Absence of a finding is silence, not low
confidence. So the relationship is **dispatch by field type**, decided by the
schema and known before either detector runs:

    identifier fields (cnic, iban, phone, email)  -> Tier 1 rules
    free text (memo)                              -> Tier 2 encoder
    declared PII (account_holder)                 -> schema rule, no model

Tier 2 → Tier 3 remains genuine escalation: same span, promoted on uncertainty.
This kills the "escalation predicate" as an open design question — there is
nothing to predict, because the schema already says which fields are free text.
It also explains §10.4: pointing Tier 2 at identifier fields masks 50% of their
characters, so dispatch is not merely cheaper than scanning everything, it is
more correct.

**Validate after matching, don't just match.** *(settled)*
CNIC province digit, IBAN ISO 7064 mod-97, phone prefix normalization. This is
what separates a rules engine from `grep`, and it is what makes confidence 1.0
meaningful.

**Checksum-failed hits are reported at reduced confidence, not dropped.**
*(superseded — see §11, "Detection and validation are separate measurements")*
`_UNVALIDATED_CONFIDENCE = 0.5`. Structurally-valid-but-unverified is exactly
the "uncertain" signal the quarantine lane exists for. The specific value is
not load-bearing; anything below 1.0 routes to review.

Still true as a statement about *confidence*. No longer sufficient as a
statement about *measurement*: it makes a checksum failure indistinguishable
from a miss in any recall figure, and the evaluation corpus turns out to fail
checksums at close to chance rate. §11 records what replaced it.

**Regex email matching, not the `email-validator` library.** *(settled)*
Measured: 93.7 µs per `validate_email` call versus 0.42 µs for a shape regex,
on a field present in every message — 220×, on the common path. The library
also raises precision by rejecting unusual-but-real addresses, i.e. it lowers
recall. For a redaction firewall a false positive costs one over-redacted
string; a false negative is a leak. Recall wins.

**Overlapping matches: longest span wins.** *(settled)*
A 13-digit run inside an email local part is part of the email, not a separate
CNIC.

**Spans index the original text.** *(settled)*
Even where a candidate was trimmed of punctuation, `span_start` is advanced by
the amount trimmed. Redaction slices the original string, so any other choice
corrupts output.

---

## 4. Delivery semantics

**At-least-once with idempotent audit writes — not exactly-once.** *(settled)*
Kafka transactions can make the consume→produce→commit leg exactly-once, but
this pipeline's outputs cross into Postgres, which Kafka cannot roll back.
Rather than claim EOS with a caveat, offsets are committed only after the
audit is durable and the produce is confirmed, and the audit upserts on
`message_id` so redelivery is harmless.

**Audit before emit.** *(settled)*
The audit row is written before the message is produced downstream, so no
record is ever emitted that has not already been recorded. A Postgres outage
halts the pipeline rather than letting unlogged data through.

**Delivery is confirmed by two independent checks.** *(settled)*
`flush()`'s return value catches "still queued when the timeout expired"; the
per-message `on_delivery` tracker catches "left the queue by failing". Neither
implies the other — a message can leave the producer queue by failing — and
either means the record is not durable, so neither alone is sufficient.

**One failed delivery blocks the commit for the whole batch.** *(settled)*
Checking only the last tracker would let a partial batch be marked complete.

**Offsets are committed asynchronously.** *(settled)*
Safety comes from idempotent upserts, not from commit synchrony. A synchronous
commit adds a coordinator round trip (~ms) to a per-message budget measured in
microseconds. This is a case where the "safer-looking" option is the wrong one.

**Manual commit, `enable.auto.commit: False`.** *(settled)*
Auto-commit fires on a timer regardless of whether processing succeeded, which
breaks at-least-once outright. Owning the commit is what makes the guarantee
real — and is also why rebalance rejections have to be handled explicitly
(§5), since auto-commit would have swallowed them invisibly.

**Producer idempotence enabled on both producers.** *(settled)*
Prevents duplicates introduced by librdkafka's own internal retries.

---

## 5. Failure taxonomy

Three categories, handled differently. Conflating any two produces either an
infinite crash loop or silent data loss.

**Message-level — fail closed.** *(settled)*
Unparseable bytes, wrong payload shape, invalid envelope fields, a detector
raising. Deterministic: it will fail identically on replay, so crashing would
wedge the pipeline forever on one record. Route to quarantine with
`failure_class`/`failure_detail` recorded, commit, continue. Fail *closed*
means unexpected errors go to quarantine, never to the clean topic — including
our own bugs.

**Infrastructure — crash and replay.** *(settled)*
Broker unreachable, Postgres down, delivery unconfirmed. Transient: likely to
succeed on replay. Don't commit, let the exception kill the process, let a
restart replay from the last committed offset.

**Routine coordination — tolerate and log.** *(settled)*
A consumer-group rebalance landing between `consume()` and `commit()`. Not a
failure at all; it happens on every deploy and scale event. Crashing would make
every scaling operation trigger a restart, which triggers another rebalance.

Tolerated codes, and only these three — all mean "group membership moved under
us", differing only in how far the change has progressed:

| code | name | meaning |
|---|---|---|
| 27 | `REBALANCE_IN_PROGRESS` | the coordinator is reassigning right now |
| 22 | `ILLEGAL_GENERATION` | a rebalance completed; you hold a stale generation |
| 25 | `UNKNOWN_MEMBER_ID` | you were evicted and a new generation formed without you |

Deliberately **not** tolerated: `_MAX_POLL_EXCEEDED` (-147) means *we* were too
slow and should be loud, not silenced; `UNKNOWN_TOPIC_OR_PART` (3) is a
deployment error; auth and coordinator failures are genuine infrastructure. A
blanket `except KafkaException` would convert all of those into silent loss.

**The dividing line is structural, not conventional.** *(settled)*
Message-level work happens inside `process_message`'s try block; everything
after it in the loop is infrastructure and is allowed to propagate.

---

## 6. Audit and storage

**Idempotency is keyed on `message_id`.** *(settled)*
Upsert on the message row; delete-and-reinsert its findings. A redelivered
message overwrites rather than duplicating.

**Poison messages get a deterministic id from `(topic, partition, offset)`.**
*(settled)* Via `uuid5`. A `uuid4` would key the row on *when the code ran*
rather than *which message it was*, so every replay would insert a duplicate —
breaking idempotency precisely in the path most likely to be retried. Offsets
are assigned at append time and never reused, so the triple identifies one
physical record permanently. Coordinates rather than a content hash, because
two byte-identical messages at different offsets are genuinely two records.

**The audit trail stores no PII.** *(settled)*
`findings` records entity type, field, span, tier and confidence — never the
matched value. An audit table accumulating the data the product protects would
be the worst possible bug.

**But it does store why.** *(settled)*
`failure_class` and `failure_detail` on `messages_processed`, so "why was this
quarantined" is answerable from SQL. Without them a fail-closed quarantine is
indistinguishable from a clean message.

**Every value crossing into a typed column is validated and normalized at the
boundary.** *(settled)* `message_id` canonicalized via `uuid.UUID` (Python
accepts `urn:uuid:` and brace forms that Postgres rejects); `event_ts`
`Z`-suffix normalized to `+00:00` (`fromisoformat` only learned `Z` in 3.11,
and the floor is 3.10); `schema_version` must be `int` and explicitly not
`bool` (bool subclasses int in Python, but Postgres won't cast boolean to
smallint). Payload shape was already validated; the envelope's own fields were
not — the half we compute on was guarded, the half we persist was not.

**Quarantine forwards original bytes, unredacted.** *(settled)*
The reviewer must see exactly what arrived.

**`schema_version` is deliberately inert.** *(settled)*
It is validated and persisted but nothing branches on it. It exists as a
forward-compatibility hook: the moment a second payload shape arrives (the
chat-log topic), consumers need a way to tell the shapes apart without
sniffing fields, and adding a version field *after* messages are already in
flight is far harder than carrying one from the start. Rejecting unknown
versions is a deliberate non-goal for now — there is only one version, so the
check would be untestable theatre.

**Redact-and-forward, quarantine only the uncertain.** *(settled)*
Confident detections are redacted in place and forwarded. Only sub-threshold
confidence and malformed messages quarantine. Mirrors how production DLP
avoids blocking the happy path.

**Redaction applies spans right-to-left.** *(settled)*
Each replacement changes string length; left-to-right invalidates every
subsequent span.

---

## 7. Batching and performance

**Micro-batching, bounded by size and time.** *(settled)*
`consume(num_messages=N, timeout=T)`. Size protects under high load (unbounded
batches blow memory and latency); the timeout protects under low load (a quiet
stream would otherwise stall forever waiting for a batch that never fills).
Whichever fires first closes the batch.

**Batch defaults: 500 messages / 1000 ms.** *(settled — measured)*
Swept 1 → 2048 over 20k messages per run, twice (forward and reverse order).
Throughput rises from 8 msg/s at N=1 to 1,450 msg/s at N=2048, but flattens
after ~512: that setting reaches 87% of the best observed rate, while 1024
adds 7 points for double the replay window and 2048 another 6 for quadruple.
Since a failed batch replays in full, batch size is purchased with replay
cost, so the default sits where the marginal return collapses rather than
where throughput peaks.

The two knobs bind under opposite conditions — at 500/1000 ms the timeout only
fires below 500 msg/s, so benchmarking must run the producer unthrottled or
batches close on the clock and the curve flattens for the wrong reason.

Measurement caveat recorded in the README: results at N ≥ 256 reproduced
within 4% across both sweeps, but N=128 differed by 2.9× between them, tracking
elapsed time under load rather than batch size. Attributed to the measurement
environment (laptop running broker, database and processor together) after the
index-growth hypothesis was eliminated by the reverse run.

**Detection is ~15% of per-record cost.** *(settled — measured)*
Detection p50 held at 0.10–0.11 ms across all nine runs and both orderings,
against a per-record cost of 0.69 ms at the best batch size. The remaining
~85% is produce, audit and broker/database round trips. This reframes the
tiering argument: Tier 1 rules are effectively free relative to the plumbing,
so the cost that matters for Tier 2 is model inference against a 0.69 ms
budget, not against the 0.10 ms of detection.

**The audit batch is one transaction.** *(settled)*
Each Postgres commit is a WAL fsync, ~1 ms. Sixty-four per-message
transactions is ~64 ms against ~8 ms of detection work — per-message auditing
would throw away most of the batching win. A rollback of N is wasted work, not
incorrectness, since replay rewrites idempotently.

**Per-message audit would *not* have avoided the crash-loop risk.** *(settled)*
The loop comes from commit granularity, not transaction granularity: the
offset can't advance past a bad message either way. What actually removed the
risk was envelope validation (§6).

**Transactions are managed by `conn.transaction()`, not manual commit.**
*(settled)* A manual commit leaves the connection in an aborted-transaction
state on failure, poisoning it for every subsequent statement. Currently
unreachable because an audit failure kills the process — but structural
correctness beats incidental correctness.

**Batching widens the replay window from 1 message to N.** *(settled,
consequence)* Still at-least-once, still safe because the audit upserts. The
cost is more repeated work on failure, and it scales with N. This is the
honest price of the throughput win.

**Commit the highest offset per partition, plus one.** *(settled)*
A batch can span partitions, so there is no single "batch offset". A committed
offset means *the next offset to consume*, not the last one processed — the
`+1` is not cosmetic. Take the max explicitly rather than trusting arrival
order.

**Error entries are filtered before offsets are computed.** *(settled)*
`consume()` returns broker notices alongside records; they carry a topic,
partition and offset but no payload. Letting one through advances the commit
position past a record that was never processed — the one failure in the whole
pipeline that loses data permanently.

---

## 8. Observability

**Progress bar on the producer, periodic stats line on the processor.**
*(settled)* The producer does bounded work so a bar applies. The processor
consumes an unbounded stream, so there is no total to progress against; a
stats tick is more useful and doubles as the benchmark data source.

**Never log per message at INFO.** *(settled)*
At these rates stdout becomes the bottleneck and the benchmark measures the
terminal. Per-message detail is DEBUG only.

**Latency percentiles come from a bounded rolling window.** *(settled)*
`deque(maxlen=...)`, so memory stays flat on a stream that never ends.
Percentiles describe recent behaviour, not all of history.

**Percentiles are index-based with no interpolation.** *(settled)*
`statistics.quantiles` interpolates between samples, inventing latency values
that were never measured — not acceptable in a number intended for
publication.

**`latency_ms` measures detection only, and is labelled as such.** *(settled)*
The timer starts immediately before `process_message`, excluding consume,
linger, audit, produce and flush. That makes it the right metric for isolating
tier cost — and the wrong one to present as "p99 latency", which reads as
end-to-end. The README benchmark table names the columns "detection p50/p95/p99"
and states plainly that end-to-end latency is not yet measured. Adding an
end-to-end measurement from `event_ts` remains open.

---

## 9. Testing

**Architecture chosen so the suite needs no infrastructure.** *(settled)*
Detection and routing are pure functions — message in, value out — so the
whole suite runs in under a second with no broker and no database. This was a
design choice about where I/O lives, not a testing trick.

**Stubs and fakes, no mocks.** *(settled)*
`StubMessage` answers queries; `RecordingConnection` and `FakeConsumer` are
working in-memory implementations that also record. Mocks couple tests to
implementation detail and break on every refactor.

**Fakes reproduce the real failure mode, not a convenient proxy.**
*(settled)* `ErrorMessage` carries a real topic/partition/offset specifically
so that omitting the error filter *silently advances the offset* rather than
crashing on a missing attribute. A fake that crashes would test the wrong
thing.

**One shared call log across the loop fakes.** *(settled)*
Ordering — audit before emit before commit — is the property that matters, and
it cannot be asserted from three separate logs.

**`main()` is tested by patching its dependencies on the module.** *(settled)*
Not a substitute for integration testing, and the docstring says so. It took
`processor.py` from 59% to 99% statement coverage.

**Assert on behaviour, never on implementation.** *(settled)*
No test asserts which regex matched. Tier 1 could be rewritten in Rust and
these tests would still describe what it must do.

**Every bug found gets a regression test.** *(settled)*
Angle-addr emails, IBAN checksums, non-UTF8 bytes, non-dict payloads, invalid
envelope fields — all are now tests.

**Coverage is a map of what's untested, not a grade.** *(settled)*
It records that a line executed, not that an assertion checked it. Mutation
testing — deliberately breaking the code to confirm a test fails — is the
check that actually matters, and has been applied to every safety-critical
path in this project.

---

## 10. Packaging and operations

**Python, not Go.** *(settled)*
Go was considered for the Securiti signal and deferred: at 5–10 hrs/week,
learning Go risks a half-finished project, and beginner Go under inspection
lands worse than clean Python. A single consumer rewritten in Go remains a
conditional later phase.

**`confluent-kafka`, not `kafka-python`.** *(settled)*
librdkafka-based, production standard, proper delivery callbacks, and it does
not cap throughput benchmarks.

**Installable package with a `src` layout.** *(settled)*
Without `pip install -e .` the documented quick start fails from a clean
clone and works only inside an IDE configured with `src` as a source root —
which is the first thing a reviewer would hit.

**Postgres published on host port 5433.** *(settled)*
5432 is commonly occupied by a native install; defaulting there connects
silently to the wrong database rather than failing loudly.

**Topics created explicitly; broker auto-create disabled.** *(settled)*
Three partitions each, so consumer-group scaling can actually be demonstrated.

**One image, four entry points.** *(settled)*
Processor (default `CMD`), topic creation, producer and governance report all
share every dependency, so four images would be four copies of the same
layers. `python:3.12-slim` rather than the host's 3.14, because
`confluent-kafka` and `psycopg[binary]` both publish manylinux wheels there —
which is what keeps a compiler and a separate librdkafka build out of the
image. The container has no reason to match the host interpreter.

**Topic creation is a one-shot container the processor waits on.** *(settled)*
`topics-init` runs `scripts/create_topics.py` and exits; the processor
declares `condition: service_completed_successfully` against it. With broker
auto-create disabled, a processor that started first would subscribe to
nothing. This converts "the topics have probably been created by now" into an
ordering guarantee.

**The producer and report are profiled services, not started by `up`.**
*(settled)*
Both sit behind the `tools` profile, so `docker compose up` brings up a
running pipeline idling on an empty topic rather than one that manufactures
its own traffic. `docker compose run` activates a service's profile
automatically, so they remain one command away. They use `entrypoint` rather
than `command` so arguments append:
`docker compose run --rm producer --rate 50 --count 1000`.

A pipeline that always generates data cannot be brought up for a benchmark, and
cannot demonstrate the empty-state governance report.

**No `container_name` on the processor.** *(settled)*
Compose refuses to scale a service that has one, and
`docker compose up --scale processor=3` is the entire consumer-group
demonstration. Verified: three consumers, one partition each, zero lag.

**`restart: unless-stopped` on the processor.** *(settled)*
This is what makes the crash-and-replay half of the failure taxonomy (§5) a
property of the deployment rather than an assertion in a README. Verified by
stopping Postgres under load: the processor crashed on the audit write and
cycled through 5 restarts, then drained the backlog when Postgres returned —
2,500 audit rows for 2,500 produced messages, no loss and no duplication.

Worth knowing: `docker kill` does *not* trigger it, because Docker treats an
explicit kill as a manual stop. The policy governs process exit, which is the
case the design is about.

**The Kafka log gets a named volume.** *(settled)*
Previously absent, so `docker compose down` discarded every message and every
committed offset. Convenient while benchmarking and incoherent for a pipeline
whose at-least-once argument rests on that log surviving a restart. Verified
across `down`/`up`: 2,000 messages and their committed offsets both survived,
and the processor resumed without reprocessing.

**`.dockerignore` is load-bearing, not hygiene.** *(settled)*
`data/nemotron-pii/` holds a 150 MB parquet, and two virtualenvs sit in the
repo root; the build context is sent to the daemon in full on every build.
Excluding `.env` is the more important half: `config.py` calls
`load_dotenv()`, and the host `.env` points at `localhost:9092` and
`localhost:5433`, both unreachable from a container. Compose `environment:`
entries win because `load_dotenv()` does not override variables already set —
but only while the file never reaches the image.

**Private until MVP; MVP is defined as four things.** *(settled)*
Runs at volume under `docker compose up`, a benchmark from a real high-volume
run, a governance report, and Tier 2 via an off-the-shelf pretrained encoder
(§3). The locale fine-tune, Tier 3, the chat topic and Airflow are explicitly
post-MVP. Including a basic encoder means the "tiered detection" headline is
backed by something measured at launch rather than being a roadmap promise.

---

## 11. Evaluation corpus

Raw measurements behind this section are in
[docs/corpus-findings.md](corpus-findings.md).

Detection quality has never been measured against independent ground truth.
The synthetic stream cannot supply it: `generator/transactions.py` emits CNICs
with valid province digits and IBANs with correct check digits *because*
`tier1_rules.py` validates those things, so recall against it is 100% by
construction. This section records what was chosen instead and what was
measured about it.

**`nvidia/Nemotron-PII` as the primary evaluation corpus.** *(settled)*
CC BY 4.0, span-level annotations, 55+ entity types. It is synthetic, which is
not the property that matters — the property that matters is that NVIDIA
generated it with no knowledge of this detector. Independence from the system
under test is what makes a number credible; realism is secondary and, for a
privacy tool, expensive to obtain legitimately.

**The downloaded file is the complete test split.** *(settled)*
`test-00000-of-00001.parquet`, 100,000 rows. The dataset card lists train 100k
+ test 100k = 200k. An earlier note recording a 50k/50k split was wrong.

**`us` and `intl` are the same documents rendered twice.** *(settled)*
49,987 distinct `document_description` values, every one present in both
locales — Jaccard 1.000. `document_format` and `domain` counts are identical
across locales to the record. So the corpus is 50,000 documents in a
matched-pairs design, not 100,000 independent samples, and effective sample
size must be reported as such.

The upside is larger than the caveat: same document, same domain, same format,
only the locale-specific identifiers differ. That is a controlled experiment
for free — "does recall drop when US identifiers become international ones?"
is answerable on paired data rather than by comparing two unrelated samples.

**The corpus's identifiers do not satisfy their own checksums.** *(settled,
measured)* Over the full test split:

| identifier | passes | chance baseline |
|---|---:|---:|
| credit card (Luhn) | 1,528 / 12,867 = **11.9%** | ~10% |
| bank routing number (ABA) | 843 / 8,354 = **10.1%** | ~10% |
| SWIFT/BIC (ISO 9362 shape) | 4,246 / 5,559 = 76.4% | — |

Cards and routing numbers are random digits carrying correct prefixes and
correct lengths; the check digit was never computed. This is the same defect
recorded in §2 for this project's own generator ("IBANs carry real mod-97
check digits. Originally random…"), present in a published NVIDIA dataset.

BIC is better but not clean: 77.1% are 8 or 11 characters, and 89.5% hold a
real ISO 3166 alpha-2 code at positions 5–6, so roughly one in four is
structurally invalid.

**Detection and validation are separate measurements.** *(settled)*
Consequence of the above. A structural match that fails its checksum is a
*detection*; whether it validates is a second, independent fact. Collapsing
them into one confidence number — as the superseded §3 entry did — means a
recall figure computed on this corpus would report ~12% on credit cards and
~10% on routing numbers, and would be measuring NVIDIA's generator rather than
this detector. Findings therefore carry validation status explicitly, and the
evaluation reports recall-at-detection and recall-at-validation separately.

**Checksum failure routes to quarantine for PK entities only.** *(settled)*
`Finding` gains an explicit validation status so that detection and validation
can be counted separately for *every* entity type. Routing, however, changes
only for the new generic pack: card / ABA / BIC structural matches redact
regardless of checksum, while PK IBAN and CNIC keep quarantining on checksum
failure exactly as today.

The asymmetry is deliberate and has a reason that survives being asked about:
**this project's generator computes real check digits, and NVIDIA's does not.**
Where a checksum failure is genuine evidence of corruption it should route to
review; where the corpus fails checksums at chance it is evidence of nothing.
Applying one uniform rule would have meant either quarantining ~90% of the
corpus's cards, or discarding `CORRUPT_IBAN_RATE` — which §2 records as
existing specifically to exercise the quarantine path end to end — and
invalidating the README's measured 3.87% quarantine rate.

**The `intl` half does not serve the Pakistani-locale evaluation.** *(settled,
measured)* Directly tested, since a positive result would have removed the need
for hand-built PK data:

- **0** PK-IBAN-shaped strings in 100,000 documents. There is no IBAN label in
  the taxonomy at all — the financial identifiers are `swift_bic`,
  `bank_routing_number` and `account_number`.
- Any-country IBAN shape: 34 intl / 14 us documents, consistent with regex
  coincidence.
- CNIC canonical shape: 20 intl documents, coincidental 5-7-1 digit groupings.
- `+92` does not appear among the top 20 dialling codes in phone spans; +91
  (India, 325) is the nearest. Pakistan does not appear among the top 25
  `country` surface forms — the intl half is weighted to Russia, the UK,
  France, India, Germany, Italy and South Korea.
- `national_id` is the one intl-only label (2,847 mentions), but its values
  span unrelated national formats (UK NINO, Chilean RUT, French INSEE) with
  **no country attribute on the span**. Detecting "national ID of unspecified
  country" is not a rules problem, and claiming otherwise would be dishonest.

A hand-built Pakistani evaluation set therefore remains necessary.

**Intl phone numbers are a usable precision probe.** *(settled)*
7,591 of 12,268 intl phone spans carry no `+` prefix and appear in bare local
formats; some match the PK mobile shape `03XX XXXXXXX` exactly. The existing
PK phone rule will fire on them, which yields a genuine **precision**
measurement against independent gold data — currently unobtainable any other
way, since the synthetic stream contains no near-miss negatives.

**The Kaggle `faker-pk` HR dataset is a name source, not an evaluation
corpus.** *(settled)*
`muhammadkhubaibahmad/synthetic-hr-dataset-created-by-faker-pk`, Apache 2.0,
~1.7 MB, generated with the `faker-pk` library. Columns: `employee_ID`,
`full_name`, `gender`, `caste`, `sect`, `dob`, `industry`, `salary`,
`sim_provider`, `bank_user`, `city`, `province`, `address`.

It cannot serve as an evaluation corpus: it is tabular with no span
annotations, and it carries **no CNIC, no IBAN and no phone number** —
`sim_provider` is an operator name and `bank_user` a bank name, neither an
identifier. So it measures nothing this detector does.

It does solve a narrower problem. Phase 4 requires PK names *sourced
independently of the author*, disjoint between generation and evaluation. The
generator's current pool is 50 hand-written names (30 first, 20 last, including
the author's own), which makes an honest disjoint split impossible. An
Apache-2.0 name, city, province and address pool the author did not write fixes
the provenance objection. Two caveats to state when used: `faker-pk`'s pool is
itself finite, so this is "independent of *our* list" rather than a natural
name distribution, and overlap against `FIRST_NAMES`/`LAST_NAMES` must be
measured and reported, not assumed.

**`caste` and `sect` are a Pakistan-specific special-category signal.** *(open)*
Surfaced by the dataset above: PK HR records routinely carry sect and caste,
which are GDPR Article 9 special-category data (religion, ethnic origin) and
acutely sensitive locally. Sensitive attributes are out of scope because they
are format-free and therefore not a rules problem — but the *locale focus*
makes this category more prominent than a Western-derived taxonomy would
suggest, and the scope exclusion should say so explicitly rather than leaving
it implicit. Not yet written into §1.

**TAB was evaluated and dropped.** *(settled)*
The Text Anonymization Benchmark (MIT licence, 1,268 European Court of Human
Rights judgments, expert annotations, `identifier_type` marking each mention
DIRECT / QUASI / NO_MASK) is the only real-text, span-labelled, redistributable
corpus in this space — i2b2 is DUA-gated and unreproducible by a reader, Enron
is unlabelled and ethically incompatible with this project's premise, and
CoNLL-style corpora are not PII. So it was the right answer to "how do we get
real data."

That was the wrong question. Under §1's instrument rule, a European legal
corpus answers nothing this project asks: it would not improve the escalation
measurement, it shares almost no entity types with the detector, and adopting
it would have required reopening the "no real personal data" decision to buy
breadth the project does not want. Recorded here rather than deleted, so the
reasoning is available if the scope is ever revisited.

---

## 12. Open questions

- **Benchmark methodology** — hardware disclosure, message-size distribution,
  and how to measure Tier 3 fairly given it is API-bound.
- ~~**`docker compose up` does not run the pipeline.**~~ **Closed** — Dockerfile
  plus `topics-init`, `processor`, and profiled `producer`/`report` services.
  `docker compose up` now brings up a running pipeline; `--scale processor=3`
  gives the consumer-group demo in one command.
- **Integration tests** against a live stack: not attempted.
- **Tier 2** — base model, name-substitution training data, disjoint train/eval
  name split, where weights live.
- ~~**Tier 3** — the confidence band that triggers escalation.~~ **Closed by
  measurement** (tier2-detection-findings.md §22). The predicted failure
  condition fired: the cheapest trigger escalates 35.6% of the stream, eighteen
  times the ~2% budget, to reach two of three leaking records. Only 0.25% of
  records leak after both tiers, and all of those are positional boundary
  errors a span rule already reaches. Recorded as a measured non-goal.
- **Topic retention** — quarantine plausibly wants longer retention than clean,
  for compliance.
- **Quarantine has no resolution path.** It is terminal; there is no reviewer
  workflow, and no DLQ distinction between "failed once" and "fails forever".
