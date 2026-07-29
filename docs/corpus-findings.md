# Corpus findings — `nvidia/Nemotron-PII`

Measurements taken directly against the downloaded file, 2026-07-29. This
document holds the raw evidence; `docs/decisions.md` §11 holds the decisions
that follow from it.

**File under measurement:** `data/nemotron-pii/test-00000-of-00001.parquet`,
150,968,398 bytes, 100,000 rows, 100 row groups. Columns: `uid`, `domain`,
`document_type`, `document_description`, `document_format`, `locale`, `text`,
`spans`, `text_tagged`.

**Method.** Two full passes over the file with `pyarrow.parquet.iter_batches`.
`spans` is a Python repr string, parsed with `ast.literal_eval`. Span surface
forms are taken as `text[start:end]` — never from the span's own `text` field,
which is not verbatim (98.99% exact, 1.01% case-differing). Percentages below
are over annotated spans of the stated label, across the whole file.

Every count in this document is reproducible from the file alone. Cross-check:
per-locale label counts from pass 1 sum exactly to the totals independently
measured in pass 2 (`swift_bic` 2,846 + 2,713 = 5,559; `credit_debit_card`
6,251 + 6,616 = 12,867; `bank_routing_number` 3,979 + 4,375 = 8,354).

---

## 1. The headline: the corpus's identifiers fail their own checksums

| identifier | valid | rate | chance baseline |
|---|---:|---:|---:|
| credit card, Luhn | 1,528 / 12,867 | **11.9%** | ~10% |
| bank routing number, ABA | 843 / 8,354 | **10.1%** | ~10% |
| SWIFT/BIC, ISO 9362 shape | 4,246 / 5,559 | 76.4% | — |

Cards and routing numbers pass at **chance**. They are random digits wearing
correct prefixes and correct lengths — the check digit was never computed.

The values look convincing precisely because the *surface* features are right:

```
credit_debit_card   '4532 0358 9923 4567'   Visa IIN, 16 digits, correct grouping — fails Luhn
                    '3742 912345 67890'     Amex IIN, 15 digits, correct grouping — fails Luhn
bank_routing_number '123427285'             9 digits — fails ABA
```

99.6% of routing numbers are exactly 9 digits (8,320 / 8,354) and 83.7% of
cards are 16 digits, so anything checking *length* is satisfied and anything
checking *arithmetic* is not.

**This is the same defect this project already found and fixed in its own
generator.** From `decisions.md` §2: *"IBANs carry real mod-97 check digits.
Originally random, which meant ~99.5% of generated IBANs failed the detector's
checksum and the entire stream quarantined."* NVIDIA shipped a public dataset
with that bug intact, and it is paired with `gliner-PII`, a model fine-tuned on
it — a model which therefore cannot have learned checksum validity as a signal,
because the training data carries none.

### BIC is better but not clean

| check | rate |
|---|---:|
| strict ISO 9362 shape | 76.4% |
| length 8 or 11 | 77.1% |
| chars 5–6 a real ISO 3166 alpha-2 code | 89.5% |

Length distribution: `{6: 2, 7: 2, 8: 340, 9: 232, 10: 838, 11: 3947, 12: 197, 16: 1}`.
Roughly one in four is the wrong length. The country position is usually right
(`WNLAUS2PQX9`, `GJHLLY56C3B`, `ABDNBG56`), which again means surface
plausibility without structural validity. Failures cluster on placeholder-ish
values: `QWERTUS12XYZ`, `QWERTUS78Z`, `ZYXCUS12G`.

### Annotation quality note

The credit-card length distribution has a tail of degenerate spans — 9 spans of
0 digits, 21 of 3, plus a handful at 24/26/32/39. Small in absolute terms
(<0.5%) but they will show up as unmatchable gold spans, and the evaluation
should report them rather than quietly discard them.

---

## 2. `us` and `intl` are the same documents rendered twice

| | value |
|---|---|
| distinct `document_description`, us | 49,987 |
| distinct `document_description`, intl | 49,987 |
| shared between locales | 49,987 |
| Jaccard | **1.000** |

`document_format` counts are identical per locale (structured 24,711,
unstructured 25,289) and so are the domain counts, to the record — Banking
1,920 in both, User Account and Transaction Services 1,897 in both, IT 1,893 in
both, and so on down the list.

**Consequences.** The file is 50,000 documents in a matched-pairs design, not
100,000 independent samples; effective sample size must be reported as such.
Against that, it is a controlled experiment handed over for free: same
document, same domain, same format, only the locale-specific identifiers
differ, so *"does recall drop when US identifiers become international ones?"*
is answerable on paired data.

Entity counts still differ between the renderings — `email` is 31,918 intl vs
22,012 us — so pairing is at the document level, not the annotation level.

---

## 3. The `intl` half contains no Pakistani data

Tested directly, because a positive result would have removed the need for a
hand-built PK evaluation set.

| probe | intl | us |
|---|---:|---:|
| PK IBAN shape (`PK\d{2}[A-Z0-9]{4}\d{16}`) | **0** | **0** |
| any-country IBAN shape | 34 | 14 |
| CNIC canonical shape (`\d{5}-\d{7}-\d`) | 20 | 0 |
| CNIC bare 13-digit shape | 345 | 14 |

Counts are documents containing at least one match, out of 50,000 per locale.
The nonzero rows are regex coincidences — arbitrary 5-7-1 digit groupings and
13-digit runs — not Pakistani identifiers. **There is no IBAN label in the
taxonomy at all**; the financial identifiers are `swift_bic`,
`bank_routing_number` and `account_number`.

Top dialling codes in intl phone spans: `+7` (1,062), `+91` (325), `+971`
(189), `+49` (182), `+966` (174), `+33` (123), `+55` (108). **`+92` does not
appear in the top 20.** Top `country` surface forms in intl: United States,
Russia, United Kingdom, France, India, Germany, Italy, South Korea, Canada,
Saudi Arabia. **Pakistan does not appear in the top 25.**

`national_id` is the only intl-exclusive label (2,847 intl, 0 us), but its
values span unrelated national schemes with **no country attribute on the
span**:

```
'QX 23 98 15 5'        UK NINO shape
'12.721.987-2'         Chilean RUT shape
'1 85 02 17 123 45'    French INSEE shape
'874-91-2516'          US SSN shape
'12345678901'          undifferentiated
```

Detecting "national ID of unspecified country" is not a rules problem. This is
an honest negative result and should be reported as one rather than papered
over with a permissive regex.

**Conclusion: the hand-built Pakistani evaluation set remains necessary.**

---

## 4. Intl phones are a usable precision probe

Of 12,268 intl phone spans, **7,591 carry no `+` prefix** and appear in bare
local formats: `'0341 8765241'`, `'0747-369-251'`, `'21 843 659 876'`,
`'9812 3456'`, `'0775851282'`.

Some match the PK mobile shape `03XX XXXXXXX` exactly. The existing PK phone
rule will fire on them, producing false positives against independent gold
labels — which is the **first available precision measurement** for that rule.
The synthetic stream cannot supply one, because it contains no near-miss
negatives: everything shaped like a PK phone in it *is* a PK phone.

By contrast us phone spans are almost entirely bare NANP (`11,632` of `11,662`
with no `+`), so the us half is the cleaner recall test and the intl half is
the precision test.

---

## 5. Financial and identifier labels by locale

| label | intl | us |
|---|---:|---:|
| email | 31,918 | 22,012 |
| phone_number | 12,268 | 11,662 |
| account_number | 7,725 | 8,968 |
| credit_debit_card | 6,251 | 6,616 |
| bank_routing_number | 3,979 | 4,375 |
| national_id | 2,847 | **0** |
| swift_bic | 2,846 | 2,713 |
| tax_id | 678 | 624 |
| passport_number | 0 | 0 |

`account_number`, `customer_id` and `employee_id` are opaque — no checkable
format, no consistent length, values ranging from `'87425693'` through
`'23CUST42805'` to `'MKT-3789'`. They are 13.3% of all mentions and are not
addressable by Tier 1 rules in any honest way; a regex broad enough to catch
them would fire on every alphanumeric token in the corpus.

---

## 6. What this means for the evaluation

1. **Detection and validation must be reported separately.** A recall figure
   that requires a passing checksum would report ~12% on cards and ~10% on
   routing numbers and would be measuring NVIDIA's generator, not this
   detector. See `decisions.md` §11.
2. **Effective n is 50,000, not 100,000.** Paired, so locale comparisons are
   strong and absolute sample-size claims must be halved.
3. **Locale packs cannot rely on checksums for confidence on this corpus.**
   Luhn and ABA remain correct engineering for production traffic; they are
   simply not measurable here.
4. **`national_id`, `account_number`, `customer_id` and `employee_id` are out
   of Tier 1's reach by construction** — jointly a large share of the corpus —
   and the reason should be stated, not left as an unexplained recall gap.
5. **The us/intl split maps onto recall/precision** — us for recall on
   well-formed NANP and US identifiers, intl for precision against near-miss
   foreign formats.