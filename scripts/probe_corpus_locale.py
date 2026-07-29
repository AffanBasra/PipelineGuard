"""Phase 0 probe: what is in the `intl` half of Nemotron-PII?

Question being answered: does the intl half contain IBAN-shaped or non-US phone
data that would partly serve the PK-locale evaluation, reducing the need for
hand-built data?
"""
import ast
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

PATH = Path(__file__).resolve().parent.parent / "data" / "nemotron-pii" / "test-00000-of-00001.parquet"

# Any-country IBAN shape: 2 letters, 2 check digits, 11-30 alphanumerics.
IBAN_SHAPE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
# PK-specific shapes from tier1_rules.py
PK_IBAN = re.compile(r"\bPK\d{2}[A-Z0-9]{4}\d{16}\b")
CNIC_CANON = re.compile(r"\b\d{5}-\d{7}-\d\b")
CNIC_BARE = re.compile(r"\b\d{13}\b")
PK_PHONE_PREFIX = re.compile(r"(\+92|\b03\d{2})")
# International dialling prefix, any country
INTL_PHONE = re.compile(r"\+(\d{1,3})[\s\-()]?\d")

locale_counts = Counter()
format_by_locale = Counter()
domain_by_locale = Counter()
label_by_locale = defaultdict(Counter)

# Sampled span surface forms, per (locale, label)
samples = defaultdict(list)
SAMPLE_LABELS = {
    "swift_bic", "bank_routing_number", "account_number", "phone_number",
    "credit_debit_card", "national_id", "passport_number", "tax_id",
    "customer_id", "employee_id", "iban", "bic", "vin", "driver_license",
}
SAMPLE_CAP = 12

# Raw-text pattern hits, per locale
text_hits = defaultdict(Counter)
# Country codes seen in +NN phone spans, per locale
dial_codes = defaultdict(Counter)
# `country` entity surface forms, per locale
countries = defaultdict(Counter)

pf = pq.ParquetFile(PATH)
cols = ["locale", "document_format", "domain", "text", "spans"]
rows = 0

for batch in pf.iter_batches(columns=cols, batch_size=2000):
    locales = batch.column("locale").to_pylist()
    formats = batch.column("document_format").to_pylist()
    domains = batch.column("domain").to_pylist()
    texts = batch.column("text").to_pylist()
    spans_col = batch.column("spans").to_pylist()

    for loc, fmt, dom, text, spans_str in zip(locales, formats, domains, texts, spans_col):
        rows += 1
        locale_counts[loc] += 1
        format_by_locale[(loc, fmt)] += 1
        domain_by_locale[(loc, dom)] += 1

        # Raw-text shape scan (independent of annotations)
        if IBAN_SHAPE.search(text):
            text_hits[loc]["iban_shape_any_country"] += 1
        if PK_IBAN.search(text):
            text_hits[loc]["pk_iban_shape"] += 1
        if CNIC_CANON.search(text):
            text_hits[loc]["cnic_canonical_shape"] += 1
        if CNIC_BARE.search(text):
            text_hits[loc]["cnic_bare_13digit_shape"] += 1
        if PK_PHONE_PREFIX.search(text):
            text_hits[loc]["pk_phone_prefix"] += 1

        for span in ast.literal_eval(spans_str):
            label = span["label"]
            label_by_locale[loc][label] += 1
            surface = text[span["start"]:span["end"]]

            if label in SAMPLE_LABELS and len(samples[(loc, label)]) < SAMPLE_CAP:
                samples[(loc, label)].append(surface)

            if label == "phone_number":
                m = INTL_PHONE.search(surface)
                if m:
                    dial_codes[loc]["+" + m.group(1)] += 1
                else:
                    dial_codes[loc]["<no + prefix>"] += 1
            elif label == "country":
                countries[loc][surface.strip()] += 1

    print(f"\r{rows:,} rows", end="", file=sys.stderr)

print(file=sys.stderr)


def hdr(s):
    print(f"\n{'=' * 72}\n{s}\n{'=' * 72}")


hdr("ROWS AND LOCALE")
print(f"total rows: {rows:,}")
for loc, n in locale_counts.most_common():
    print(f"  {loc:<12} {n:>8,}")

hdr("DOCUMENT FORMAT BY LOCALE")
for (loc, fmt), n in sorted(format_by_locale.items()):
    print(f"  {loc:<12} {fmt:<16} {n:>8,}")

hdr("TOP DOMAINS BY LOCALE")
for loc in locale_counts:
    print(f"\n  [{loc}]")
    top = Counter({d: n for (l, d), n in domain_by_locale.items() if l == loc})
    for d, n in top.most_common(10):
        print(f"    {d:<32} {n:>7,}")

hdr("LABELS PRESENT IN ONE LOCALE BUT NOT THE OTHER")
all_labels = set()
for c in label_by_locale.values():
    all_labels |= set(c)
locs = sorted(locale_counts)
for label in sorted(all_labels):
    counts = [label_by_locale[l].get(label, 0) for l in locs]
    if 0 in counts:
        pairs = "  ".join(f"{l}={c:,}" for l, c in zip(locs, counts))
        print(f"  {label:<34} {pairs}")

hdr("FINANCIAL / ID LABEL COUNTS BY LOCALE")
fin = ["swift_bic", "bank_routing_number", "account_number", "credit_debit_card",
       "national_id", "passport_number", "tax_id", "phone_number", "email"]
print(f"  {'label':<28}" + "".join(f"{l:>12}" for l in locs))
for label in fin:
    print(f"  {label:<28}" + "".join(f"{label_by_locale[l].get(label, 0):>12,}" for l in locs))

hdr("RAW-TEXT SHAPE SCAN (documents containing at least one match)")
pats = ["iban_shape_any_country", "pk_iban_shape", "cnic_canonical_shape",
        "cnic_bare_13digit_shape", "pk_phone_prefix"]
print(f"  {'pattern':<30}" + "".join(f"{l:>12}" for l in locs))
for p in pats:
    print(f"  {p:<30}" + "".join(f"{text_hits[l].get(p, 0):>12,}" for l in locs))

hdr("PHONE DIALLING CODES BY LOCALE (top 20)")
for loc in locs:
    print(f"\n  [{loc}]  {sum(dial_codes[loc].values()):,} phone spans")
    for code, n in dial_codes[loc].most_common(20):
        print(f"    {code:<16} {n:>8,}")

hdr("COUNTRY ENTITY SURFACE FORMS (top 25 per locale)")
for loc in locs:
    print(f"\n  [{loc}]")
    for c, n in countries[loc].most_common(25):
        print(f"    {c:<32} {n:>7,}")

hdr("SAMPLE SPAN SURFACE FORMS")
for (loc, label) in sorted(samples):
    print(f"\n  [{loc}] {label}")
    for s in samples[(loc, label)]:
        print(f"    {s!r}")
