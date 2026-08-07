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
