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

> **Partly corrected by §9.2.** Four of the 25 PK negatives contain a token no
> Roman-Urdu corpus attests. Excluding them, over-redaction at 0.25 falls from
> 21% to **16%** and the fire rate from 40% to 38%. The multiplier below is
> therefore **2×, not 2.6×** — the effect is real and smaller than first
> measured.

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

---

## 9. Is the Roman Urdu real? Mostly yes, with two defects

Measured 2026-08-05 with `scripts/probe_urdu_plausibility.py`. §5 records that
the Roman Urdu in these probes was written by the author, who is not a native
writer of it — the softest point under the project's headline finding. This
checks the probe vocabulary against Roman Urdu written by actual speakers.

Three public corpora, so a word is attested by independent sources rather than
by whichever one happened to be chosen:

| corpus | licence | tokens |
|---|---|---:|
| `community-datasets/roman_urdu` | — | 264,286 |
| `Khubaib01/RomanUrdu-NLP-Sentiment-Corpus` | Apache-2.0 | 1,764,194 |
| `hafiz-hassaan-saeed/Roman-Urdu-Toxic-Corpus` | CC BY 4.0 | 1,402,163 |

These are scraped social-media corpora that may contain real names, so they are
used strictly one-directionally, per `decisions.md` §1: token frequencies are
computed, this project's own word list is looked up against them, and only
aggregate statistics about our words are retained. No corpus sentence is
stored, printed or committed.

### 9.1 The address nouns are real and common

**53 of 55 probe words appear in all three corpora.** The three the entire §3
address finding rests on, in occurrences per million tokens:

| word | roman_urdu | sentiment | toxic |
|---|---:|---:|---:|
| `ghar` | 654.6 | 465.9 | 649.0 |
| `gali` | 45.4 | 109.4 | 214.7 |
| `makan` | 18.9 | 15.9 | 7.1 |
| `pata` | 507.0 | 1014.1 | 493.5 |

`ghar` is about as frequent as `waqt` (time) and more frequent than `bijli`
(electricity). `makan` is the rarest of the three but attested in all three
corpora. **§3's finding does not rest on invented words** — the models are
failing on vocabulary Pakistanis demonstrably use.

All 8 name-homograph nouns from §8.4 are attested too (`noor` 55–106,
`aman` 91–151, `rehmat` 34–106 per million), so that result is not an artifact
of unusual word choice either.

### 9.2 Two genuine defects, and they inflated §8

| token | attested | note |
|---|---|---|
| `bhejunga` | **0 / 3** | not attested anywhere |
| `rakam` | 2 / 3, 0.6–1.4 pm | `raqam` is the standard form, 22–100 pm |

`rakam` also appears in a *positive* case — `probe_ner_locale.py`'s
`"Bhai {x} ko rakam bhaij di"` — so it is in the committed §2 numbers as well.
(`customer`, `invoice` and `pending` were also flagged, but those are English
loanwords, normal in code-switched Urdu and simply absent from colloquial
corpora. Not defects.)

This matters because an out-of-vocabulary token is exactly what a NER model
over-flags, and **two of §8.3's three worst false positives contain one**:
`'Kiraya agle mahine bhejunga'` and `'Rakam wapas bhej dein'` were both
redacted whole. Excluding all four affected sentences:

| PK negatives, urchade | thr 0.25 | thr 0.55 |
|---|---:|---:|
| all 25, as reported in §8 | 40% fire / 21% over-red | 24% / 14% |
| **21 fully attested** | **38% fire / 16% over-red** | **19% / 8%** |

So §8 overstated over-redaction by about a quarter. The effect survives: 38% of
clean, fully-attested Roman-Urdu memos still fire at threshold 0.25, and
`'Paisay bhej diye hain'` — every word attested in all three corpora — is still
returned whole as a PERSON. The Roman-Urdu penalty is **2× English, not 2.6×**.

### 9.3 Limits

- **Attestation is not idiomaticity.** This shows the words are real and how
  common they are. It does not test word order, agreement or register, and a
  fluent speaker reading the templates would still be worth more than this.
  §5's caveat is narrowed, not closed.
- **The corpora are social media** — reviews, comments, tweets. Transaction
  narrations are a different register, so frequency here is evidence the words
  exist, not that they are what someone writes in a bank memo.
- **Frequency is not correctness.** `rakam` is attested twice; it is still the
  wrong spelling.
- The two defects are recorded rather than silently fixed, because §2's
  committed numbers were measured on the text as it stands.

---

## 10. Which records are worth a human? Only the destroyed ones

§8 measured over-redaction and the decision that followed was to **keep
threshold 0.25** — coverage is not traded away — and mitigate the resulting
damage with a review queue instead. That converts "which records get reviewed"
into a boundary somebody has to pick, and picking it by eye would put an
invented number in the routing path.

The constraint that shapes the whole question: **at runtime there is no ground
truth.** The processor cannot know whether a masked span was a real name or a
false positive; if it could, it would not need the model. The only quantity it
can compute is how much of the memo was masked. So the boundary has to be a
function of masked fraction alone.

Measured by `scripts/probe_redaction_damage.py` at threshold 0.25 on
`urchade/gliner_multi_pii-v1`, over both corpora at once — 237 positives from
`probe_ner_locale.build_cases()` and 30 free-text negatives from
`probe_ner_precision.build_negatives()`. Both label sets run on every case,
because production runs both and a PERSON memo still absorbs whatever the
ADDRESS labels fire on.

### 10.1 Masked fraction does not separate good redaction from bad

The intuition behind a graded confidence band was that heavily-masked records
are the suspicious ones. It is false, and backwards:

| group | n | mean masked | p50 | max | untouched |
|---|---:|---:|---:|---:|---:|
| pos / address_roman_urdu | 24 | **72%** | 75% | 96% | 0% |
| pos / address_codeswitch | 24 | 65% | 71% | 79% | 0% |
| pos / address_english | 24 | 56% | 67% | 81% | 0% |
| pos / person_english | 55 | 45% | 45% | 80% | 0% |
| pos / person_roman_urdu | 55 | 38% | 33% | 62% | 0% |
| pos / person_codeswitch | 55 | 32% | 30% | 56% | 0% |
| neg / pk_ambiguous | 10 | 27% | 16% | 100% | 40% |
| neg / pk_plain | 15 | 21% | 0% | 100% | 73% |
| neg / clean_memo | 5 | 5% | 0% | 23% | 80% |

**Positives are masked more than negatives at every percentile.** That is not a
malfunction — an address genuinely is most of `"Ghar 12, Gali 4, F-8/3
Islamabad"`, so removing it correctly consumes most of the memo. Size cannot
distinguish the two because correct redaction of a short memo is, by
construction, large.

The sweep makes the cost explicit. `queue yield` is the share of the review
queue that is actually repairable damage:

| B | flagged | of pos | of neg | damage caught | queue yield |
|---:|---:|---:|---:|---:|---:|
| 0.20 | 88% | 95% | 27% | 73% | **3%** |
| 0.40 | 49% | 52% | 20% | 55% | 5% |
| 0.60 | 27% | 28% | 13% | 36% | 6% |
| 0.80 | 5% | 4% | 13% | 36% | 31% |
| 0.90 | 3% | 2% | 13% | 36% | 50% |
| **1.00** | **1%** | **0%** | 13% | 36% | **100%** |

Any graded band drowns the reviewer: at 0.20 they inspect 88% of the stream and
97% of what they see was handled correctly. A band is not a viable design here.

### 10.2 The one rule that works is a degenerate-state check

At B = 1.00 the picture inverts: 1% of records flagged, **no positives at all**,
and every flagged record genuinely damaged. The four that qualify:

```
100%  'Paisay bhej diye hain'
100%  'Kiraya agle mahine bhejunga'
100%  'Rakam wapas bhej dein'
100%  'Rehmat ho gayi hai'
```

This works because it is not a tuned threshold but a **qualitative state**:
redaction consumed the entire field and left nothing. A legitimate redaction
essentially never reaches it, because a real memo has structure around the
entity — `"Transfer to X"` still leaves `"Transfer to"`. The rule is
*"flag when redaction leaves nothing"*, which needs no calibration and cannot
drift with the score distribution the way §7.2 showed a threshold can.

The margin is thin and should be stated: `address_roman_urdu` peaks at 96%
masked, four points below the line. The rule must therefore test **full
saturation**, not `>= 0.9x` — the exactness is what makes it safe.

### 10.3 A second signal was tested and refuted

Masked fraction is blind to *partial* over-redaction, which is 64% of the
damage. So a second, independent signal was tried: characters labelled **both**
PERSON and ADDRESS. The reasoning was that the two are mutually exclusive in
reality — a run of characters is somebody's name or where they live, never both
— so overlap is the model contradicting itself rather than finding two things.

It does not work, for the same reason §10.1 does not:

| group | any conflict | mean frac |
|---|---:|---:|
| pos / address_codeswitch | **54%** | 15% |
| pos / address_roman_urdu | 46% | 15% |
| pos / person_english | 38% | **36%** |
| neg / pk_ambiguous | 20% | 13% |
| neg / pk_plain | 13% | 13% |

Conflict fires *more* on real entities than on clean text. Label confusion is a
property of the model being unsure **which** entity it found, not whether there
is one. Adding it to the rule strictly worsens the outcome — queue yield falls
from 100% to 8–14% while flagging 28% of positives:

| C | flagged | of pos | damage caught | queue yield |
|---:|---:|---:|---:|---:|
| 0.01 | 27% | 28% | 55% | 8% |
| 0.25 | 21% | 21% | 55% | 11% |
| 0.50 | 13% | 13% | 45% | 14% |
| **off** | **1%** | **0%** | 36% | **100%** |

Recorded rather than dropped: it was a plausible mechanism, and the measurement
is the only reason it is not in the router.

### 10.4 Incidental — the field scoping decision is confirmed

`pipeline_field` negatives were excluded from the boundary analysis because
Tier 2 is scoped to free text. Scored anyway, they mask **50% of characters on
average, 100% at worst**, with only 46% untouched — `channel` values like
`'atm'` come back as PERSON *and* ADDRESS. Running Tier 2 over every string
field, as `processor.py:234` does for Tier 1, would be substantially worse than
running it nowhere.

### 10.5 What this changes

- **No confidence band.** The middle-band design is dropped; §10.1 shows every
  graded boundary has 3–7% yield.
- **Flag on full saturation only**, emitted-and-flagged rather than blocked:
  the record is already safe, so diverting it costs availability for no privacy
  gain. The flag belongs in the audit row, not in a routing decision.
- **~1% of records reach review**, which is a queue a human can actually work.
- **64% of over-redaction is accepted, unflagged and invisible.** This is the
  real cost of the §8 decision to keep coverage, and it should be stated
  plainly rather than implied by the 36% figure.

### 10.6 Limits

- **30 free-text negatives, 11 of them damaged, 4 saturated.** The 100% yield
  at B = 1.00 rests on those 4. It is a small sample and the right reading is
  "no positive came close to saturation", not "precision is exactly 1.0".
- Measured at one threshold (0.25) on one model. If either changes, the
  saturation rule survives — it is scale-free — but the 36% damage-caught
  figure does not.
- Negatives are still the author's Roman Urdu, with §9's two defects present.
  Three of the four saturated records contain an attested vocabulary; one
  (`'Kiraya agle mahine bhejunga'`) contains the unattested `bhejunga`.
- **Whether a fully-masked memo is worth a human at all** is unexamined here.
  The alternative is dropping the memo and emitting the rest of the record,
  which needs no reviewer.

---

## 11. The GPU — the one lever that moved, and what it does not buy

§7 concluded that runtime optimization on CPU narrows the Tier 2 cost gap from
65× to ~23× without closing it, leaving one untested lever with the right order
of magnitude: move the model off the CPU. Measured here via
`probe_ner_runtime.py --device cuda`.

Hardware: **NVIDIA RTX 3050 Ti Laptop, 4.3 GB, sm_86**, torch 2.13.0+cu130.
A laptop part, and a weak one by GPU standards — which makes this a floor
rather than a headline.

Two measurement details that decide whether the numbers mean anything. CUDA
kernel launches are **asynchronous**, so timing a forward pass without
`torch.cuda.synchronize()` measures the time to *enqueue* it and reports a GPU
as impossibly fast; `sync()` is called after every timed call. And GPU warmup is
5× longer than CPU, because the first launches pay for context creation, kernel
autotuning and allocator growth.

### 11.1 Results

The CPU column was re-measured on the **same** cu130 wheel, so the comparison is
not confounded by the torch build changing underneath it. It came out at
40.5 ms against the 40.9 ms committed in §7 — within noise, so §7's numbers
stand.

| config | batch 1 | batch 8 | batch 32 | PERSON | ADDRESS |
|---|---:|---:|---:|---:|---:|
| CPU torch fp32 | 116.4 | 40.5 | 40.3 | 99.4% | 84.7% |
| CPU ONNX fp32 (§7) | — | **31.0** | — | 99.4% | 84.7% |
| **GPU torch fp32** | 29.4 | **8.03** | 7.64 | 99.4% | 84.7% |

**3.9× faster than the best CPU configuration, at identical accuracy.**
PERSON 0.9939 and ADDRESS 0.8466 match the CPU run to four decimals — this is
the same model arriving at the same answers sooner, not a speed-for-quality
trade. Contrast §7.2, where int8's 2× came with a collapsed score distribution.
Batch 32 was measured without OOM on 4 GB, and saturation is still at batch 8.

### 11.2 What it does to the throughput argument

At the 50% escalation floor (roughly half of memos genuinely contain a name, so
no predicate does better — §6.3), against the measured 0.69 ms/record Tier 1
baseline of 1,450 msg/s:

| config | ms/record | throughput | gap to 1,450 |
|---|---:|---:|---:|
| CPU ONNX fp32 | 16.2 | 62 rec/s | 23× |
| **GPU, 50% escalation** | 4.7 | **213 rec/s** | **6.8×** |
| GPU, 100% escalation | 8.7 | 115 rec/s | 12.6× |

The gap goes from 23× to **6.8×**. That is the first change of this whole
investigation that moves the number by an order of magnitude, and 213 rec/s is
a throughput many real deployments would accept.

### 11.3 Three things it does not buy

**The advantage is entirely contingent on batching.** At batch 1 the GPU costs
29.4 ms — statistically indistinguishable from the 31.0 ms CPU ONNX graph
*batched*. The 3.9× exists only at batch 8 and above. The processor already
consumes in batches (`--batch-size 500`), so this is reachable, but it couples
Tier 2 throughput to batch fill and adds queuing latency that the per-record
figure hides.

**The tiering claim is about cost, not latency, and this does not obviously
help it.** Tiering pays if escalating a subset to an expensive model is cheaper
than running it on everything. A GPU instance costs a multiple of a comparable
CPU instance, and if that multiple exceeds 3.9× then moving Tier 2 to a GPU is a
cost *regression* even though every latency number improves. Settling this needs
real prices for whatever the deployment target actually is — it is not
answerable from these measurements, and it should not be asserted either way
without them.

**This is a laptop GPU with nothing else on it.** No Kafka, no Postgres, no
audit writer competing for the host; a single process with exclusive use of the
card. A datacenter part would very likely be faster, but a shared one under
contention would not.

### 11.4 Limits

- One GPU, one model, fp32 only. **int8 and ONNX were deliberately skipped on
  cuda** — dynamic quantization dispatches to FBGEMM (a CPU backend,
  `gliner/model.py:614`) and the ONNX graph loads on the CPU execution
  provider, so both would have reported CPU timings under a GPU heading.
- **TensorRT and `onnxruntime-gpu` are untested**, and are the obvious next
  lever if 6.8× still is not enough.
- fp16/bf16 untested. On sm_86 that is plausibly another large factor and,
  unlike int8, is not known to break calibration — but "plausibly" is not a
  measurement.
- `torch` and `gliner` are **not declared** in `requirements.txt` or
  `pyproject.toml`; they were installed ad hoc for these probes. Correct while
  Tier 2 is unshipped, a packaging gap the moment it is not — and a GPU build
  makes the install platform-specific, which is a heavier dependency decision
  than a pure-Python one.

---

## 12. What the cost numbers were, and were not, evidence for

§6.3, §7 and §11 are written as though throughput were adjudicating the
architecture: "65× the entire budget", "the gap does not close", "tiering only
pays below ~1.55% escalation". That framing came from a decision entry titled
*"Tier assignment by cost, not by capability"* — a title that argues the
opposite of its own body, which reserves Tier 2 for *"entities with no format to
match"*. Corrected in `decisions.md` on 2026-08-06.

**Tier 2 exists because free text has no format to match.** No rule finds
`Ayesha Malik` in a memo — §2 measured that Tier 1's entire vocabulary
(CNIC, IBAN, phone, email) has nothing to say about names, and a memo produces
no findings at all rather than uncertain ones. Free-text redaction is the
product. It cannot be optimised away, because there is no cheaper thing that
does it.

So every cost figure in this document keeps its value and changes its job:

| section | measured | what it actually bounds |
|---|---|---|
| §6.3 | 44.6 ms/record | throughput with free-text redaction on |
| §7 | ONNX 31.0, int8 refuted | how much of that is recoverable on CPU |
| §11 | GPU 8.03 ms | how much is recoverable at all |

None of them is evidence that Tier 2 is the wrong design, and §7's "the gap does
not close" should be read as *"this is what the capability costs"*, not *"this
capability failed to justify itself"*. **The honest comparison is not 213 rec/s
against 1,450.** 1,450 is the throughput of a pipeline that leaks every name in
every memo — it is not a competing configuration, it is the same product with
its main function switched off. The real statement is:

> Structured identifiers cost 0.69 ms/record via rules. Free-text redaction
> costs an additional 8.03 ms/record on a GPU, 31.0 on CPU. Throughput is
> ~213 rec/s with free-text redaction enabled at the 50% memo rate, ~1,450
> without it.

That is a capability/cost curve a reader can price against their own workload,
and it is a considerably stronger claim than the tiering argument it replaces —
because it is true, and the tiering argument was undermined by §6.3 onward.

### 12.1 What this does not excuse

The reframe is not permission to stop caring about throughput. Capability that
cannot be afforded on the target stream is not capability, and 213 rec/s is a
real ceiling — a workload needing 5,000 msg/s is not served by a better
framing. §11.3's cost caveat also survives intact: if a GPU instance costs more
than 3.9× a CPU one, the GPU is a cost regression that merely looks faster.

What changes is which question those numbers answer. They size the deployment
envelope. They were never a verdict on the design.

### 12.2 Consequences elsewhere

- **The escalation predicate stops being an open question.** Dispatch is by
  field type, which the schema fixes before either detector runs, so there is
  nothing to predict. The 50% figure was only ever a problem for a cost
  argument that no longer carries the weight. Retired in `handoff.md` §8.
- **§10.4 is promoted from an aside to a reason.** Pointing Tier 2 at
  identifier fields masks 50% of their characters, so dispatch is not just
  cheaper than scanning every field — it is more correct.
- **`processor.py:234` is now wrong in a stateable way.** It runs one detector
  over every string field. The design calls for dispatch: identifiers to rules,
  free text to the encoder, `account_holder` to a schema rule with no model at
  all.

### 11.5 Correction — 8.03 ms/record was one label set, production needs two

`probe_ner_runtime.py` times a single label list (`LABELS["ADDRESS"]`). The
processor needs PERSON **and** ADDRESS, and the obvious economy — one pass over
all six labels — was measured and rejected:

| | combined, 1 pass | separate, 2 passes |
|---|---:|---:|
| PERSON coverage | 90.9% | **99.4%** |
| ADDRESS coverage | 70.5% | **84.7%** |

The labels compete for the same spans, so folding them into one call costs 8.5
and 14.2 points. Given the standing decision not to trade coverage, Tier 2 runs
one pass per label group and costs proportionally more.

Measured through `Tier2Detector.detect_batch` on the same GPU, 237 texts at
batch 8: **17.7 ms/record**, including the Python overhead of building findings.

| | ms/record | at 50% escalation | gap to 1,450 |
|---|---:|---:|---:|
| §11.2 as published | 8.03 | 213 rec/s | 6.8× |
| **actual** | **17.7** | **~105 rec/s** | **~14×** |

§11.2's table is therefore optimistic by roughly 2×. The GPU is still the lever
it was — CPU at two passes lands near 31 rec/s — but the honest gap is ~14×.

The same correction applies to §7's CPU figures, which were also single-label:
ONNX fp32's 31.0 ms is ~62 ms for a production pass.

---

## 13. ADDRESS dropped from Tier 2 — a capability with no field

> **Superseded by §18.** The generator now emits addresses, so the premise below
> no longer holds, and §18.2 found the 30% false-positive rate in §13.2 changes
> no redacted output. ADDRESS is back in `LABEL_GROUPS`. Kept as written because
> the reasoning was right for the state of the pipeline at the time.

§3 and §6.2 established the address-form penalty as the strongest finding on
this branch: −21 points for Urdu-form addresses on urchade, −18 on nvidia, two
independent models agreeing. It is also the only place the Pakistani-locale
angle is genuinely differentiated, since §2 refuted the equivalent claim for
names.

None of that is in question here. What was never checked is whether anything in
the pipeline **contains** an address.

### 13.1 Nothing does

All ten `MEMO_TEMPLATES` in `generator/transactions.py`, in full:

```
Transfer to {name}              Zakat contribution      Salary for {name}
Rent payment from {name}, ...   Utility bill payment    Eidi for {name}
Sent by {name} (CNIC {cnic})    Loan installment        Refund ... {email}
Payment against order #{inv}, contact {phone}
```

No address placeholder, and `make_transaction()` has no address field. A grep
for `address|ghar|gali|makan|sector|house` across the whole generator returns
nothing. So on this stream the ADDRESS pass has **no true positive available to
it** — every span it emits is false by construction.

### 13.2 Measured: 30% false-positive rate, mostly on names

200 generated memos, urchade at threshold 0.25:

| | fired on |
|---|---:|
| PERSON_NAME | 119 (60%) |
| **ADDRESS** | **60 (30%)** — all false |

```
'Transfer to Muhammad Gill'             -> ADDRESS 'Muhammad Gill'         0.493
'Rent payment from Eman Syed, ...'      -> ADDRESS 'Eman Syed'             0.938
'Rent payment from Hamza Butt, ...'     -> ADDRESS 'Hamza Butt'            0.973
'Sent by Rizwan Malik (CNIC 37320-...)' -> ADDRESS 'CNIC 37320-6821782-0'  0.408
```

It re-tags people's names as addresses, some at 0.97 — high enough that no
threshold change removes them. Because those spans overlap what PERSON_NAME
already found, `merge_spans` unions them and the redacted *text* is mostly
unchanged; the damage is to the audit trail, which records ADDRESS findings for
records containing no address, and to cost.

### 13.3 Cost

The ADDRESS labels were one of two forward passes. Measured by interleaving both
configurations in one process (cross-process comparison is invalid on a laptop
GPU, which throttles under sustained load — an earlier single-shot reading gave
20.7 ms for *less* work):

| config | median ms/record | samples |
|---|---:|---|
| PERSON + ADDRESS | 17.7 | 13.6–22.0 |
| **PERSON only** | **7.2** | 6.7–13.4 |

**59% saved.** More than the ~50% a pass-count argument predicts, because the
spurious spans also cost post-processing. Variance is wide in both; medians only.

Throughput, against the 0.69 ms/record Tier 1 baseline of 1,450 msg/s. Note the
escalation rate is **100%, not the 50% used in §11.2** — dispatch is on "is this
field non-empty free text", not "does it contain a name", and every template
this generator emits produces a non-empty memo:

| | ms/record | throughput | gap to 1,450 |
|---|---:|---:|---:|
| with ADDRESS | 18.4 | 54 rec/s | 27× |
| **without** | **7.9** | **127 rec/s** | **11×** |

### 13.4 What this does and does not decide

- **PERSON coverage is unchanged at 99.4%**, re-measured after the removal.
- The §3/§6.2 finding stands. It is a real property of these models, recorded
  and reproducible; it is simply not load-bearing for a stream with no
  addresses in it.
- **Restoring ADDRESS is one line** in `LABEL_GROUPS`, and a test pins its
  absence so it cannot drift back silently.
- A *declared* address field would need none of this — it is `account_holder`
  again, a schema rule at zero cost and 100% coverage. Only addresses embedded
  in free text need a model.
- Whether real Pakistani bank memos contain inline addresses is **unmeasured**,
  and is the question that decides if this ever comes back.

---

## 14. Real addresses — the model we picked is the worst one tested

§3 and §6.2 recorded the strongest finding on this branch: encoders lose ~21
points of character coverage on Urdu-form addresses, replicated across two
models. §5 recorded what sat underneath it — **the author wrote every one of
those addresses.** §13 then dropped ADDRESS from Tier 2, because nothing in the
pipeline contains one.

This scores four encoders on **7,371 real OpenStreetMap addresses** instead.

### 14.1 Method

`scripts/build_address_corpus.py` reads an Overpass export (9,446 elements,
Lahore-metro) and emits deduplicated addresses. `scripts/probe_address_real.py`
substitutes each into the *same* `ADDRESS_TEMPLATES` frames §6.2 used, so one
variable changes: the address is real instead of hand-written. The gold span
needs no alignment — the script inserts the address and knows the offsets.

300 addresses balanced across kind, through 12 frames = **3,600 cases** per
model per threshold.

**The privacy control.** Raw OSM carries real personal data: a `building=house`
node in this file holds `name="Muhammad Ibrahim"`, and 296 elements carry phone
numbers. Only `addr:*` keys are read, plus `building` as a single documented
exception — it describes a structure, not a person, and is what makes the
residential split reportable. Enforced by test, not convention.

**Two things deduplication exposed.** Karachi looked like 253 addresses; it is
`Nazimabad 5` repeated 252 times. And 101 street values already end in the city,
so appending `addr:city` produced `Lahore, Pakistan, Lahore` — an address nobody
wrote, which would have been scored as one.

### 14.2 Results

| model | thr | ALL | residential | commercial | gap | any-hit |
|---|---:|---:|---:|---:|---:|---:|
| **gliner-community/gliner_medium-v2.5** | 0.25 | **85.8%** | **84.1%** | 88.4% | 4.3 | 99.6% |
| nvidia/gliner-PII | 0.25 | 80.2% | 79.5% | 82.5% | **3.0** | 98.9% |
| urchade/gliner_multi_pii-v1 | 0.25 | 75.9% | 70.0% | 82.4% | 12.4 | 99.2% |
| xlm-roberta-large-conll03 | 0.5 | 45.0% | **27.4%** | 72.6% | **45.2** | 98.3% |

**`urchade/gliner_multi_pii-v1` — the model this project selected and shipped —
is the worst of the three GLiNER checkpoints on real addresses**, and has the
largest residential penalty of the three by a factor of three.

That selection was not wrong on its own terms. §6.1 chose urchade on PERSON
coverage (99.4% against nvidia's 91%), cost, and licence. Nothing here disturbs
those. It does mean the choice was never tested on the axis that matters for
addresses, because §13 had removed addresses from the pipeline.

### 14.3 The finding that matters: residential is harder than commercial

Every model is worse on homes than on businesses. A home address is personal
data. A shop's address largely is not. **The models are weakest on precisely the
category that is PII.**

The size of that penalty is a property of the model, not of the task:

- `nvidia` 3.0 points, `gliner_community` 4.3 — small.
- `urchade` 12.4 — four times either.
- `xlm-roberta-conll03` 45.2 — the model is close to useless on homes.

The XLM-R number has a mechanical explanation, and it is worth stating because
it generalises. CoNLL03 offers `PER/ORG/LOC/MISC` and **has no address class**.
A commercial address is caught as `ORG`, because it is a business. A residential
address has no label that fits. A general-purpose NER model is not a weak
address detector — for homes it is barely a detector at all.

### 14.4 Coverage is not the problem; completeness is

Every model finds *something* in nearly every address:

```
gliner_community   coverage 85.8%   any-hit 99.6%
urchade            coverage 75.9%   any-hit 99.2%
xlmr_conll         coverage 45.0%   any-hit 98.3%
```

xlm-roberta locates 98.3% of addresses and covers 45% of their characters. This
is §3's failure mode on real data: **it finds the city and leaves the house
number in the clear.** A pipeline shipping that looks like it redacted the
address. A total miss would at least be visible.

### 14.5 A §6.2 caveat, retired

§6.2 recorded that urchade scores *higher* on Roman-Urdu frames than English,
called it "not explicable", and attributed it to noise at n=12. It replicates at
n=1,200 on addresses neither of us wrote:

| model | english | codeswitch | roman_urdu |
|---|---:|---:|---:|
| gliner_community | 87.9% | 84.8% | 84.7% |
| nvidia | **90.9%** | 75.3% | 74.4% |
| urchade | **69.6%** | 74.7% | **83.4%** |
| xlmr_conll | 44.9% | 45.0% | 45.0% |

urchade rises 13.8 points into Roman Urdu; nvidia falls 16.5. §6.2's conclusion
— that the sentence-language effect does not replicate *across* models — stands.
Its dismissal of urchade's direction as noise does not. I still have no
explanation, and should stop offering one.

### 14.6 What this says about fine-tuning

The question was whether the address gap justifies fine-tuning an encoder. On
this evidence, **largely not yet.**

An off-the-shelf model nobody had tried reaches **85.8% overall and 84.1% on
residential**, ten points above the model in use. The obvious cheap move is to
evaluate `gliner_community` properly — on PERSON as well, at its own swept
threshold, and for cost — before training anything. Fine-tuning to recover
ground that a checkpoint swap already covers would be effort spent on the wrong
problem.

What would still justify fine-tuning: 84% is not 99%, and the residual is
concentrated in house numbers, which are the identifying part.

### 14.7 Limits

- **Lahore-metro only.** 8,466 of 9,446 elements. Islamabad sector forms
  (`F-8/3`) are 0.3% of the corpus, so the controlled comparison against §6.2 —
  whose addresses were Islamabad sectors — **cannot be made from this data.**
  Comparing these numbers to §6.2's changes provenance, city and convention at
  once. Model-against-model here is controlled; the §6.2 comparison is not.
- **78.7% of the corpus has no building type**, so the residential/commercial
  split rests on 840 and 713 addresses respectively, not on the full 7,371.
- **Sentence frames are still hand-written.** The addresses are independent;
  the surrounding memo text is not. Half-independent, not fully.
- 51 Urdu-script addresses were excluded. Script coverage is a separate
  question and would confound the residential comparison.
- `xlm-roberta-large-conll03` is scored at a single threshold. Its scores are
  real probabilities and do not respond to GLiNER's cutoffs.
- Both `gliner_community` and `xlm-roberta` failed on first run with network
  errors, not model errors. Re-run before trusting any single number here.

---

## 15. A checkpoint swap beats fine-tuning, and §14 was measured on too little

§14 scored four encoders on 7,371 Lahore addresses from a Kaggle export. This
re-runs it on **91,663 addresses across eight cities**, fetched from Overpass
directly, and adds the PERSON axis §14 never measured.

Two of §14's claims do not survive. Both were mine.

### 15.1 What changed about the corpus

The Overpass fetcher failed three times in ways that returned **HTTP 200 with a
plausible result** rather than an error:

- `area["name"="Islamabad"]` matched nothing — Pakistani administrative
  boundaries are named in Urdu script (`اسلام‌آباد`, `پنجاب`).
- `area["name:en"=...]` resolved, but also matched three `Islomobod` hamlets in
  Central Asia, and adding `["boundary"="administrative"]` matched nothing at
  all, because Overpass areas do not carry that tag.
- Scanning a resolved area for every `addr:street` node timed out with 504.

Bounding boxes avoid area resolution entirely. But the first set was written
**from memory, and six of eight clipped their city** — a failure that is
invisible, because a small box returns fewer addresses rather than an error.
Islamabad's 5,331 elements looked entirely reasonable. Boxes now come from
Nominatim's administrative boundary, checked by `scripts/verify_bboxes.py`,
which also rejects point results: Nominatim resolves Multan and Quetta to an
office node and a railway station, and a point fits inside any box.

| | §14 (Kaggle) | §15 (8 cities) |
|---|---:|---:|
| raw elements | 9,446 | **150,504** |
| unique addresses | 7,371 | **91,663** |
| residential | 840 | 30,325 |
| commercial | 713 | 24,833 |
| sector-code forms | **19** | **2,592** |

### 15.2 A wiring bug that would have published a false result

The first PERSON run reported urchade at **8.4%**, against the 99.4% §2
measured. The tell was in the bucket names: it reported `kind:residential` and
`form:sector_code` at n=3600. Those are *address* cases. The patch had switched
the labels to PERSON and left the corpus on addresses, so it searched 3,600
street addresses for people's names.

Read at face value it says these models cannot find names. The probe now prints
`entity=` and its bucket names on every run, so the mismatch announces itself.

### 15.3 PERSON — 165 synthetic name cases

| model | ALL | common | rare | ambiguous |
|---|---:|---:|---:|---:|
| **gliner-community/gliner_medium-v2.5** | **100.0%** | 100.0% | 100.0% | 100.0% |
| urchade/gliner_multi_pii-v1 | 99.4% | 100.0% | 97.8% | 100.0% |
| xlm-roberta-large-conll03 | 93.8% | 95.8% | **84.4%** | 99.8% |
| nvidia/gliner-PII | 91.1% | 91.4% | 92.6% | 88.9% |

Names stay synthetic on purpose. `decisions.md` §1 forbids processing real
personal data, so unlike addresses there is no honest way to source these
externally. **These numbers are comparable to §2, not independent of it**, and
that is a real limit on what this table proves.

### 15.4 ADDRESS — 91,663 real addresses

| model | ALL | residential | commercial | sector_code | plain_street |
|---|---:|---:|---:|---:|---:|
| **gliner_medium-v2.5** | **86.5%** | 87.2% | 84.8% | **84.3%** | 86.8% |
| urchade | 75.3% | 73.7% | 73.7% | 66.3% | 77.3% |
| nvidia | 74.7% | 78.5% | 65.4% | **57.9%** | 82.4% |
| xlmr_conll | 52.3% | 48.3% | 55.6% | 51.7% | 60.0% |

### 15.5 §14.3 was wrong

§14.3 said *"every model is worse on homes than on businesses"* and called it
the finding that matters. On ten times the data it does not hold:

| model | §14 gap | §15 gap |
|---|---:|---:|
| urchade | 12.4 | **−0.1** |
| nvidia | 3.0 | **−13.0** |
| gliner_community | 4.3 | **−2.4** |

The gap vanishes for urchade and reverses for the other two. §14.3 was an
artifact of an 840-address Lahore-only sample.

**The replacement needs its own caveat.** The scored sample is 251 of 300 from
Karachi, because Karachi is 75,693 of 91,663 addresses. So "residential is not
harder" is a statement about Karachi, not Pakistan. Swapping one over-claim for
another would repeat the mistake rather than fix it.

What holds across *both* corpora is the **address-form penalty**, now measured
on 2,592 real sector-code addresses rather than 19:

```
nvidia            plain 82.4%  ->  sector 57.9%   (-24.5)
urchade           plain 77.3%  ->  sector 66.3%   (-11.0)
gliner_community  plain 86.8%  ->  sector 84.3%   ( -2.5)
```

That is §3 and §6.2's finding, confirmed on addresses nobody here wrote.

### 15.6 §6.2's style divergence replicates

| model | english | codeswitch | roman_urdu |
|---|---:|---:|---:|
| gliner_community | 87.3% | 86.0% | 86.1% |
| nvidia | 83.0% | 72.3% | **68.9%** |
| urchade | **67.4%** | 77.0% | 81.6% |

urchade rises 14.2 points into Roman Urdu; nvidia falls 14.1. §6.2 saw this,
called urchade's direction "not explicable", and attributed it to noise at
n=12. It replicates at n=1,200 on real addresses. The cross-model conclusion
stands; the dismissal does not. I still have no explanation.

`gliner_community` is flat across all three, which is its own kind of evidence.

### 15.7 Cost

Measured through `Tier2Detector.detect_batch` on the same GPU, PERSON labels
only, interleaved medians over 5 runs:

| model | ms/record | vs urchade |
|---|---:|---:|
| urchade | 6.6 | 1.00× |
| gliner_medium-v2.5 | **7.3** | **1.11×** |

**11% more expensive for +11 points of ADDRESS coverage and +0.6 on PERSON.**

### 15.8 What this decides

**Fine-tuning is not justified.** The question was whether the address gap
warranted training a model. An off-the-shelf checkpoint nobody had tried
reaches 100% on PERSON and 86.5% on ADDRESS for 11% more compute — no training,
no synthetic-data pipeline, and none of the eval-set circularity that would
make a fine-tuned result hard to trust.

The honest reading of §14 and §15 together is that **the model selection in §6.1
was made on the wrong axis** — PERSON coverage and cost, at a time when ADDRESS
had been removed from the pipeline. It was not wrong; it was under-tested.

Before switching `TIER2_MODEL`, two things are still unmeasured:

- **Precision.** §8 measured over-redaction for urchade only. A model that
  covers more characters may also destroy more clean text, and §8 is the
  measurement that caught int8 doing exactly that.
- **Threshold.** 0.25 was swept for urchade. §6.1 established thresholds do not
  transfer between checkpoints, and `Tier2Detector.load()` now warns when the
  model does not match the one the threshold was tuned for.

### 15.9 Limits

- **83% Karachi.** Every conclusion here is weighted toward one city's
  conventions. Islamabad contributes 2,407 addresses and Multan, Peshawar and
  Quetta under 300 combined.
- PERSON is synthetic and shares its corpus with §2.
- Sentence frames remain hand-written; only the addresses are independent.
- `xlm-roberta-large-conll03` has no address class, so its ADDRESS numbers
  measure a label mismatch rather than the model. Its **PERSON** number, where
  `PER` is a genuine match, is the fair test of it — and 84.4% on rare names is
  the weakest cell in that column.
- Both non-baseline models failed on first run with network errors. Re-run
  before trusting any single figure.

---

## 16. The decision: swap the checkpoint, do not fine-tune

§15 recommended `gliner-community/gliner_medium-v2.5` on coverage alone. Two
things then changed that recommendation's basis: a precision probe, and a code
review that invalidated one of §15's claims outright.

### 16.1 §15's sector-code claim is withdrawn

§15.5 said the form penalty was *"now measured on 2,592 real sector-code
addresses rather than 19"*. **That is false and is withdrawn.** The corpus holds
2,627 sector-code addresses; the scored sample held **13**. `load_corpus`
stratifies by `kind`, so a form at 2.9% prevalence was barely sampled.

This is the same error §14.3 was corrected for — a headline resting on a sample
too small to carry it — committed one section later, in the correction itself.
`--stratify form` now exists so a claim about forms is measured on forms.

Four other defects were fixed in the same pass, three of which were silently
distorting these tables:

- **Deduplication kept the first duplicate, not the best-typed one.** Overpass
  returns a node and a way for the same building; the node carries only
  `addr:*`, the way carries `building=house`. The same address classified
  `unknown` or `residential` by arrival order. 528 records reclassified.
- **`SECTOR_CODE` lacked `re.I`** while `BLOCK_FORM` had it, so `f-8/3` was
  filed as `plain_street` — contaminating both cells of the comparison it feeds.
- **The duplicate-city guard was a substring test**, so `Multan Road` + city
  `Multan` yielded an address with no city.
- **The fetcher treated every HTTP 200 as success.** Overpass reports a
  server-side timeout in a `remark` field with a truncated element list. §15.1
  names this exact failure shape as the one that cost three iterations; this was
  a fourth instance, in the same file.

### 16.2 Precision, which reverses §15 at its own threshold

At 0.25 — urchade's operating point — `gliner_community` is **worse**, not
better. Over 25 clean Pakistani memos containing no PII:

| model | thr | fires | over-redaction |
|---|---:|---:|---:|
| urchade | 0.25 | 40% | 21% |
| **gliner_community** | **0.25** | **76%** | **32%** |
| gliner_community | 0.40 | 36% | 11% |
| **gliner_community** | **0.55** | **20%** | **6%** |
| gliner_community | 0.70 | 4% | 1% |

§15's 11-point coverage advantage at 0.25 was bought by flagging nearly
everything — the §7→§8 int8 pattern exactly. Recommending the swap on §15's
numbers alone would have shipped a worse model.

The thresholds do not transfer (§6.1), and that is the whole point: at **0.55**,
where this checkpoint actually operates, it dominates instead.

### 16.3 The comparison at each model's own operating point

Form-stratified, 100 addresses per form, 1,200 cases per bucket:

| | urchade @ 0.25 | gliner_community @ 0.55 |
|---|---:|---:|
| ADDRESS coverage | 74.0% | **79.0%** |
| PERSON coverage | 99.4% | 99.4% |
| sector-form penalty | 11.2 pts | **1.5 pts** (at 0.25) |
| fires on clean PK memos | 40% | **20%** |
| over-redaction | 21% | **6%** |
| cost | 6.6 ms/record | 7.3 ms/record |

**Better coverage, equal on names, half the false positives, one third the
over-redaction, for 11% more compute.** That is a Pareto improvement, not a
trade.

Form-stratified coverage at 0.25, which is where the form penalty is clearest:

| model | ALL | sector | block | plain | penalty |
|---|---:|---:|---:|---:|---:|
| **gliner_community** | **85.3%** | **84.2%** | 86.0% | 85.7% | **+1.5** |
| nvidia | 75.5% | 63.7% | 75.8% | 86.9% | +23.2 |
| urchade | 74.0% | 66.5% | 77.6% | 77.7% | +11.2 |
| xlmr_conll (0.5) | 52.6% | 51.5% | 48.4% | 57.7% | +6.2 |

`gliner_community` is the only model without a meaningful form penalty. Against
nvidia's 23.2 points that is a sharper claim than "it scores higher", and it is
what would matter for Islamabad sector addresses.

Also corrected: urchade at 0.55 collapses to **39.2%** on a form-balanced
sample, not the 42.0% §15 reported from a kind-balanced one.

### 16.4 Arguments for fine-tuning

Recorded because the case is not empty, and a decision that only lists one side
is not a decision.

- **79% is not 99%.** PERSON sits at 99.4%; ADDRESS at 79.0% is twenty points
  worse on an entity type that is equally identifying.
- **The residual is concentrated in the identifying part.** §14.4 measured
  any-hit at ~99% against coverage at ~85%: the models nearly always find the
  city and miss the house number. A partially-redacted address looks redacted
  and is not.
- **A training corpus now exists.** 91,675 real addresses with independent
  provenance, and gold spans that need no alignment because the harness inserts
  them. The circularity objection that blocked this in §13 is answered.
- **Karachi plot numbers are the weak cell** — 83.4% against 92.7% on Karachi
  plain streets. The Islamabad sector convention is 94.4% (§19.3).
- **Nobody else has this.** A Pakistani-locale address model is the one thing
  in this project that cannot be obtained by picking a better checkpoint.

### 16.5 Arguments against, which currently win

- **A checkpoint swap already recovers most of the gap**, today, for 11% more
  compute and zero training.
- **The eval set would have to be split.** Training and evaluating on the same
  91,675 addresses proves nothing. Held-out Islamabad-only evaluation is
  possible but leaves ~2,400 sector addresses to both train and test on.
- **83% of the corpus is Karachi.** A model fine-tuned on it would learn
  Karachi conventions and be evaluated on them. That is the §14.3 mistake with
  a training loop attached.
- **The generator still contains no addresses.** §13 removed ADDRESS from Tier 2
  because nothing in the pipeline has one. Fine-tuning an address model before
  the pipeline carries addresses is building a capability with no field.
- **Precision is unmeasured for a fine-tuned model** and is where the last three
  reversals came from — int8 in §8, gliner_community at 0.25 in §16.2.
- **Cost is not free.** Tier 2 is already the dominant per-record cost at
  7.3 ms against Tier 1's 0.69.

**Verdict: do not fine-tune yet.** Take the checkpoint swap. Revisit if
addresses enter the pipeline as a real field, and only with a genuinely held-out
evaluation set — ideally a city the model never trained on.

### 16.6 What changed in the code

`config.py` now defaults to `gliner-community/gliner_medium-v2.5` at threshold
**0.55**, and `_TUNED_FOR` in `tier2_encoder.py` moves with it so the
mismatch warning stays truthful. Both remain overridable by environment
variable, and §6.1's rule holds: changing `TIER2_MODEL` without re-sweeping the
threshold produces results that are not comparable.

### 16.7 Limits

- **Over-redaction rests on 25 hand-written memos** — the §8 corpus, in which §9
  found two vocabulary defects. It is the weakest evidence in this section and
  it is carrying the threshold choice.
- **83% Karachi.** Every address number here is weighted toward one city.
- **PERSON is synthetic** and shares its corpus with §2, so those numbers are
  comparable to §2 rather than independent of it.
- **0.55 is a judgement, not an optimum.** 0.70 gives 4% fire and 1%
  over-redaction for 68.6% coverage. If over-redaction matters more than address
  completeness, that is the better point.
- ADDRESS remains **out of Tier 2** (§13). This section changes which checkpoint
  runs, not which entity types it looks for. **Superseded by §18.2**, which
  restores it after finding that its false positives change no output.
- **The coverage numbers above are raw**, and §17.1 shows the raw metric counts
  separators between adjacent spans as leaks. Effective coverage is ~10 points
  higher for every model here.

---

## 17. Separators are not leaks, and the residual is interior

§14.4 recorded the shape of the address problem — any-hit ~99% against coverage
~85% — and left the question that decides what to do about it unanswered: *where
inside the address are the missed characters?* Leading and trailing characters
are recoverable by a rule. Interior ones are not, and are the honest case for
fine-tuning.

`scripts/probe_address_residual.py` measures the split. It was written and run
**before** the rule in §18.4, deliberately: a rule designed against an assumed
failure mode measures its own assumption.

3,600 cases — 300 OSM addresses, form-stratified, each through 12 sentence
frames — against `gliner-community/gliner_medium-v2.5` at 0.55.

### 17.1 A metric defect that runs through §3, §6.2, §14, §15 and §16

When the encoder returns two adjacent spans for one address, `char_coverage`
scores the `', '` between them as missed. Redaction then emits
`[ADDRESS], [ADDRESS]`. A comma survives. Nothing identifying does.

Measured: **53% of interior misses are punctuation and whitespace alone**, and
21,430 separator characters in this run were scored as leaks.

| metric | coverage |
|---|---:|
| raw — every gold character, the §14–§16 metric | 77.7% |
| **effective — alphanumeric characters only** | **88.3%** |

Every coverage number in §14, §15 and §16 is a raw number and understates the
models by roughly ten points. The **rankings** between models mostly survive —
all four pay the same penalty — but the distance to 100% does not, and that
distance is what §16.4's first argument for fine-tuning was built on.

This is the third measurement in this project reversed by measuring the same
thing a second way.

### 17.2 The residual is interior

Of the missed *identifying* characters, 14,810 of 126,312:

| where | chars | share |
|---|---:|---:|
| leading — the house or plot number | 3,972 | 26.8% |
| trailing — the city | 294 | 2.0% |
| **interior — a gap in the middle** | **10,073** | **68.0%** |
| never found at all | 471 | 3.2% |

So a span-extension rule has a **ceiling of 28.8%** of the residual: it could
take effective coverage from 88.3% to at most 91.7%. My hypothesis going in —
that the residual was mostly the dropped house number — was wrong in aggregate.

It is not wrong everywhere. By form:

| form | effective | leading | trailing | interior |
|---|---:|---:|---:|---:|
| plain_street | 93.5% | **64.1%** | 0.9% | 28.0% |
| block_phase | 89.9% | 18.7% | 6.1% | 73.3% |
| sector_code | 84.7% | 23.0% | 0.3% | 73.6% |

On plain streets the residual **is** the house number, and a rule reaches most
of it.

### 17.3 The 26.8% is the most identifying 26.8%

Aggregate share is the wrong way to weigh this. What survives, verbatim
(`·` = redacted):

```
'R1525, FB Area Block 3, Karachi'   ->  R1525, ·······················
'68, Model Town Block G, Lahore'    ->  68, ··························
'D 5, Block 10-A, ...'              ->  D 5, ·························
```

The street and city are redacted; the plot number is not. `FB Area Block 3` is a
district of some 40,000 people. `R1525` selects one house in it. A partly
redacted address looks redacted and is not, and the part that survives here is
the part that identifies.

Interior gaps are a different failure and mostly land on degenerate OSM records
— `'Block 5-E Block 5 E Block 5 Nazimabad'` is malformed data, not an address
anyone wrote.

---

## 18. ADDRESS returns to Tier 2, with a rule for the house number

### 18.1 The generator now emits addresses

§13 removed ADDRESS because nothing in the stream contained one. That made the
capability untestable end to end and the fine-tuning question unanswerable, so
`generator/addresses.py` closes the premise rather than arguing with it.

Place names are real, from the OSM corpus. **House and plot numbers are always
random.** That split is the whole privacy argument and it is asserted in tests:
a place name is geography, the number attached to it is what identifies a
dwelling, and `decisions.md` §1 requires the generator to generate rather than
replay. Hand-writing a fresh set would have rebuilt §5's circularity one layer
down.

`ADDRESS_MEMO_RATE = 0.30` of non-blank memos, so the default mix is 40% blank,
18% address-bearing, 42% other narration.

### 18.2 §13.2's false-positive rate is withdrawn

Measured on the shipped path — `Tier2Detector`, not a reimplementation — over
400 address-bearing and 400 address-free memos:

| | ADDRESS | PERSON | incremental over-redaction | cost |
|---|---:|---:|---:|---:|
| PERSON labels only | 24.1% | 99.2% | — | 1.00x |
| **+ ADDRESS group** | **98.5%** | **100.0%** | **0.0%** | ~2x |
| all labels, one pass | 98.0% | 98.4% | 0.0% | 1.10x |

The ADDRESS labels **do** fire on ~50% of address-free memos, which is roughly
what §13.2 reported. But every character they claim was already claimed by
PERSON or is intended PII, so `merge_spans()` unions them and the redacted
output is **byte-identical**. §13.2 counted the firing and not the effect.

*A false positive that changes no output is not a cost.* The only real price is
one extra forward pass.

### 18.3 One pass or two

§13 recorded combining all labels into one pass as dropping PERSON 99.4% → 90.9%.
**That does not replicate here**: 100.0% → 98.4%, for 45% less compute — another
instance of §6.1's rule that nothing transfers between checkpoints.

Two passes still ship. PERSON is the entity this pipeline exists for, and 1.6
points of it is not worth ~7 ms. **This is a live trade to revisit if throughput
binds** — it is the cheapest 2x available.

### 18.4 The span-extension rule

`extend_address_span()` widens an ADDRESS finding over its leading house number
and its trailing city — the two positional components §17.2 identified.

| | before | after |
|---|---:|---:|
| OSM corpus, effective coverage | 88.3% | **90.0%** |
| OSM corpus, addresses **fully** redacted | 62.4% | **74.3%** |
| generated stream, ADDRESS coverage | 98.5% | **99.9%** |
| incremental over-redaction, address-free memos | — | **0.0%** |

The second row matters most for a firewall: the share of addresses leaving with
*nothing* identifying intact rises by twelve points.

By form, effective coverage: plain_street 93.5% → **96.3%**, block_phase 89.9% →
**91.4%**, sector_code 84.7% → **86.2%**. It delivered 1.7 of the 3.4 points
§17.2 said were available, and the shortfall is deliberate conservatism.

The rule requires a digit, so `'qadri manzil'` is left alone rather than guessed
at — a building name can carry a person. It extends one token per side, because
an unbounded walk swallows the invoice number in `'Payment against order #4821,
House 12, Model Town'`.

Three defects the tests caught in the rule as first written:

- a bare word boundary let the trailing-city match eat `Sialkot` out of
  `'Model Town, Sialkot Road'` — in Pakistan a city name is a road name more
  often than not
- the optional letter prefix, which exists for `R1525` and `D 5`, had no left
  boundary and matched the final `o` of `'Statement to 14, Islamabad'`
- a single-comma anchor could not reach back over OSM's doubled separators

### 18.5 What this does to the fine-tuning question

§16.5 recommended against fine-tuning. That holds, and two of its reasons are
now stronger while one is gone.

**Gone:** "the generator contains no addresses, so fine-tuning would build a
capability with no field." It has a field now.

**Stronger:** §16.4's lead argument was "79% is not 99%". At the corrected metric
with the rule applied it is **90.0% against 100.0%** — the same gap, ten points
narrower, closed without training anything.

**Stronger:** the corpus is thinner than 91,675 suggests. It holds **17,177
distinct street-tails**, of which Karachi contributes 15,236; `'PECHS Block 2,
Karachi'` appears 2,655 times with a different number in front. As an evaluation
set 91,675 is honest. As a training set it is ~17k contexts, 83% one city.

**And the sector-code finding needs qualifying again.** The `sector_code` bucket
mixes two unrelated patterns: 55% match mid-string (the Islamabad `F-6/3`
convention) and 45% at position 0 (Karachi plot numbers — `A-50`, `R-31`,
`C-34`). By city it is 1,910 Karachi against 548 Islamabad. Every claim in §15
and §16 about sector codes being weakest is measured on a bucket that is mostly
Karachi plot numbers. Splitting the bucket is the next measurement, not a
conclusion.

The residual remaining after §18.4 is **74.6% interior**. That is the part no
rule reaches and the only honest argument left for fine-tuning — and it cannot
be tested, because the only city with the Islamabad convention in any volume is
Islamabad itself (548 addresses). Hold it out and you train with almost no
examples of the form you want to fix; keep it in and there is no held-out
evaluation. Lahore has 12 such addresses and Rawalpindi 11.

**Verdict unchanged: do not fine-tune.** Not because the gap is closed, but
because the corpus cannot support the experiment that would prove it closed.

### 18.6 Limits

- **The generated addresses are easier than real ones.** 99.9% coverage on the
  stream against 90.0% on OSM. The generator does not reproduce OSM's doubled
  commas, repeated block names or ALL-CAPS shop descriptions, so the stream
  number is an upper bound and the corpus number is the one to trust.
- **`CITIES` is a fixed list of eleven.** An address in a city not on it loses
  the trailing-city extension silently. It is locale knowledge of the same kind
  Tier 1 already encodes, and it degrades toward under-extension rather than
  over-redaction.
- **Cost figures fluctuate ±15% between runs** on this GPU. Treat the ~2x
  multiplier as the result, not the millisecond values.
- **Over-redaction is still measured on generated memos**, and §16.7's warning
  stands: the hand-written precision corpus is the weakest evidence here.
- **The interior residual is unaddressed** and this section does not pretend
  otherwise.

---

## 19. The sector-code weakness was Karachi plot numbers

§15.5, §16.4 and the Monday recap all carried a version of the same claim:
sector-code addresses are the form encoders handle worst. §16.4 used it as an
argument for fine-tuning — *"sector forms stay weakest even for the best model,
74.0% at 0.55 against 82.0% on plain streets."*

The claim was measured on a bucket holding two structurally different things.

### 19.1 One regex, two conventions

`SECTOR_CODE` matches `[A-Z]-\d+(/\d+)?`. In this corpus that catches:

```
plot_number   'A-103, Block S North Nazimabad Town, Karachi'
sector_code   '14, Hill Road, F-6/3, Islamabad'
```

The first is a plot or flat designator in the leading component. The second is
the Islamabad sector convention in a later one. 45% of the old bucket was the
first kind, and by city it was 1,910 Karachi against 548 Islamabad.

`classify()` now splits them by **position**. The corpus gains a `plot_number`
form; `block_phase` and `plain_street` are untouched, so every number §15 and
§16 reported for those still compares.

### 19.2 Position is not convention, so the probe reports city as well

Splitting by position does not by itself separate the conventions, and the
letters say why:

```
mid-string codes, Islamabad   F 275   I 161   G 66   H 10   E 3
mid-string codes, Karachi     A 112   B 106   G 70   D 52   R 44   C 44   F 28
```

Islamabad's mid-string codes are the sector alphabet. Karachi's are spread
across the alphabet and are flat or shop designators — but Karachi *also* uses
G, F and E, so no letter rule separates them either. Only city does, which is
why `probe_address_residual.py` now reports city and form together rather than
trusting `classify()` to carry the distinction alone.

### 19.3 The finding reverses

800 addresses, form-stratified, 9,600 cases, `gliner_medium-v2.5` at 0.55.
Effective coverage (§17.1), no extension rule:

| cell | n | raw | **effective** |
|---|---:|---:|---:|
| **Islamabad / sector_code** | 1,200 | 80.7% | **94.4%** |
| Karachi / plain_street | 1,824 | 81.1% | 92.7% |
| Karachi / block_phase | 2,112 | 80.8% | 91.6% |
| Karachi / plot_number | 2,100 | 73.9% | 83.4% |
| Karachi / sector_code | 1,128 | 71.6% | 81.9% |
| *the old mixed bucket* | — | *74.6%* | *85.9%* |

**The Islamabad sector form is among the strongest cells measured.** It is the
form §3 built the entire address finding on, and the weakness attributed to it
belongs to Karachi's plot and flat designators.

With the span-extension rule applied, by form:

| form | raw | effective |
|---|---:|---:|
| plain_street | 91.5% | 95.7% |
| block_phase | 89.5% | 93.1% |
| sector_code | 82.1% | 87.4% |
| plot_number | 79.4% | 85.3% |

Aggregate: 89.4% effective, 73.0% of addresses fully redacted. **Those
aggregates are not comparable to §18.4's**, because the sample now spans four
buckets rather than three and the new bucket is the weakest; the per-cell
numbers are the ones that carry.

### 19.4 What is withdrawn

- §15.5's and §16.4's "sector forms stay weakest" — withdrawn. It measured
  Karachi plot numbers.
- The Monday recap's fine-tuning argument built on it — withdrawn with it.
- §16.7's "83% Karachi" limit is *reinforced*: the mixed bucket was a case of
  Karachi's composition being read as a property of a form.

This is the fourth result in this project reversed by measuring the same thing a
second way, and the second where the first measurement was mine.

### 19.5 A defect that destroyed the corpus twice

`build_address_corpus.py` prints Urdu-script sample addresses at the end of its
report, which raises `UnicodeEncodeError` on a cp1252 console — and `report()`
ran **before** the file was written, so the whole build was discarded each time.

It happened twice before being noticed. The first traceback scrolled past under
a `head` pipe; the stale corpus on disk looked current, and a full probe ran
against it and produced a three-bucket result that was read as real.

The write now happens first and stdout is reconfigured to UTF-8. The file is the
deliverable; the report is a convenience.

---

## 20. Pinning the checkpoint, and making Tier 2 deployable

§6.1 established that a threshold does not transfer between checkpoints, and
§16 chose `gliner-community/gliner_medium-v2.5` at 0.55 on that basis. Neither
section noticed that the checkpoint was never actually fixed.

### 20.1 A model repo is a git repo

`GLiNER.from_pretrained(model_id)` with no `revision` resolves `main`. That is a
moving pointer:

```
88c3b98b57ad  2026-04-28  add fp16 and bf16 variants
ed16f26c9374  2024-06-18  Update README.md
```

The weights changed in April. The name `gliner-community/gliner_medium-v2.5` did
not. `_TUNED_FOR` compares the **name**, so it would have stayed silent: the
pipeline would run an uncalibrated cutoff against weights nobody swept it
against, and the audit would look normal.

`TIER2_MODEL_REVISION` now pins `88c3b98b57ad5e7d66fb209ed61c53f4b1fd05da` — the
commit every number in §14–§19 was measured against. The repo carries no tags,
so a commit hash is the only immutable identifier available.

Verified end to end: the pinned SHA returns `200` from the hub, and a
non-existent one raises `RevisionNotFoundError` rather than silently loading
`main`. A pin that is not enforced is worse than none.

`resolved_revision()` drops the default pin when a different model is chosen — a
commit hash belongs to one repo, and carrying it across would fail the load on
a config the operator never set. An explicitly supplied revision is always kept.

### 20.2 The pin is not total

GLiNER resolves its base ~~tokenizer~~ **architecture config** from
`microsoft/deberta-v3-base` at `main`, a different repo the revision above
cannot reach. Pinning fixes the weights, not every byte the load touches.

*Corrected in §25.1: it is the config, not the tokenizer — the checkpoint ships
its own tokenizer. §25 closes the gap with a prefetch plus offline mode, and
`load()` now logs which of the two states it is in.*

Only a warm cache plus `HF_HUB_OFFLINE=1` closes that, and compose documents it.
Verified: with the volume warm, the container loads and detects with
`HF_HUB_OFFLINE=1` set, in **10 seconds and zero network calls**.

Stating the gap is worth more than the pin: a pin believed to be total is how a
silent change gets attributed to something else.

### 20.3 Tier 2 could not run in Docker at all

`torch` and `gliner` lived only in `requirements.txt`, which the Dockerfile
never reads — it runs `pip install .` from `pyproject.toml`. The image had no
encoder. It started only because `TIER2_ENABLED` defaults to false; setting it
true died on an import three frames deep.

| | |
|---|---|
| `[tier2]` extra | `pip install '.[tier2]'` |
| Build arg | `--build-arg INSTALL_TIER2=true`, default false |
| torch source | the CPU index explicitly — pip's default serves a multi-gigabyte CUDA build this image has no GPU for |
| Image size | 257 MB base, **1.81 GB** with Tier 2 |
| Cache | `HF_HOME=/models` on a named volume — 1.6 GB, so a restart does not re-fetch |
| Warm start | 10 s, offline-capable, against ~14 min cold |
| Failure mode | asking for Tier 2 without the extra now names the fix |

The cache volume matters more than it looks. `restart: unless-stopped` is what
makes the crash-and-replay design real; without a warm cache, one network
outage turns that restart policy into a download loop.

### 20.4 Limits

- **The image is 7x larger with Tier 2**, which is why it is opt-in rather than
  default. A GPU image would be larger again and is not built here.
- **`HF_HUB_OFFLINE=1` is documented, not default.** The first run must
  download, so defaulting it on would break a clean start.
- **Nothing verifies the weights against a checksum after download.** The
  revision is the hub's guarantee, and this project takes it on trust.

---

## 21. Fine-tuning: the decision, with the evidence that decides it

§16.5 recommended against fine-tuning on five grounds. §18.5 noted one had
disappeared and two had strengthened. §19 then removed the strongest remaining
argument for it. This settles the question with four new measurements.

### 21.1 Two thirds of the residual is malformed OSM data

800 addresses, `gliner_medium-v2.5` at 0.55 with the span rule, classified by
whether the record is something a person would write. A record is malformed if
it has an empty comma component, runs past 80 characters, repeats a two-word
phrase, or shouts in capitals.

| | addresses | coverage | share of all missed characters |
|---|---:|---:|---:|
| malformed records | 202 | 83.9% | **63.3%** |
| well-formed records | 598 | **93.3%** | 36.7% |

```
empty-component   119     'D-57,, Block H North Nazimabad Town'
over-80-chars     105     'V28C+4M, SHOP NO G-12 GROUND FLOOR THE CENTRAL MALL ...'
repeated-phrase    29     'Block 5-E Block 5 E Block 5 Nazimabad'
shouting            8
```

This matters more than the aggregate. **On addresses a person would actually
write, the shipped configuration is at 93.3%, not 90.5%.** The aggregate is
dragged down by OSM data-entry errors, and a model fine-tuned on this corpus
would spend most of its capacity learning to parse them.

That is not a hypothetical objection. 63.3% of the training signal available
here is signal about malformed records.

### 21.2 The worst well-formed cases were one fixable shape

Ranking well-formed addresses by coverage, the bottom of the list was almost
entirely one pattern:

```
31.6%   C-21, Block J North Nazimabad Town, Karachi
31.6%   C-11, Block I North Nazimabad Town, Karachi
32.4%   C-18, Block J North Nazimabad Town, Karachi
50.0%   B-14, Block D North Nazimabad Town, Karachi
```

The encoder finds `North Nazimabad Town`. The plot number sits **two** components
out, behind `Block J` — which carries no digit, so the single-step rule of §18.4
stopped there and never reached it.

Extending the walk to step over a structural component and try again:

| | effective coverage | over-redaction |
|---|---:|---:|
| §18.4 rule | 89.4% | 0.0% |
| **stepping rule** | **90.5%** | **0.0%** |

By form: `plot_number` 85.3% → **86.9%**, `sector_code` 87.4% → **88.2%**,
`block_phase` 93.1% → **94.3%**, `plain_street` 95.7% → **96.5%**. Addresses
redacted *completely* rise from 74.3% to **77.1%**.

Over-redaction on 400 address-free memos is **0.0% of memos and 0.00% of
characters** — identical to the narrower rule. The gain is free.

**This is the second time a residual attributed to model weakness turned out to
be a span-boundary problem a regex could reach.** That is the pattern the
fine-tuning decision has to weigh.

### 21.3 Full fine-tuning does not fit the *local* hardware

~~Not an argument about value — an argument about possibility~~ **Withdrawn as
an argument against fine-tuning.** The measurement below stands; the conclusion
drawn from it does not. Kaggle (T4 16 GB, 30 h/week) and Colab are available, so
the constraint is local only.

```
parameters                     208.6 M (all trainable)
weights on GPU                 0.84 GB
weights + gradients + Adam     4.18 GB
GPU total                      4.29 GB  (RTX 3050 Ti Laptop)
```

**4.18 of 4.29 GB before a single activation**, on a card that also drives a
display. Full fine-tuning cannot run on *this* machine — but 4.18 GB fits a
16 GB T4 roughly four times over, so LoRA and 8-bit Adam are not needed either.

This removes one of the five arguments in §21.4, and it was the one explicitly
labelled as not being about value. §21.5 does not turn on it.

### 21.4 The arguments, current state

**For, and what remains of each:**

| §16.4 argument | state |
|---|---|
| "79% is not 99%" | **weakened further.** 96.0% on well-formed addresses after §23, against 100% for PERSON |
| "sector forms stay weakest" | **withdrawn** (§19.3). It was Karachi plot numbers |
| "the residual is the identifying part" | **partly addressed.** 77.1% of addresses now leave with nothing readable, up from 62.4% |
| "a training corpus now exists" | **true, and thinner than it looks.** 17,177 distinct street-tails, 83% Karachi, 63% of the residual malformed |
| "no Pakistani-locale address model off the shelf" | **still true.** The one thing a checkpoint cannot buy |
| *new:* "ADDRESS has a field now" | **true.** §13's objection is gone |

**Against:**

- **The weak cell is a span problem, not a language problem.** Karachi plot
  numbers at 86.9%, and 30.2% of that residual is still leading characters —
  the shape rules have twice proven able to reach.
- **PERSON is at 100% and there is nothing to protect it with.** Fine-tuning on
  ADDRESS risks catastrophic forgetting on the entity this pipeline exists for,
  and `decisions.md` §1 forbids a real-name corpus to train against. The only
  PERSON data is synthetic and already scores 100%, so it can detect damage but
  cannot prevent it.
- **Testable, but only just.** Grouped splitting by street-tail gives an honest
  evaluation — a plain record split leaks, because `PECHS Block 2, Karachi`
  appears 2,655 times with different numbers. But the Islamabad sector cell has
  **134 distinct street contexts**, so that cell cannot be evaluated with
  confidence whatever the split.
- ~~**It does not fit the hardware** (§21.3).~~ Withdrawn — free Kaggle/Colab
  GPUs are available, and 208M params fit a T4 without LoRA.
- **Tier 2 already costs ~2x** since ADDRESS returned. Fine-tuning does not
  reduce that.

### 21.5 Decision

**Do not fine-tune. Ship the pinned checkpoint plus the span rules.**

The reasoning has changed since §16.5, and the change matters more than the
verdict. §16 said don't, mostly because ADDRESS had no consumer and the
evaluation would be circular. Both of those are now fixed — and the answer is
still don't, for a better reason:

> Every time this project has attributed an address failure to the model, and
> then measured where the failure actually was, it turned out to be somewhere a
> rule could reach. §17 found separators. §18 found the house number. §21.2
> found the component behind the structural word. Three for three.

The remaining residual is 63% malformed records and 27.8% leading characters.
Neither is a language-understanding problem. Before spending a rented GPU on
208M parameters, the cheaper question is how much of the last 6.7% on
well-formed addresses is still positional.

### 21.6 What would change this

Concrete, so the decision can be revisited on evidence rather than mood:

1. **The residual stops being positional.** If a further span rule gains under
   0.3 points, the easy ground is gone and the rest is genuinely the model.
   *Tested in §23: the bridging rule gained 2.6 points. This did not fire.*
2. **Addresses become a primary entity.** ADDRESS is a secondary field in a
   memo. If a `beneficiary_address` column appears, 93.3% is no longer good
   enough and the calculus changes.
3. **A second city arrives with real sector density.** 134 Islamabad street
   contexts cannot support a claim either way. More Islamabad and Rawalpindi
   data is a prerequisite, not an optimisation.
4. **PERSON gets a corpus that can prove it survived training.** Without one,
   any fine-tune is a bet on the entity that matters most.

### 21.7 If it is done anyway, the design

Recorded now so it cannot be improvised later:

- **Group by street-tail.** Never split one street across train and test. 17,177
  groups.
- **Stratify the test set by city × form**, so no cell is reported as an average
  of Karachi.
- **Exclude malformed records from training**, or measure with and without. 63%
  of the signal is otherwise about data-entry errors.
- **Pre-declare the PERSON floor.** Run the §2 probe before and after; any drop
  below 99% fails the experiment regardless of the ADDRESS result.
- **Pre-declare the ADDRESS bar.** Not clearly above ~~93.3%~~ **96.0%** (§23.4)
  on well-formed addresses means the checkpoint won.
- **Report the Islamabad sector cell separately**, labelled underpowered at
  n≈134 contexts.
- **Run it on Kaggle or Colab.** Full fine-tuning does not fit locally, but
  4.18 GB fits a 16 GB T4 with room to spare — no LoRA, no 8-bit Adam.
- **Upload nothing that §1 forbids.** The corpus is public OSM address data and
  synthetic names, so a hosted notebook is within the privacy line. That has to
  stay true of anything added to it later.

### 21.8 Limits

- **"Malformed" is a heuristic**, not ground truth. It uses four structural
  rules and no human review. The 63.3% is indicative.
- **The stepping rule is tuned on this corpus.** It was designed after looking
  at the failures it fixes, which is exactly the loop §5 warns about — the
  0.0% over-redaction figure is the guard, and it is measured on generated
  memos, not real ones.
- **No fine-tune was actually run.** This argues from the structure of the
  residual and the cost of the alternative, not from a trained model. A
  contrary result from an actual run would beat this reasoning.

---

## 22. Tier 3 has nothing to escalate on

`decisions.md` §11 settles Tier 3's shape — genuine escalation, same span,
promoted on uncertainty — and §12 pre-registers its failure condition: *"if it
exceeds ~1-2% of messages the cost argument for tiering collapses."*

Both assume an uncertainty signal exists. §10.1 already tested one and found it
backwards. This tests the only remaining candidate, the encoder's own confidence
score, over 1,200 generated memos: 600 address-bearing, 600 address-free.

### 22.1 Confidence is weakly predictive, and not monotonic

| band | spans | precision |
|---|---:|---:|
| 0.55–0.65 | 525 | 76.6% |
| 0.65–0.75 | 668 | 85.8% |
| 0.75–0.85 | 899 | **99.3%** |
| 0.85–0.95 | 1,441 | 95.4% |
| 0.95–1.00 | 188 | 100.0% |
| **all** | **3,721** | **92.2%** |

Mean confidence is 0.811 on correct spans and 0.690 on wrong ones. So the signal
is real — unlike §10.1's masked fraction, this is not backwards.

It is also **not monotonic**: the 0.75–0.85 band is more precise than 0.85–0.95.
A threshold drawn anywhere on this curve is drawn through noise as well as
signal.

### 22.2 The population it would fix is 0.25% of records

Counting a leak as identifying characters surviving **both** tiers:

```
records leaking after Tier 1 + Tier 2 : 3 of 1,200  (0.25%)
```

**§23 has since cut this to 1 of 1,200 (0.08%)** by closing two of the three.
Every figure below is the pre-§23 measurement; the decision only hardens, since
a smaller population buys less.

All three are address boundary errors:

```
'Delivery to Bilal Raza, Flat G-790, Vehari Road, Multan, contact ...'
   leaks: 'Vehari Road'
'Delivery to Muhammad Iqbal, House No. 38-N, Chaman Housing Scheme, Quetta, ...'
   leaks: 'Chaman Housing Scheme'
'Statement Plot 12B, Street 14, PECHS Block 2, Karachi par bhej dein'
   leaks: 'Karachi'
```

Not one is a missed entity. All three are the same positional class §17, §18 and
§21 have now reached three times with regexes.

**A correction to this section's own first run.** It scored Tier 2 in isolation
and reported 6.1% of records leaking, with half invisible to any confidence
rule. Production runs Tier 1 on the same memo, and once the rules are included
the figure is 0.25% and nothing is invisible. The phones, CNICs and emails the
rules already remove were being counted as Tier 2's misses. Fifth reversal in
this project, and the third caused by measuring one component as though it were
the system.

### 22.3 The trigger costs a third of the stream to reach two records

| escalate if any span below | catches | escalates |
|---|---:|---:|
| 0.65 | 66.7% of leaks (2 of 3) | **35.6% of the stream** |
| 0.75 | 66.7% | 61.8% |
| 0.85 | 66.7% | 74.7% |

To adjudicate 2 leaking records, 427 records go to an LLM. **213 to 1.**

Against §12's budget:

| threshold | records escalated | §12 verdict |
|---|---:|---|
| < 0.60 | 20.5% | collapses |
| < 0.65 | 35.6% | collapses |
| < 0.70 | 49.2% | collapses |
| < 0.75 | 61.8% | collapses |

There is no threshold under 2%. The lowest band that exists at all starts at
0.55, because that *is* the shipped threshold — everything below it was already
discarded. The confidence scores that survive into production are, by
construction, the ones the model was most sure of.

### 22.4 Cost, if it were built anyway

At ~800 ms per LLM call, amortised over the whole stream:

| escalation rate | added ms/record | throughput ceiling |
|---:|---:|---:|
| 1% | ~8 ms | ~125 /s |
| 20.5% (the best measured) | ~164 ms | **~6 /s** |

Current throughput is ~160 /s. The measured escalation rate would cut it by
96%.

Local inference does not rescue this. The GPU holds 4.29 GB and GLiNER already
occupies 0.84 GB during inference, leaving room for roughly a 3B model at 4-bit
— and a 3B model adjudicating Pakistani-locale PII is not obviously better than
the encoder it is adjudicating.

### 22.5 Decision

**Tier 3 is not built. Recorded as a measured non-goal, not an unfinished item.**

The three-tier architecture in the README and `decisions.md` describes an
escalation Tier 3 that this measurement shows cannot exist as specified:

1. The error population is **0.25% of records**.
2. Every one of those errors is **positional**, and rules have reached that class
   three times.
3. The cheapest trigger that finds two thirds of them **escalates 35.6% of the
   stream** — eighteen times §12's budget.
4. It would cost **96% of throughput**.

This is the second uncertainty signal this project has tested and the second to
fail, and the reason is the same both times. §10 stated it plainly: *"at runtime
there is no ground truth. The processor cannot know whether a masked span was a
real name or a false positive; if it could, it would not need the model."*
Confidence is a weaker restatement of the model's own opinion, not an
independent check on it.

### 22.6 What would change this

- **A different error population.** If contextual PII enters the stream —
  identity inferable across a sentence rather than located in a span — Tier 3
  stops being escalation and becomes a capability argument, which is how Tier 2
  earned its place. That needs a corpus demonstrating the gap, which does not
  exist.
- **An independent signal.** Disagreement between two cheap detectors is a real
  uncertainty measure in a way one model's own score is not. Two encoders cost
  2x, not 100x, and that is measurable.
- **A materially higher leak rate.** 0.25% leaves nothing to buy. If a field
  arrives where Tier 2 performs far worse, the arithmetic changes.

### 22.7 Limits

- **Measured on generated memos**, whose addresses §21.8 records as easier than
  real OSM ones. The true leak rate on messier text is higher than 0.25%, and
  the 3-record population is too small to characterise.
- **`decisions.md` §11's design is not disproven in general** — only shown to
  have no viable trigger on this stream, with this detector.
- **No LLM was benchmarked.** The 800 ms figure is an assumption; the argument
  does not turn on it, because the escalation rate fails at any latency.
- **The generous span-correctness rule** (a span is correct if it touches gold
  at all) flatters precision. A stricter rule would lower every band and would
  not change the escalation rates, which depend only on scores.

---

## 23. The residual was a hole, not a boundary

§21.6 pre-registered the condition that would end the span-rule programme:
*"if a further span rule gains under 0.3 points, the easy ground is gone and the
rest is genuinely the model."* This tests it. The rule gains **2.6 points**, so
the condition does not fire.

### 23.1 The encoder was not missing the address — it was splitting it

Dumping every identifying run the extension rules still leave behind, over 2,786
well-formed OSM addresses, the residual is **not** where §17 and §18 found it.
Leading runs: 104. **Interior runs: 263.** The single largest shape:

```
48 runs   'Gulistan e Jauhar Block #'
15 runs   'Block <letter> North Nazimabad Town'
 5 runs   'Federal B Area Block'
 3 runs   'Bahria Town Main Boulevard'
```

Inspecting the spans behind them shows why no outward walk could reach them:

```
'Deliver to C-21, Block J North Nazimabad Town, Karachi'
   0.77 'C-21'
   0.89 'Karachi'          <- the locality between them: no span at all

'Transfer to 706, Federal B Area Azizabad Block 8 Gulberg Town'
   0.63 '706'
   0.87 'Federal B Area'
   0.91 'Gulberg Town'     <- 'Azizabad Block 8' falls in the hole
```

The encoder returns the plot number *and* the city and drops the locality
between them. `extend_address_span()` walks outward from a span; an interior gap
has no outer edge to walk from. **This is a different failure mode from §17 and
§18, and it was hidden inside the word "interior" in §17's breakdown.**

### 23.2 Joining spans, and where to stop

`bridge_address_spans()` joins two ADDRESS spans separated by at most
`_MAX_BRIDGE` characters. Sweeping the window on the same 2,786 addresses,
against over-redaction on 400 address-free memos:

| max gap | coverage | fully redacted | memos hurt | extra chars |
|---:|---:|---:|---:|---:|
| 0 (shipped before) | 93.4% | 88.1% | 0.0% | 0.00% |
| 8 | 93.7% | 89.7% | 0.0% | 0.00% |
| 16 | 94.2% | 91.0% | 1.5% | 0.38% |
| 24 | 94.9% | 92.4% | 1.5% | 0.38% |
| **32** | **96.0%** | **93.5%** | **1.5%** | **0.38%** |
| 48 | 96.6% | 94.1% | 1.5% | 0.38% |
| 64 | 96.7% | 94.2% | 1.5% | 0.38% |

**32 ships.** The cost is flat from 16 upward, so the window is not chosen to
control cost — it is chosen to bound the blast radius on text shapes this corpus
does not contain. 48 and 64 buy 0.6 and 0.7 points for the right to join spans
half a line apart, and a memo carrying an amount or a reference number between
two locations is exactly the shape not sampled here.

### 23.3 The cost is one stopword

Every over-redacted character, at every window from 16 to 64, is the same thing:

```
'Rent payment from Hussain Syed, contact 03033547159'
   newly redacted: ', contact '
```

The name and the phone number either side are both PII and both already
redacted. Bridging joins them and takes the word `contact` with it. That is the
whole of the 0.38%: no amount, no reference, no business fact — one stopword
between two spans that were being masked anyway.

### 23.4 Results on the shipped path

Measured through `Tier2Detector` and `RulesDetector` as `processor.py` wires
them, not a reimplementation:

| | before | after |
|---|---:|---:|
| OSM well-formed, coverage | 93.4% | **96.0%** |
| OSM well-formed, fully redacted | 88.1% | **93.5%** |
| generated stream, ADDRESS | 99.9% | **100.0%** |
| generated stream, PERSON | 100.0% | **100.0%** |
| **records leaking after both tiers** | **3 of 1,200** | **1 of 1,200** |

By cell, the gain lands on the weakest ones — the cell §19 identified and §21
proposed to fine-tune:

| cell | before | after | |
|---|---:|---:|---:|
| **Karachi / plot_number** | 86.5% | **95.2%** | **+8.7** |
| Islamabad / block_phase | 90.7% | 96.9% | +6.2 |
| Karachi / sector_code | 89.9% | 96.0% | +6.1 |
| Peshawar / plain_street | 85.9% | 89.0% | +3.0 |
| Karachi / block_phase | 95.6% | 97.9% | +2.3 |

The two leaks it closes are `'Vehari Road'` and `'Chaman Housing Scheme'` — two
of the three §22 found. The survivor is `'Karachi'` in
`'Statement Plot 12B, Street 14, PECHS Block 2, Karachi par bhej dein'`.

### 23.5 What this does to the fine-tuning decision

**It strengthens §21.5 rather than reopening it.** §21.6's stopping condition was
written to be falsifiable and it did not fire: the rule gained 2.6 points where
0.3 would have ended the argument.

> §17 found separators. §18 found the house number. §21.2 found the component
> behind the structural word. §23 found the hole between two spans. **Four for
> four**, every address failure this project has attributed to the model has
> turned out to be somewhere a rule could reach.

Two consequences for §21.7, which stay pre-declared:

- **The ADDRESS bar moves from 93.3% to 96.0%** on well-formed addresses. A
  fine-tune now has to clear a materially higher number to win.
- **The proposed anchors were not the target.** `Plot\s*\d+` and `KDA Scheme` were
  proposed as the Karachi fix; measurement shows the encoder already finds
  `'Plot NO 13A'` (0.81), `'Plot# 5-11'` (0.86) and `'KDA Scheme No 1'` (0.81).
  Adding those anchors would have gained nothing, because the failure was never
  the prefix.

### 23.6 Limits

- **`_MAX_BRIDGE` is tuned on this corpus**, the loop §5 warns about. The 400
  address-free memos are the guard, and they are generated, not real.
- **Bridging cannot separate two genuinely different addresses** in one memo if
  they sit within 32 characters. No case appeared in 400 memos; the shape is
  plausible in real remittance narration and is not covered by any test.
- ~~**One leak survives** and it is a trailing city after a Roman-Urdu tail.~~
  **Closed in §24.** The cause was not the encoder's span but the trailing-city
  rule's lookahead, which required the sentence to end after the city. Found by
  a live broker run, not by any probe here.
- **The over-redaction sample is one template family.** All six damaged memos are
  `'Rent payment from <name>, contact <phone>'`, so 0.38% is a measurement of one
  shape, not a general rate.

---

## 24. A live broker found what no offline probe did

Every number in §14–§23 came from a probe calling the detectors directly. This
runs the actual pipeline: Kafka 3.8 (KRaft), Postgres 16, and the processor in
its own container with `INSTALL_TIER2=true`, consuming `txn.raw` and writing
`txn.clean` plus the audit.

It found a defect that eleven sections of offline measurement did not.

### 24.1 The defect: the city rule assumed the sentence ended

§18.4's trailing-city rule required end-of-string or punctuation after the city:

```python
_TRAILING_CITY = re.compile(rf"^,?\s*({cities})(?=$|[,.;])")
```

That guard exists for a real reason — `'Model Town, Sialkot Road'` must not lose
`Sialkot`, because in Pakistan a city name is a road name more often than not.
But it is too strong, and **Roman-Urdu memos continue past the city**:

```
RAW  : Statement Plot E-379, Airport Road, Quetta par bhej dein
CLEAN: Statement [ADDRESS], Quetta par bhej dein          <- leaked
```

The lookahead sees ` par`, not `$` or punctuation, so the rule declines and the
city ships in the clear. **This is the leak §22.2 recorded and §23.4 could not
close** — the survivor was `'Karachi'` in
`'... PECHS Block 2, Karachi par bhej dein'`, the identical shape.

The corrected test is not "is anything after the city" but "is what follows a
ROAD word", which is all the original guard was ever protecting against:

```python
(?=$|[,.;]|\s+(?!(?i:Road|Rd|Street|St|Highway|...|Cantt)\b))
```

| tail | before | after |
|---|---|---|
| `, Quetta par bhej dein` | no match | **`, Quetta`** |
| `, Multan hai` | no match | **`, Multan`** |
| `, Sialkot Road` | no match | no match |
| `, Sialkot road` | no match | no match |
| ` Karachi Cantt` | no match | no match |
| `, Quetta.` | `, Quetta` | `, Quetta` |

### 24.2 Proof on the live stream

Same broker, same topics, processor rebuilt and restarted mid-run:

| | memos | city left in the clear |
|---|---:|---:|
| processed **before** the fix | 3,984 | **2** |
| processed **after** the fix | 1,147 | **0** |

141 of the post-fix memos carried a redacted address. No city survived any of
them.

### 24.3 Why the probes missed it

The offline probes build their cases as `TEMPLATE.format(a=address)` where the
address is at or near the end of the string. `probe_address_residual.py` uses
six templates and only one puts narration after the address. The generator's
`ADDRESS_MEMO_TEMPLATES` do — `'Statement {} par bhej dein'` — but
`probe_address_in_stream.py` scored ADDRESS coverage at 99.9% because it
measured *identifying characters covered*, and a missed city is 7 characters
against an address of 40.

**The aggregate hid it. The stream did not.** A leak is a property of a record,
not of a character count, and only the end-to-end run scored records.

### 24.4 What else the run confirmed

- **Tier 2 loads in a container from the warm volume**, at the pinned revision,
  and logs it: `revision=88c3b98b... threshold=0.55 labels=['PERSON_NAME',
  'ADDRESS'] batch=8 device=cpu`.
- **The pin's known gap is visible in the logs.** The run still fetches
  `microsoft/deberta-v3-base` at `main` for the tokenizer — exactly what §20
  documents the revision pin does *not* cover.
- **Both tiers write to the audit**: 21,678 Tier 1 findings and 2,609 Tier 2
  (1,957 PERSON_NAME, 652 ADDRESS) over the run.
- **CPU throughput is ~13 records/s**, p50 112–128 ms, against ~160/s rules-only.
  That is the honest cost of Tier 2 without a GPU, and the Dockerfile installs
  the CPU wheels deliberately.
- **The integration tests ran for the first time.** With Postgres up, the 13
  tests that always skip executed and passed.

### 24.5 An observation the run surfaced, not yet acted on

The encoder labels address fragments as people:

```
'Deliver to C-21, Block J, Karachi'
   PERSON_NAME  'C-21'      conf=0.81
   ADDRESS      'C-21, Block J, Karachi'  conf=0.84
   PERSON_NAME  'Block J'   conf=0.70
```

The redaction is correct — `merge_spans()` unions them and the output is
`[ADDRESS+PERSON_NAME]`, nothing leaks. But **the audit records two PERSON_NAME
findings that are not people**, and the governance report counts entity types
from that table. §18.2 established that a false positive changing no output is
not a cost; that argument holds for redaction and does not hold for the audit.

Not fixed here. Recorded because the compliance report is the one consumer for
which entity_type accuracy, not span coverage, is the product.

### 24.6 Limits

- **One broker, one partition assignment, one consumer.** Rebalancing under
  `--scale processor=3` with Tier 2 loaded is still untested, and model load
  takes ~20 s per replica, which is inside the rebalance window.
- **CPU only.** The container installs CPU torch. No GPU container was built,
  so the ~16 ms/record GPU figure is still probe-measured, not stream-measured.
- **The fix is measured on 1,147 records.** The shape it corrects is common in
  this generator; its frequency in real remittance narration is unknown.
- **`_ROAD_WORD` is a list, and lists are incomplete.** `Chowk`, `Cantt` and
  `Bypass` are in it because they appeared; a city followed by an unlisted road
  word will still be over-extended.

---

## 25. The second repo, and how to pin something that takes no revision

§20 pinned `TIER2_MODEL_REVISION` and recorded that the pin was not total. §24
saw the gap in a live log. This closes it, and corrects what §20 said the gap
was.

### 25.1 It is the backbone config, not the tokenizer

§20 said GLiNER "resolves its base tokenizer from microsoft/deberta-v3-base at
`main`". That is wrong. Reading `gliner/model.py`:

```python
tokenizer_config_path = model_dir / "tokenizer_config.json"
if tokenizer_config_path.is_file():
    tokenizer = AutoTokenizer.from_pretrained(model_dir, ...)   # <- this branch
else:
    tokenizer = AutoTokenizer.from_pretrained(config.model_name, ...)
```

The checkpoint ships its own tokenizer, so the fallback never runs:

```
gliner_medium-v2.5 snapshot: tokenizer.json, tokenizer_config.json,
                             special_tokens_map.json, added_tokens.json, spm.model
```

The real fetch is in `gliner/modeling/encoder.py`:

```python
encoder_config = AutoConfig.from_pretrained(model_name, cache_dir=cache_dir)
```

No `revision=`, and GLiNER exposes no way to pass one. The cache confirms which
repo is touched and how little of it:

```
models--microsoft--deberta-v3-base/snapshots/8ccc9b6f.../config.json    (only)
```

**One file: the architecture config.** Weights and tokenizer both come from the
pinned checkpoint. That makes the risk smaller than §20 implied — a changed
`hidden_size` fails the load loudly rather than drifting — but it is still an
unpinned input to a system whose threshold is checkpoint-specific.

### 25.2 A pin that cannot be passed as an argument

Since no revision can be handed to `AutoConfig`, the only lever is what the
cache contains and whether the network is allowed. Measured on a clean
`HF_HOME`:

| | result |
|---|---|
| `snapshot_download(repo, revision=SHA)` writes `refs/main`? | **no** |
| offline `AutoConfig.from_pretrained(repo)` with no revision | **works** |
| offline `AutoConfig.from_pretrained(repo, revision=SHA)` | works |

The second row is the one that matters and it was not obvious: with no `refs`
written, offline resolution still finds the snapshot on disk. So **prefetching
an exact commit and then forbidding the network is a real pin**, not a hope.

That gives the two-step:

```bash
python -m pipelineguard.prefetch     # once, with network
HF_HUB_OFFLINE=1 ...                 # from then on
```

`prefetch_pinned()` fetches the checkpoint at `TIER2_MODEL_REVISION` and exactly
`config.json` of the backbone at `TIER2_BASE_REVISION`.

### 25.3 The pin says whether it is real

A pin nobody checks is a comment (§20). `Tier2Detector.load()` now reports which
state it is in, because "pinned" is a property of the machine, not of the code:

```
backbone config PINNED: microsoft/deberta-v3-base@8ccc9b6f3619 (offline)
```

and otherwise warns, naming the fix:

```
backbone config UNPINNED: microsoft/deberta-v3-base is resolved at `main`
because GLiNER passes no revision. Run `python -m pipelineguard.prefetch`
then set HF_HUB_OFFLINE=1 to pin it. cached=['8ccc9b6f3619']
```

The third state — offline but the cache holds more than one commit — warns too.
Offline resolution picks one and does not say which, so a cache with two
commits is not a pin even though nothing goes over the network.

### 25.4 Measured, both environments

| | HF requests during load |
|---|---:|
| native venv, before | several |
| native venv, prefetched + `HF_HUB_OFFLINE=1` | **0** |
| container, before | several (visible in §24's log) |
| container, prefetched + `HF_HUB_OFFLINE=1` | **0** |

The container then processed 300 records with Tier 2 enabled and **zero**
requests to huggingface.co.

Note that the two caches are separate: the native run uses the developer's
`HF_HOME`, the container uses the `models` volume. The prefetch is once per
cache, not once per project.

### 25.5 Limits

- **`AutoConfig` is not the only unpinned input.** This closes the one a live
  log exposed. `torch` and `gliner` are pinned by version range in
  `pyproject.toml`, not by lockfile, and the CUDA/CPU wheel choice is a build
  argument.
- **`HF_HUB_OFFLINE=1` is not the default** in `docker-compose.yml`, because the
  first run on a cold volume has to download. Two states ship, and only one of
  them is pinned.
- **The backbone commit was read off a warm cache**, not chosen. It is whatever
  `main` served on 2026-08-12. Recording it makes it stable, not correct.
