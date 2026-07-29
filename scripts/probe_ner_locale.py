"""Does off-the-shelf NER actually fail on Roman Urdu?

This is the premise behind adding Roman-Urdu templates to the generator. If it
is false -- if the models cope fine -- the argument for the change collapses
and English templates are enough.

Two entity types, tested independently:

  PERSON   the same 11 names appear in all three sentence styles, so the
           surrounding language is the only variable.

  ADDRESS  a 2x2. Sentence language (english / codeswitch / roman_urdu) is
           crossed with address FORM -- the same address written with English
           structural nouns ("House 12, Street 4") or Urdu ones ("Ghar 12,
           Gali 4"). Without that split, a drop could be caused by either and
           the result would not say which.

Three metrics, because they mean different things for a redaction firewall:

  exact     span boundaries match the gold span
  any-hit   some prediction overlaps it (generous -- "found the surname only"
            counts here, so reported failure is a floor, not an inflation)
  coverage  fraction of the gold span's CHARACTERS covered by predictions

Coverage is the one that matters operationally. Detecting "Islamabad" inside
"House 12, Street 4, F-8/3 Islamabad" is an any-hit success and a redaction
failure: the house number survives into the clean stream.
"""
from __future__ import annotations

import sys
from collections import defaultdict

# --------------------------------------------------------------------------- #
# PERSON cases -- names from the generator's own pools
# --------------------------------------------------------------------------- #
NAMES = [
    ("common", "Ayesha Malik"),
    ("common", "Salman Baig"),
    ("common", "Hamza Qureshi"),
    ("common", "Fatima Sheikh"),
    ("common", "Bilal Awan"),
    ("rare", "Mahnoor Warraich"),
    ("rare", "Areeba Cheema"),
    ("rare", "Laiba Bhatti"),
    # First names that are also ordinary Urdu nouns (noor = light, iman =
    # faith). transactions.py:17 says these exist to stress Tier 2.
    ("ambiguous", "Noor Khan"),
    ("ambiguous", "Iman Raza"),
    ("ambiguous", "Eman Gill"),
]

NAME_TEMPLATES = {
    # Mirrors the generator's current MEMO_TEMPLATES.
    "english": [
        "Transfer to {x}",
        "Salary for {x}",
        "Eidi for {x}",
        "Rent payment from {x}, contact 03001234567",
        "Sent by {x} for invoice #40321",
    ],
    "codeswitch": [
        "Salary transfer kar di hai for {x}",
        "Rent payment {x} ko bhej diya",
        "Please {x} ko 5000 bhej dein",
        "{x} ka invoice payment kar diya, contact karein",
        "Transfer done for {x}, baqi kal",
    ],
    "roman_urdu": [
        "{x} ko paisay bhej diye hain",
        "Ghar ka kiraya {x} ko transfer kiya",
        "{x} ki salary jama karwa di hai",
        "{x} ka bijli ka bill ada kar diya",
        "Bhai {x} ko rakam bhaij di",
    ],
}

# --------------------------------------------------------------------------- #
# ADDRESS cases -- Pakistani formats, paired so that each Urdu-form address is
# the same address as its English-form counterpart with only the structural
# nouns swapped (House->Ghar, Street->Gali, Flat->Makan). That pairing is what
# makes the two forms comparable.
# --------------------------------------------------------------------------- #
ADDRESSES = [
    ("english_form", "House 12, Street 4, F-8/3 Islamabad"),
    ("english_form", "Flat 3B, Askari Tower, Gulberg III, Lahore"),
    ("english_form", "House No. 221, Sector G-9/1, Islamabad"),
    ("urdu_form", "Ghar 12, Gali 4, F-8/3 Islamabad"),
    ("urdu_form", "Makan 3B, Askari Tower, Gulberg III, Lahore"),
    ("urdu_form", "Makan No. 221, Sector G-9/1, Islamabad"),
]

ADDRESS_TEMPLATES = {
    "english": [
        "Deliver statement to {x}",
        "Address on file: {x}",
        "Cheque posted to {x}",
        "Customer resides at {x}",
    ],
    "codeswitch": [
        "Statement {x} par bhej dein",
        "Address update kar diya: {x}",
        "Cheque {x} par post kiya",
        "Customer {x} par rehta hai",
    ],
    "roman_urdu": [
        "{x} par statement bhej dein",
        "Pata: {x}",
        "Cheque {x} par bhaij diya",
        "Customer {x} mein rehta hai",
    ],
}

STYLES = ("english", "codeswitch", "roman_urdu")


def build_cases():
    """(entity_type, style, band, value, text, gold_start, gold_end)"""
    cases = []
    for style, templates in NAME_TEMPLATES.items():
        for template in templates:
            for band, name in NAMES:
                text = template.format(x=name)
                s = text.index(name)
                cases.append(("PERSON", style, band, name, text, s, s + len(name)))

    for style, templates in ADDRESS_TEMPLATES.items():
        for template in templates:
            for form, addr in ADDRESSES:
                text = template.format(x=addr)
                s = text.index(addr)
                cases.append(("ADDRESS", style, form, addr, text, s, s + len(addr)))
    return cases


def char_coverage(gold_start, gold_end, preds) -> float:
    """Fraction of the gold span's characters covered by any prediction."""
    gold = set(range(gold_start, gold_end))
    if not gold:
        return 0.0
    covered = set()
    for p_start, p_end, _ in preds:
        covered |= set(range(p_start, p_end)) & gold
    return len(covered) / len(gold)


# --------------------------------------------------------------------------- #
# Model adapters: text -> [(start, end, label)] restricted to the entity type
# --------------------------------------------------------------------------- #
def load_bert_ner():
    from transformers import pipeline

    ner = pipeline("ner", model="dslim/bert-base-NER",
                   aggregation_strategy="simple", device=-1)
    # CoNLL has no address class; LOC is the closest thing it can offer, and
    # that mismatch is itself part of the finding.
    wanted = {"PERSON": {"PER"}, "ADDRESS": {"LOC"}}

    def predict(text: str, entity_type: str):
        return [
            (e["start"], e["end"], e["entity_group"])
            for e in ner(text)
            if e["entity_group"] in wanted[entity_type]
        ]

    return predict


def load_gliner(model_id: str):
    from gliner import GLiNER

    model = GLiNER.from_pretrained(model_id)
    labels = {
        "PERSON": ["person", "first_name", "last_name"],
        "ADDRESS": ["address", "street_address", "location"],
    }

    def predict(text: str, entity_type: str):
        return [
            (e["start"], e["end"], e["label"])
            for e in model.predict_entities(text, labels[entity_type], threshold=0.4)
        ]

    return predict


MODELS = [
    ("dslim/bert-base-NER", load_bert_ner),
    ("urchade/gliner_multi_pii-v1", lambda: load_gliner("urchade/gliner_multi_pii-v1")),
    ("nvidia/gliner-PII", lambda: load_gliner("nvidia/gliner-PII")),
]


def main() -> int:
    # Optional substring filter so a single model can be rerun without
    # re-downloading the others.
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    if wanted:
        globals()["MODELS"] = [m for m in MODELS if wanted.lower() in m[0].lower()]

    cases = build_cases()
    n_person = sum(1 for c in cases if c[0] == "PERSON")
    n_addr = len(cases) - n_person
    print(f"{len(cases)} cases: {n_person} PERSON, {n_addr} ADDRESS\n")

    for model_name, loader in MODELS:
        print("=" * 78)
        print(model_name)
        print("=" * 78)
        try:
            predict = loader()
        except Exception as exc:
            print(f"  SKIPPED: {type(exc).__name__}: {str(exc)[:200]}\n")
            continue

        stats = defaultdict(lambda: {"exact": 0, "hit": 0, "n": 0, "cov": 0.0})
        bands = defaultdict(lambda: {"hit": 0, "n": 0, "cov": 0.0})
        examples = defaultdict(list)

        for etype, style, band, value, text, gs, ge in cases:
            try:
                preds = predict(text, etype)
            except Exception as exc:
                print(f"  prediction failed: {exc}")
                continue

            hits = [p for p in preds if gs < p[1] and p[0] < ge]
            exact = any(p[0] == gs and p[1] == ge for p in hits)
            cov = char_coverage(gs, ge, hits)

            key = (etype, style)
            stats[key]["n"] += 1
            stats[key]["cov"] += cov
            bands[(etype, style, band)]["n"] += 1
            bands[(etype, style, band)]["cov"] += cov
            if exact:
                stats[key]["exact"] += 1
            if hits:
                stats[key]["hit"] += 1
                bands[(etype, style, band)]["hit"] += 1

            if not exact and len(examples[key]) < 3:
                got = ", ".join(f"{text[p[0]:p[1]]!r}" for p in hits) or "nothing"
                examples[key].append(f"{value!r} in {text!r}\n        -> {got}  (coverage {cov:.0%})")

        for etype in ("PERSON", "ADDRESS"):
            print(f"\n  [{etype}]")
            print(f"  {'sentence style':<14} {'n':>4} {'exact':>8} {'any-hit':>9} {'char coverage':>15}")
            for style in STYLES:
                s = stats[(etype, style)]
                if not s["n"]:
                    continue
                print(f"  {style:<14} {s['n']:>4} {s['exact'] / s['n']:>7.0%} "
                      f"{s['hit'] / s['n']:>8.0%} {s['cov'] / s['n']:>14.0%}")

            group = ("common", "rare", "ambiguous") if etype == "PERSON" else ("english_form", "urdu_form")
            label = "name difficulty" if etype == "PERSON" else "address form"
            for metric, k in (("any-hit", "hit"), ("char coverage", "cov")):
                print(f"\n  {metric} by {label}:")
                print(f"  {'':<14} " + "".join(f"{g:>14}" for g in group))
                for style in STYLES:
                    row = f"  {style:<14} "
                    for g in group:
                        d = bands[(etype, style, g)]
                        row += f"{d[k] / d['n']:>13.0%} " if d["n"] else f"{'--':>14}"
                    print(row)

            for style in STYLES:
                if examples[(etype, style)]:
                    print(f"\n  non-exact examples [{style}]:")
                    for e in examples[(etype, style)]:
                        print(f"     {e}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
