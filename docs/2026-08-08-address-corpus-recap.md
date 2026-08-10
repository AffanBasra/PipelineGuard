# Session recap — real address corpus and model re-evaluation

**Branch:** `address-corpus` · **Date:** 2026-08-08 · 331 tests passing

Read this first on Monday. It covers what was built, what broke, what the
numbers say, and the one decision left open.

---

## 1. The question this session answered

`tier2-detection-findings.md` §3 and §6.2 recorded the project's strongest
result: encoders lose ~21 points of coverage on Urdu-form addresses. §5 recorded
the weakness under it — **the author wrote every one of those addresses.** §13
then removed ADDRESS from Tier 2 because nothing in the pipeline contains one.

So: *is the address gap real, or an artifact of hand-written test data?* Every
path forward — restoring ADDRESS, fine-tuning, adding addresses to the generator
— depended on the answer, and none could be justified from data this project
authored.

**Answer: the gap is real, and a different off-the-shelf checkpoint mostly
closes it.**

---

## 2. What was built

| file | purpose |
|---|---|
| `scripts/fetch_osm_addresses.py` | Overpass API fetcher, 8 Pakistani cities, cached and ODbL-attributed |
| `scripts/build_address_corpus.py` | raw OSM → address corpus, with the privacy whitelist |
| `scripts/verify_bboxes.py` | checks the fetcher's bounding boxes against Nominatim |
| `scripts/probe_address_real.py` | scores 4 encoders on ADDRESS and PERSON |
| `tests/test_address_corpus.py` | 55 tests, most guarding the privacy control |

**Corpus: 91,675 unique addresses** from 150,504 raw elements across Islamabad,
Karachi, Lahore, Rawalpindi, Faisalabad, Multan, Peshawar and Quetta.

```
by kind                      by form
  unknown       39.1%          block_phase   53.8%
  residential   33.2%          plain_street  43.3%
  commercial    27.5%          sector_code    2.9%
  institutional  0.1%
```

**Privacy control.** Raw OSM carries real personal data — a `building=house`
node in this data holds `name="Muhammad Ibrahim"`, and 296 elements carry phone
numbers. Only `addr:*` keys are read, plus `building`/`shop`/`amenity`/`office`
as documented exceptions that describe structures and business categories, never
people. Enforced by test, not convention. The raw cache is gitignored.

---

## 3. Bugs found and fixed

Nine of these produced **plausible-looking wrong output** rather than errors.
That is the theme of the session.

### Overpass query — three failures, all HTTP 200 with zero elements

1. `area["name"="Islamabad"]` matched nothing. Pakistani administrative
   boundaries are named in Urdu script (`اسلام‌آباد`, `پنجاب`).
2. `area["name:en"=...]` resolved but also matched three `Islomobod` hamlets in
   Central Asia; adding `["boundary"="administrative"]` matched nothing, because
   Overpass areas do not carry that tag.
3. Scanning a resolved area for every `addr:street` node timed out with 504.

Fixed by using bounding boxes, which skip area resolution entirely.

### Bounding boxes — six of eight clipped their city

Written from memory. A too-small box returns *fewer addresses*, not an error, so
Islamabad's 5,331 elements looked entirely reasonable. Now taken from
Nominatim's administrative boundary.

Nominatim resolves **Multan and Quetta to a point** (an office node and a
railway station), and a point fits inside any box — so both would have passed
verification for the wrong reason. `verify_bboxes.py` rejects reference spans
under 0.02°.

### The probe scored the wrong corpus

The first PERSON run reported urchade at **8.4%** against §2's 99.4%. The patch
had switched the *labels* to PERSON and left the *corpus* on addresses — it
searched 3,600 street addresses for people's names. The tell was bucket names
reading `kind:residential` at n=3600. The probe now prints `entity=` and its
buckets every run.

### Found by code review

- **Deduplication kept the first duplicate, not the best-typed one.** Overpass
  returns a node and a way for the same building; the node has only `addr:*`,
  the way has `building=house`. Same address, `unknown` or `residential` by
  arrival order. 528 records reclassified.
- **`SECTOR_CODE` lacked `re.I`** while `BLOCK_FORM` had it, so `f-8/3` filed as
  `plain_street` — contaminating both cells of the comparison it feeds.
- **The duplicate-city guard was a substring test**: `Multan Road` + city
  `Multan` produced an address with no city. The first fix reintroduced the
  original bug and a test caught it; the correct test is comma-delimited
  component membership.
- **Sampling was stratified by `kind`**, so `sector_code` at 2.9% prevalence gave
  13 addresses in a 300-address sample — while §15 claimed 2,627. `--stratify
  form` added.
- **The fetcher treated every HTTP 200 as success.** Overpass reports
  server-side timeouts in a `remark` field with a truncated element list.
- `--only` overwrote the results file, destroying models that had succeeded.
- `verify_bboxes.py` counted a skipped city as a pass, so it could exit 0 having
  verified three of eight.
- `classify_kind` read POI tags straight off the element, bypassing the
  whitelist it claimed was the only path.

---

## 4. Final model evaluation

**ADDRESS**, form-stratified, 100 addresses per form, 1,200 cases per bucket:

| model | thr | ALL | sector | block | plain | form penalty |
|---|---:|---:|---:|---:|---:|---:|
| **gliner_medium-v2.5** | 0.25 | **85.3%** | **84.2%** | 86.0% | 85.7% | **+1.5** |
| nvidia/gliner-PII | 0.25 | 75.5% | 63.7% | 75.8% | 86.9% | +23.2 |
| urchade (current) | 0.25 | 74.0% | 66.5% | 77.6% | 77.7% | +11.2 |
| xlm-roberta-conll03 | 0.5 | 52.6% | 51.5% | 48.4% | 57.7% | +6.2 |

**PERSON**, 165 synthetic name cases:

| model | ALL | common | rare | ambiguous |
|---|---:|---:|---:|---:|
| **gliner_medium-v2.5** | **100.0%** | 100.0% | 100.0% | 100.0% |
| urchade | 99.4% | 100.0% | 97.8% | 100.0% |
| xlm-roberta-conll03 | 93.8% | 95.8% | 84.4% | 99.8% |
| nvidia/gliner-PII | 91.1% | 91.4% | 92.6% | 88.9% |

**Precision**, 25 clean Pakistani memos with no PII:

| model | thr | fires | over-redaction |
|---|---:|---:|---:|
| urchade | 0.25 | 40% | 21% |
| gliner_community | 0.25 | **76%** | **32%** |
| **gliner_community** | **0.55** | **20%** | **6%** |
| gliner_community | 0.70 | 4% | 1% |

**At 0.25 the new model is worse.** Its coverage advantage there is bought by
flagging nearly everything — the same pattern int8 showed in §7 before §8 caught
it. Thresholds do not transfer between checkpoints, and at 0.55 it dominates.

### Head to head, each at its own operating point

| | urchade @ 0.25 | gliner_community @ 0.55 |
|---|---:|---:|
| ADDRESS | 74.0% | **79.0%** |
| PERSON | 99.4% | 99.4% |
| fires on clean memos | 40% | **20%** |
| over-redaction | 21% | **6%** |
| cost | 6.6 ms/rec | 7.3 ms/rec |

Better on every axis except 11% more compute.

**Changed:** `config.py` now defaults to
`gliner-community/gliner_medium-v2.5` at threshold **0.55**, and `_TUNED_FOR`
moved with it so the mismatch warning stays truthful.

---

## 5. Fine-tuning: the case both ways

### For

- **79% is not 99%.** PERSON is at 99.4%; ADDRESS is twenty points worse on an
  equally identifying entity.
- **The residual is the identifying part.** any-hit ~99% against coverage ~85%
  means the models find the city and miss the house number. A
  partially-redacted address looks redacted and is not.
- **A training corpus now exists** — 91,675 real addresses, independent
  provenance, gold spans that need no alignment. The circularity objection is
  answered.
- **Karachi plot numbers are the weak cell**: 83.4% against 92.7% on Karachi
  plain streets. The Islamabad sector convention is 94.4% (findings §19.3).
- **It is the one thing a checkpoint swap cannot buy.** A Pakistani-locale
  address model does not exist off the shelf.

### Against

- **A checkpoint swap already recovers most of the gap**, today, for 11% more
  compute and zero training.
- **83% of the corpus is Karachi.** A model fine-tuned on it learns Karachi and
  is evaluated on Karachi — the §14.3 mistake with a training loop attached.
- **The eval set would have to be genuinely held out.** Training and testing on
  the same 91,675 proves nothing; Islamabad-only holdout leaves ~2,600 sector
  addresses to both train and test on.
- **The generator still contains no addresses.** §13 removed ADDRESS from Tier 2
  for exactly this reason. Fine-tuning would build a capability with no field.
- **Precision would be unmeasured**, and precision is where the last three
  reversals came from.
- **Tier 2 is already the dominant cost** — 7.3 ms against Tier 1's 0.69.

### Recommendation

**Do not fine-tune. Take the checkpoint swap.**

Revisit only when addresses enter the pipeline as a real field, and only with a
held-out city the model never trained on.

---

## 6. Open for Monday

**Decide:**
1. Accept the model swap, or hold it pending a broader precision corpus. The
   over-redaction numbers rest on **25 hand-written memos** — the §8 corpus, in
   which §9 found two vocabulary defects. That is the weakest evidence here and
   it is carrying the threshold choice.
2. Whether 0.55 is the right point. **0.70** gives 4% fire and 1%
   over-redaction for 68.6% coverage. If over-redaction matters more than
   address completeness, 0.70 is better. 0.55 is a judgement, not an optimum.
3. Whether ADDRESS returns to Tier 2 at all. It is still removed (§13), and
   nothing in the generator produces an address.

**Carried debts:**
- The four `account_holder` tests have never been run against pre-change code.
- No test exercises Tier 2 against a live broker.
- The saturation flag (§10) is designed and unbuilt; it needs a boolean column.

**Not started:** Tier 3 (LLM escalation).

---

## 7. Commits

```
d1581dc  Fix eleven code-review findings, two of which invalidated §15 claims
c0188fd  §15: a checkpoint swap beats fine-tuning, and §14 was under-measured
c564375  Fix the Overpass query and the bounding boxes; corpus grows 7k -> 92k
1752e82  §14: the model we shipped is the worst of three on real addresses
e5d28c1  Split the corpus by building type, and probe four encoders
8b3affc  Build an address corpus from OSM, with the privacy control as a test
```

Nothing pushed. Nothing merged. `master` untouched.
