"""Phase 0 probe 2: are Nemotron's synthetic identifiers checksum-valid?

This decides a Phase 1 design question. The `generic` locale pack is planned to
carry Luhn-validated credit cards, ABA-validated routing numbers and structural
BIC validation. If the corpus's synthetic values do not satisfy those checks,
strict validators will report false negatives that are the corpus's fault, not
the detector's -- and the pack must emit reduced-confidence findings instead.
"""
import ast
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

PATH = Path(__file__).resolve().parent.parent / "data" / "nemotron-pii" / "test-00000-of-00001.parquet"

# ISO 3166-1 alpha-2, abbreviated to what plausibly appears; membership is only
# used to report how often the BIC country position holds a real code.
ISO2 = set(
    "AD AE AF AG AI AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BI BJ BN BO BR BS BT BW BY BZ "
    "CA CD CF CG CH CI CL CM CN CO CR CU CV CY CZ DE DJ DK DM DO DZ EC EE EG ER ES ET FI FJ "
    "FR GA GB GD GE GH GM GN GQ GR GT GW GY HK HN HR HT HU ID IE IL IN IQ IR IS IT JM JO JP "
    "KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MG MH MK "
    "ML MM MN MR MT MU MV MW MX MY MZ NA NE NG NI NL NO NP NZ OM PA PE PG PH PK PL PT PY QA "
    "RO RS RU RW SA SB SC SD SE SG SI SK SL SM SN SO SR SS ST SV SY SZ TD TG TH TJ TL TM TN "
    "TR TT TW TZ UA UG US UY UZ VC VE VN VU WS YE ZA ZM ZW".split()
)

BIC_STRICT = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or len(digits) < 12:
        return False
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def aba_ok(d: str) -> bool:
    if not d.isdigit() or len(d) != 9:
        return False
    w = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    return sum(int(c) * k for c, k in zip(d, w)) % 10 == 0


pf = pq.ParquetFile(PATH)
TARGET = {"swift_bic", "bank_routing_number", "credit_debit_card"}

bic = Counter()
bic_len = Counter()
bic_bad = []
routing = Counter()
routing_len = Counter()
card = Counter()
card_len = Counter()
card_bad = []

# Pairing hypothesis: are us/intl the same documents rendered twice?
desc_by_locale = defaultdict(set)

rows = 0
for batch in pf.iter_batches(columns=["locale", "document_description", "text", "spans"], batch_size=2000):
    locales = batch.column("locale").to_pylist()
    descs = batch.column("document_description").to_pylist()
    texts = batch.column("text").to_pylist()
    spans_col = batch.column("spans").to_pylist()

    for loc, desc, text, spans_str in zip(locales, descs, texts, spans_col):
        rows += 1
        desc_by_locale[loc].add(desc)
        for span in ast.literal_eval(spans_str):
            label = span["label"]
            if label not in TARGET:
                continue
            s = text[span["start"]:span["end"]].strip()

            if label == "swift_bic":
                v = s.replace(" ", "").upper()
                bic_len[len(v)] += 1
                strict = bool(BIC_STRICT.match(v))
                bic["total"] += 1
                bic["strict_ok"] += strict
                bic["len_8_or_11"] += len(v) in (8, 11)
                bic["country_pos_iso"] += len(v) >= 6 and v[4:6] in ISO2
                if not strict and len(bic_bad) < 15:
                    bic_bad.append(v)

            elif label == "bank_routing_number":
                v = re.sub(r"\D", "", s)
                routing_len[len(v)] += 1
                routing["total"] += 1
                routing["nine_digits"] += len(v) == 9
                routing["aba_ok"] += aba_ok(v)

            elif label == "credit_debit_card":
                v = re.sub(r"\D", "", s)
                card_len[len(v)] += 1
                card["total"] += 1
                card["luhn_ok"] += luhn_ok(v)
                if not luhn_ok(v) and len(card_bad) < 15:
                    card_bad.append(s)

    print(f"\r{rows:,}", end="", file=sys.stderr)
print(file=sys.stderr)


def pct(n, d):
    return f"{n:,} / {d:,} = {100 * n / d:.1f}%" if d else "n/a"


print("\n=== SWIFT / BIC ===")
print("  strict ISO 9362 shape :", pct(bic["strict_ok"], bic["total"]))
print("  length 8 or 11        :", pct(bic["len_8_or_11"], bic["total"]))
print("  chars 5-6 a real ISO2 :", pct(bic["country_pos_iso"], bic["total"]))
print("  length distribution   :", dict(sorted(bic_len.items())))
print("  examples failing shape:", bic_bad)

print("\n=== BANK ROUTING NUMBER (ABA) ===")
print("  exactly 9 digits      :", pct(routing["nine_digits"], routing["total"]))
print("  ABA checksum passes   :", pct(routing["aba_ok"], routing["total"]))
print("  length distribution   :", dict(sorted(routing_len.items())))

print("\n=== CREDIT / DEBIT CARD ===")
print("  Luhn passes           :", pct(card["luhn_ok"], card["total"]))
print("  length distribution   :", dict(sorted(card_len.items())))
print("  examples failing Luhn :", card_bad)

print("\n=== PAIRING HYPOTHESIS ===")
us, intl = desc_by_locale["us"], desc_by_locale["intl"]
print(f"  distinct document_description  us={len(us):,}  intl={len(intl):,}")
print(f"  shared between locales         {len(us & intl):,}")
print(f"  jaccard                        {len(us & intl) / len(us | intl):.3f}")
