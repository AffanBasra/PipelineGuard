# Tier 2 findings — does off-the-shelf NER handle Pakistani data?

Measured 2026-07-29, before committing to a Tier 2 scope. This document exists
because the answer contradicted the assumption the plan was built on, and a
measured negative result is easy to lose.

**The question.** `docs/decisions.md` §3 says the locale fine-tune will be
justified by a measured before/after — *"off-the-shelf NER missed X% of
Roman-Urdu names."* That presumes off-the-shelf NER does badly on Roman Urdu.
Nobody had checked. If the premise is false, the argument for adding
Roman-Urdu data to the generator collapses.

Reproduce with `scripts/probe_ner_locale.py` (needs `torch`, `transformers`,
`gliner`; set `HF_HUB_DISABLE_XET=1` or the download hangs).

---

## 1. Method

237 cases. The same values appear across every condition, so the text around
them is the only thing that varies.

**PERSON** — 11 names from the generator's own pools × 5 templates × 3
sentence styles = 165. Names are split into `common` (5), `rare` (3) and
`ambiguous` (3 — "Noor", "Iman", "Eman", first names that are also ordinary
Urdu nouns; `transactions.py:17` says they exist to stress Tier 2).

**ADDRESS** — a 2×2, because a drop could be caused by either variable and an
aggregate would not say which:

| | |
|---|---|
| *sentence language* | english / codeswitch / roman_urdu |
| *address form* | `House 12, Street 4, F-8/3 Islamabad` vs `Ghar 12, Gali 4, F-8/3 Islamabad` |

Each Urdu-form address is its English-form counterpart with **only the
structural nouns swapped** (House→Ghar, Street→Gali, Flat→Makan). 6 addresses
× 4 templates × 3 styles = 72, giving **n=12 per 2×2 cell** — small, and the
reason this is an indication rather than a benchmark.

**Three metrics**, because they mean different things for a redaction firewall:

- `exact` — predicted span boundaries equal the gold span
- `any-hit` — some prediction overlaps it (deliberately generous, so reported
  failure is a floor)
- **`char coverage`** — fraction of the gold span's *characters* covered

Coverage is the operative one. Detecting `Islamabad` inside `House 12, Street
4, F-8/3 Islamabad` is an any-hit success and a **redaction failure**: the
house number survives into the clean stream.

---

## 2. PERSON — the premise is false for any model worth shipping

Character coverage:

| model | english | codeswitch | roman_urdu |
|---|---:|---:|---:|
| `dslim/bert-base-NER` | 91% | 87% | **74%** |
| `urchade/gliner_multi_pii-v1` | 98% | 100% | **100%** |
| `nvidia/gliner-PII` | 91% | 91% | **91%** |

`nvidia/gliner-PII` is **flat to the percentage point** across all three
styles, at 100% any-hit in every difficulty band. Its 91% is not a miss: it
returns `'Ayesha'` and `'Malik'` as separate entities, so the only uncovered
character is the space between them. Redacting both leaves `" "`, which is
functionally complete.

`urchade/gliner_multi_pii-v1` reaches 100% coverage on Roman Urdu. Its `exact`
drop to 80% is *over*-capture — it returns `'Bhai Ayesha Malik'`, swallowing
the honorific — which for redaction is the safe direction to err.

**Only the weak English-only model degrades**, and it degrades on the axis you
would expect — name rarity, not language:

`dslim/bert-base-NER`, coverage by name difficulty (n=25/15/15 per cell):

| style | common | rare | ambiguous |
|---|---:|---:|---:|
| english | 98% | 72% | 97% |
| codeswitch | 94% | 70% | 93% |
| roman_urdu | 86% | **43%** | 84% |

Note the ambiguous names ("Noor Khan", "Iman Raza") are handled as well as
common ones by every model. The deliberate stress case in the generator turns
out not to stress anything.

> **Conclusion: do not add Roman-Urdu memo templates to make names harder.**
> They do not make names harder. `decisions.md` §3's "off-the-shelf NER missed
> X% of Roman-Urdu names" is not a claim this evidence supports for a
> production-grade model.

---

## 3. ADDRESS — the premise holds, and both variables bite

Character coverage by sentence style:

| model | english | codeswitch | roman_urdu |
|---|---:|---:|---:|
| `dslim/bert-base-NER` | 60% | 51% | 48% |
| `urchade/gliner_multi_pii-v1` | 55% | 78% | 78% |
| `nvidia/gliner-PII` | 83% | 72% | **64%** |

Exact-match rates: `bert` **0%** and `nvidia/gliner-PII` **0%** across all 72
cases; `urchade/gliner_multi_pii-v1` managed 21–29%. So exact address
extraction is rare but not impossible.

### The 2×2, `nvidia/gliner-PII`

| sentence ↓ / form → | english_form | urdu_form |
|---|---:|---:|
| english | **96%** | 70% |
| codeswitch | 83% | 60% |
| roman_urdu | 77% | **51%** |

Fully English → **96%**. Fully Roman Urdu → **51%**. The two effects are
independent and compound: swapping only the address form costs 26 points
(96→70); swapping only the sentence language costs 19 (96→77).

The failure mode is consistent, and it is the dangerous one:

```
'Ghar 12, Gali 4, F-8/3 Islamabad'      -> 'Islamabad'   (28% coverage)
'House No. 221, Sector G-9/1, Islamabad' -> 'Islamabad'   (24% coverage)
```

The city is found; the house number, street and sector — the parts that
actually identify a person — are left in the clear. A pipeline shipping this
would appear to have redacted the address.

### Honest caveat on the other two models

Only `nvidia/gliner-PII` shows a clean monotonic pattern. `bert` scores
*higher* on urdu_form than english_form (65% vs 56%), which is a floor effect
— it is uniformly bad at ~50% everywhere. `urchade/gliner_multi_pii-v1`
scores *higher* on Roman-Urdu sentences than English ones (78% vs 55%), which
is not explicable and at n=12 per cell is probably noise.

So the address finding rests mainly on the best model. That is the right
model to rest it on — it is the one under consideration — but it is one
model, not three agreeing.

---

## 4. What this changes

1. **Address is the entity type worth adding to the generator**, not
   Roman-Urdu names. It is the measurable gap in every model and every
   language.
2. **Roman Urdu still earns its place — for addresses.** `Ghar`/`Gali` costs
   26 coverage points on its own, which is what makes the locale claim
   substantive rather than decorative.
3. **This challenges a PERSON-only Tier 2 scope.** On this evidence PERSON is
   nearly solved off-the-shelf while ADDRESS is not, so a PERSON-only tier
   would ship the easy half.
4. ~~**`nvidia/gliner-PII` is the model to use**~~ — **superseded by §6.**
   This was decided at a single threshold that was never varied. Sweeping it
   reverses the result, and measuring latency makes cost, not coverage, the
   binding constraint.
5. **The README's "Urdu/Roman-Urdu names" wording is wrong regardless.** The
   generator emits Pakistani proper nouns in Latin script — transliteration,
   not Roman Urdu, of which there is currently none in the pipeline. The two
   Urdu loanwords present ("Zakat", "Eidi") sit inside English sentences.

---

## 5. Limits

- **237 hand-built cases is an indication, not a benchmark.** Address 2×2
  cells hold n=12.
- **The Roman Urdu was written by the author, who is not a native writer of
  it.** If the phrasing is unnatural, the models are being judged on text no
  real user would produce, and the address-form numbers deserve the most
  scrutiny since they turn on `Ghar`/`Gali`/`Makan`.
- **`dslim/bert-base-NER` is scored on a task it never claimed.** CoNLL has no
  address class; `LOC` is the closest label it can emit. Its address row is
  weaker evidence than its person row.
- **`exact` is unreliable for `bert`** — it splits names across subwords
  (`'Aye'`, `'sha Malik'`), so boundaries rarely match while coverage is
  100%. Read its coverage column, not its exact column.
- ~~**Latency was not measured.**~~ **Closed by §6**, and it is the finding
  that matters most: Tier 2 costs 65× the entire current per-record budget.

---

## 6. Second pass — threshold, latency, architecture

Measured 2026-08-05 with `scripts/probe_ner_sweep.py`. At threshold 0.4 every
number in §2 and §3 reproduces exactly, so the harness is the same instrument;
what follows is what §1–§5 did not vary.

`dslim/bert-base-NER` is dropped here — §2 established it is not a candidate.

### 6.1 The threshold was never tuned, and it decides the winner

`probe_ner_locale.py:181` hardcodes `threshold=0.4`. Sweeping it:

| ADDRESS char coverage | urchade | nvidia |
|---|---:|---:|
| 0.25 | **85%** | 82% |
| 0.40 | 70% | **73%** |
| 0.55 | 52% | **60%** |

The models **cross over near 0.3**. urchade swings 33 points across the range,
nvidia 22; the gap between them at any single threshold is at most 8. So
**threshold placement is worth 3–4× more than the model choice §4 was decided
on** — and 0.4 sits just past the crossover, in the only region where nvidia
leads. NVIDIA evaluate their own model at 0.3, below where §3 scored it.

PERSON is completely threshold-invariant: urchade 99% and nvidia 91% at all
three thresholds, unchanged to three decimals. Both models' name detections sit
far above 0.55, so §2's conclusion is unaffected by any of this.

### 6.2 The ADDRESS 2×2 for both models

§3 conceded the address finding rested on one model. It no longer does. At
threshold 0.25:

| sentence ↓ / form → | urchade en | urchade urdu | nvidia en | nvidia urdu |
|---|---:|---:|---:|---:|
| english | 94% | **57%** | 96% | 82% |
| codeswitch | 95% | 83% | 89% | 69% |
| roman_urdu | 97% | 81% | 89% | 69% |

**The address-form penalty replicates**: urchade 95%→74% (−21 points averaged
over styles), nvidia 91%→73% (−18). Two independent models agree.

**The sentence-language effect does not.** nvidia falls 89→79 going English to
Roman Urdu; urchade *rises* 76→89. Opposite directions. So of §3's two
variables, form is robust and sentence language is not — which sharpens §4.2:
what earns Roman Urdu its place is `Ghar`/`Gali`/`Makan`, not Roman-Urdu
sentence framing.

§3's "not explicable" urchade anomaly also localises. Its weak cell is
specifically an Urdu-form address inside an *English* sentence (57%); both
congruent cells score 81–83%. That is a context-congruence pattern, not noise.

### 6.3 Latency — the number the tiering argument rests on

CPU, `torch 2.13.0+cpu`, p50 of 20 calls after 3 discarded warm-ups:

| input | urchade | nvidia |
|---|---:|---:|
| memo, 60 chars | 123 ms | 505 ms |
| 601 chars | 311 ms | 700 ms |
| 969 chars | 491 ms | 910 ms |

Batched, per record, 60-char input:

| batch | urchade | nvidia |
|---:|---:|---:|
| 1 | 131.9 ms | 381.5 ms |
| 8 | 45.1 ms | 114.1 ms |
| 32 | **44.6 ms** | **110.7 ms** |

Batching buys ~3× and **saturates at 8**.

Against the measured pipeline (README / `handoff.md` §3: 0.69 ms per record,
1,450 msg/s, Tier 1 detection 0.10 ms), the best case — urchade, batched — is
**65× the entire per-record budget and 446× Tier 1's detection cost.** Tier 2
on every record gives ~22 rec/s.

The escalation budget that follows, at `0.69 + r × 44.6 ms`:

| escalation rate | per record | throughput |
|---:|---:|---:|
| 100% | 45.3 ms | 22 /s |
| 10% | 5.15 ms | 194 /s |
| **1.55%** | 1.38 ms | 725 /s (half of today) |
| 1% | 1.14 ms | 879 /s |

**Tiering only pays if escalation stays near 1%.** `decisions.md` §12 predicted
this failure condition for Tier 3 — *"if it exceeds ~1–2% of messages the cost
argument for tiering collapses"* — and it binds on Tier 2 first.

This invalidates the escalation rule proposed in `handoff.md` §7 (*escalate
unless Tier 1's findings cover the entire field*): the memo is free text and
Tier 1 covers only CNIC/IBAN/phone/email, so essentially every record
escalates. It also inverts the profile in §3 of that document — detection goes
from ~15% of per-record cost to ~98.5%.

### 6.4 Architecture, from the configs on disk

| | urchade | nvidia |
|---|---|---|
| backbone | `mdeberta-v3-base` | `deberta-v3-large` |
| layers / hidden | 12 / 768 | 24 / 1024 |
| vocab | 251,000 | 128,004 |
| total params | 288,949,504 | 445,463,040 |
| word embeddings | 192,080,640 | 131,076,096 |
| **compute-bearing** | **96,868,864** | **314,386,944** |
| licence | **apache-2.0** | NVIDIA Open Model License |
| training data | synthetic-pii-ner-mistral-v1 | **nemotron-pii** |

Compute-bearing ratio **3.24×**; measured latency ratio 1.85–4.09×, which
brackets it. File size (1,699 vs 1,102 MB) understates the gap by more than
half, because 66% of urchade's weights are a vocabulary lookup that costs
nothing per token.

NVIDIA's card claims 5.7×10⁸ parameters; the loaded state dict totals 445.5M.
The ~125M discrepancy is unexplained.

### 6.5 A mechanism that was tested and refuted

The obvious explanation for the Urdu-form penalty is subword fragmentation.
It is wrong, and measurably so.

Tokens per word: urchade 1.661 / 1.505 / 1.466 across english / codeswitch /
roman_urdu, nvidia 1.479 / 1.380 / 1.448. The English-only tokenizer fragments
*less* on every style, and **neither model degrades** from English to Roman
Urdu.

On the address forms specifically:

| | english_form | urdu_form | delta |
|---|---:|---:|---:|
| mdeberta (urchade) | 35 tok | 38 tok | +8.6% |
| deberta-v3-large (nvidia) | 38 tok | 38 tok | **0%** |

`Ghar` and `Makan` split in two under mdeberta; both are **single vocabulary
items** under deberta-v3-large. So the English-only tokenizer handles the Urdu
structural nouns better than the multilingual one.

This is a causal exclusion, not just a dead hypothesis: **for `nvidia/gliner-PII`
the Urdu-form penalty cannot be tokenization, because tokenization is identical
between the two forms.** The cause is semantic — the training distribution never
associated `Ghar 12, Gali 4` with an address label. That is a stronger basis for
the locale argument in §4.2 than a tokenizer story would have been.

### 6.6 What this changes

**`urchade/gliner_multi_pii-v1`, replacing §4.4's choice.** 2.5× faster (so 2.5×
the escalation budget, which is the binding constraint), 8 points better on
PERSON at every threshold, better on ADDRESS at 0.25, Apache-2.0 rather than a
bespoke licence, and — decisively for the evaluation plan — **not trained on
Nemotron**, so `handoff.md` §7's Phase 0–3 yields a genuine held-out number.
NVIDIA's own card quantifies why that matters: 0.87 F1 on Nemotron against
0.64–0.70 held out.

nvidia wins exactly one cell: English-form address in an English sentence, 96%
vs 94% at threshold 0.25 — a 2-point gap, not the 28-point gap visible at 0.4.

### 6.7 Limits specific to this pass

- **CPU only.** NVIDIA's published testing used an A100. A GPU changes every
  latency number here by one to two orders of magnitude and may reorder them.
- **Batch latency is a best case.** One string was replicated; GLiNER pads to
  the longest sequence in a batch, so mixed-length memos cost more per record.
- **The 601 and 969-char inputs are repeated filler** — right length, wrong
  entity density.
- **20 samples per latency cell**, on the laptop `handoff.md` §3 already
  documents as thermally unstable under sustained load.
- **No threshold below 0.25 was tested**, and urchade was still improving at
  the bottom of the range. The optimum may lie lower.
- **Precision was not measured.** Every case here contains a real entity, so
  these are coverage numbers only. Nothing in this document says how much
  non-PII text either model would over-redact.
- Address 2×2 cells remain **n=12**.

---

## 7. Can the cost be engineered away? Mostly no

Measured 2026-08-05 with `scripts/probe_ner_runtime.py`, on
`urchade/gliner_multi_pii-v1`, CPU. §6.3 leaves one live option: if the model
can be made ~30× cheaper, tiering works; if not, the architecture claim has to
be restated. This tests the two standard CPU routes.

Accuracy is measured for every configuration, not just speed. `gliner/model.py:562`
warns that *"stock DeBERTa-based models lose accuracy with int8"* absent
quantization-aware training, and this is exactly such a model.

### 7.1 Results

| configuration | batch-8 ms/rec | speedup | PERSON cov | ADDRESS cov |
|---|---:|---:|---:|---:|
| torch fp32 @ 0.25 (baseline) | 40.9 | 1.00× | 99.4% | 84.7% |
| torch int8 @ 0.25 | 20.7 | 1.97× | **0.0%** | **0.0%** |
| **torch int8 @ 0.002** | **20.7** | **1.97×** | **98.4%** | **85.7%** |
| ONNX fp32 @ 0.25 | 31.0 | 1.32× | 99.4% | 84.7% |
| ONNX int8 @ 0.25 | 22.8 | 1.79× | 89.1% | 35.2% |

ONNX fp32 reproduces fp32 coverage **exactly** (99.4% / 84.7%), which is the
check that the export is numerically faithful rather than merely fast.

### 7.2 int8 breaks calibration, not discrimination

Read at the fp32 threshold, int8 looks totally destroyed — zero coverage. That
reading is wrong, and the raw scores show why:

```
fp32   'Ayesha Malik' 0.999   'House 12' 0.347   'Islamabad' 0.582
int8   'Ayesha Malik' 0.044   'House 12' 0.054   'Islamabad' 0.023
```

The *ranking* is intact — real entities still outscore `'Transfer'` (0.003) and
`'to'` (0.0008). Quantization compresses the score distribution roughly 20×
toward zero without reordering it. Recalibrating the threshold recovers the
model:

| int8 threshold | PERSON | ADDRESS | preds/case |
|---:|---:|---:|---:|
| 0.002 | 98.4% | **85.7%** | 3.64 |
| 0.005 | 96.5% | 83.2% | 2.84 |
| 0.01 | 85.0% | 65.1% | 1.93 |
| 0.02 | 51.1% | 31.9% | 0.89 |

At 0.002 int8 matches fp32 on both entity types — ADDRESS is marginally
*better* — while emitting 3.64 predictions per case against fp32's 2.93, i.e.
24% more spans, at half the cost. Whether that 24% is affordable is a precision
question §7 does not answer; see §8.

**Reporting int8 as unusable would have been wrong**, and would have discarded
a 2× speedup. Threshold and quantization are not independent knobs.

### 7.3 What it does not fix

> **Superseded by §8.** int8 at 0.002 is *not* viable. It reaches fp32 coverage
> by redacting almost everything — 94% of the characters in clean English memos
> and 84% in clean Roman-Urdu ones. The recall parity below is an artifact of
> firing on all input, and the correct best viable configuration is **ONNX fp32
> at 31.0 ms/record**, not 20.7. The rest of this section is left as measured.

Best viable configuration is **torch int8 at threshold 0.002, 20.7 ms/record**.
Against the pipeline's measured 0.69 ms:

| escalation rate | per record | throughput |
|---:|---:|---:|
| 100% | 21.4 ms | 47 /s |
| **50%** (the realistic floor) | 11.0 ms | **91 /s** |
| 3.3% | 1.38 ms | 725 /s |

Roughly half of all memos genuinely contain a name (5 of 10 templates at
`transactions.py:35`), so 50% is the floor a *perfect* escalation predicate
would reach. That floor moves from 43 rec/s to 91 rec/s — against 1,450 today.

**Quantization buys ~2×. The gap is ~30×.** Runtime optimisation narrows it
from 65× to 30× and does not close it. On CPU, Tier 2 in the synchronous path
costs roughly an order of magnitude more than the tiering argument assumed,
and no threshold, predicate or generator change alters that.

What remains untested: GPU (NVIDIA's own testing used an A100, and a GPU is the
one lever with the right order of magnitude), a smaller backbone such as
`gliner_small`, and reducing `max_len` from 384. Recalibrating ONNX int8 was
also not attempted — it is slower than torch int8 (22.8 vs 20.7 ms), so it
could not have won.

---

## 8. Precision — how much clean text does Tier 2 destroy?

Measured 2026-08-05 with `scripts/probe_ner_precision.py`. Every case in §1–§7
contains a real entity, so nothing so far measures over-redaction. That left
`decisions.md` §3's standing justification for a low threshold — *"a false
positive costs one over-redacted string, a false negative is a leak"* — as an
assertion. It is now measured, and it is more expensive than that phrasing
implies.

43 negative inputs, none containing a person or an address, so **every**
prediction is a false positive: 13 pipeline field values, the 5 memo templates
at `transactions.py:35` that embed no name, and 25 hand-written Roman-Urdu and
code-switched memos. Two metrics — `fires` (share of inputs with at least one
spurious span) and `over-redacted` (share of all characters that would be
masked).

### 8.1 Results

| config | thr | pipeline field | clean memo | PK negative |
|---|---:|---:|---:|---:|
| | | fires / over-red | fires / over-red | fires / over-red |
| urchade fp32 | 0.25 | 54% / 46% | 20% / 8% | 40% / **21%** |
| urchade fp32 | 0.40 | 31% / 29% | 20% / 8% | 32% / 17% |
| urchade fp32 | 0.55 | 15% / 15% | 20% / 8% | 24% / 14% |
| **urchade int8** | 0.002 | 100% / **98%** | 100% / **94%** | 100% / **84%** |
| urchade int8 | 0.005 | 100% / 98% | 100% / 94% | 96% / 63% |
| urchade int8 | 0.01 | 100% / 87% | 100% / 77% | 92% / 44% |
| nvidia fp32 | 0.25 | 46% / 31% | 40% / 14% | **68%** / 23% |
| nvidia fp32 | 0.55 | 23% / 19% | 40% / 10% | **68%** / 21% |

### 8.2 int8 was a mirage, and this is why precision had to be measured

§7.2 reported int8 at threshold 0.002 matching fp32 coverage at half the cost,
and called it the leading candidate. **That conclusion was wrong.** It reaches
that coverage by redacting nearly everything:

```
'Zakat contribution'    -> whole string, PERSON:first_name  (18/18 chars)
'Utility bill payment'  -> whole string, PERSON + ADDRESS   (20/20 chars)
'Loan installment'      -> whole string, PERSON + ADDRESS   (16/16 chars)
'atm'                   -> PERSON:last_name + ADDRESS:address (3/3 chars)
```

100% of clean memos fire and 94% of their characters are masked. A model that
flags all input scores perfect recall, and a recall-only evaluation cannot tell
that apart from a model that works. **Two measurements were required to see
it**, and the 2× speedup is not available at any usable threshold.

### 8.3 A false positive is not "one over-redacted string"

Even fp32 at 0.25 — the configuration §6 recommends — destroys whole memos:

```
'Paisay bhej diye hain'         -> whole string, PERSON  (21/21 chars)
'Kiraya agle mahine bhejunga'   -> whole string, PERSON + ADDRESS  (27/27)
'Rakam wapas bhej dein'         -> whole string  (21/21)
'raast'                         -> PERSON:first_name
'branch'                        -> ADDRESS:address
```

40% of clean Roman-Urdu memos are hit and 21% of their characters masked. The
`decisions.md` §3 framing survives *directionally* — dropping the threshold from
0.55 to 0.25 buys 33 points of address coverage (§6.1) for 7 points of
over-redaction, a favourable ratio — but the absolute cost is a fifth of the
clean stream's Roman-Urdu text, not one string.

### 8.4 Roman Urdu is penalised twice

| clean text | over-redaction, urchade @ 0.25 |
|---|---:|
| English memos | 8% |
| Roman-Urdu memos | **21%** |

**2.6× worse on the locale this project exists to serve** — and in the same
direction as §3's detection finding. Roman-Urdu addresses are *under*-detected
and Roman-Urdu clean text is *over*-redacted. Both failures come from the same
place: the training distribution has not seen this language, so the model is
uncertain in both directions at once.

The name-homograph cases are the sharpest instance. Splitting the PK negatives
into plain and ambiguous (Urdu nouns that are also Pakistani first names —
`noor` = light, `iman` = faith, `sana` = praise, used here as ordinary nouns):

| | plain | ambiguous |
|---|---:|---:|
| urchade @ 0.25 | 27% fire | **60%** fire |
| nvidia @ 0.25 | 47% fire | **100%** fire |

`transactions.py:19` puts those names in the generator "to stress Tier 2".
§2 found they do not make *detection* harder — every model handles them as well
as common names. They make **over-redaction** much harder, which is the
opposite end of the same problem and the direction nobody looked.

### 8.5 This reinforces the model choice

nvidia fires on **68% of PK negatives at every threshold tested**, identical to
the percentage point from 0.25 to 0.55 — the same threshold-insensitivity §6.2
found in its PERSON numbers, and here it is a liability: there is no setting at
which it over-redacts less. urchade ranges 24–40% and responds to the knob.

Combined with §6.6, `urchade/gliner_multi_pii-v1` wins on cost, on PERSON
coverage, on ADDRESS coverage at 0.25, on licence, on training-set
independence, and now on precision.

### 8.6 Limits

- **43 negative cases**, hand-built. An indication, not a benchmark — the same
  standing as §1's positive set.
- **The Roman Urdu is written by the author**, who is not a native writer of it
  (§5). It constrains this section exactly as it constrains §3.
- **Over-redaction is measured on the assumption that every predicted span is
  masked.** A production redactor might mask only the highest-scoring
  non-overlapping spans, which would lower these numbers.
- **The identifier fields are not pure negatives.** A PERSON hit on a CNIC is a
  mislabel rather than a leak, and Tier 1 would have redacted that field
  anyway, so `pipeline_field` over-redaction overstates real harm. The
  `channel` values (`atm`, `branch`, `raast`) are true negatives and are the
  ones that should worry.
- **No precision measurement against an independent corpus.** Nemotron-PII has
  gold spans and would give a defensible number on text this project did not
  write; it is US/intl prose, so it answers a different question than the PK
  negatives do. Not attempted here.
