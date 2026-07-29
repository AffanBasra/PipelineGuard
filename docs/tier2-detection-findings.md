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
4. **`nvidia/gliner-PII` is the model to use** — best and most stable of the
   three, and its weakness is a real finding rather than an artifact.
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
- **Latency was not measured.** Tier 2's cost per record — the number the
  whole tiering argument depends on — is still unknown.
