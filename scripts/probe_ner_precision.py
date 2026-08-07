"""How much clean text does Tier 2 destroy?

Every case in probe_ner_locale.py and probe_ner_sweep.py contains a real
entity, so both report coverage and neither reports precision. That makes the
standing argument for a low threshold -- "a false positive costs one
over-redacted string, a false negative is a leak" (docs/decisions.md section 3)
-- an assertion rather than a measurement. If threshold 0.25 redacts half of
every memo, the clean topic stops being useful to whatever consumes it and the
argument stops holding.

This is the negative half. Every input here contains NO person and NO address,
so any PERSON or ADDRESS prediction is by construction a false positive.

Three sources, matching where Tier 2 would actually be pointed:

  pipeline_field  the non-memo payload fields a naive drop-in would scan
                  (processor.py:234 runs the detector over every string field).
                  channel is an enum; the identifier fields hold PII, but not
                  of these two types.

  clean_memo      the generator's own memo templates that embed no name --
                  5 of 10 at transactions.py:35. This is the case that decides
                  the escalation economics, since roughly half of all real
                  memos look like this.

  pk_negative     hand-written Roman Urdu and code-switched memos with no PII.
                  A deliberate subset uses Urdu words that are also Pakistani
                  first names ("noor" = light, "iman" = faith, "sana" = praise)
                  as ordinary nouns. transactions.py:19 says those names exist
                  in the generator to stress Tier 2; this tests the reverse
                  direction, which is where over-redaction would come from.

Two numbers per configuration, because they answer different questions:

  fp_input_rate   share of inputs with at least one spurious span. "How often
                  does this fire when it shouldn't."
  over_redaction  share of all characters that would be masked. "How mangled
                  is the surviving text." This is the one that decides whether
                  the clean topic is still worth consuming.

Reported against the same 0.25 / 0.40 / 0.55 thresholds as probe_ner_sweep.py,
so precision here pairs with coverage there into an actual tradeoff curve
rather than two unrelated tables.

Caveat stated up front: the pk_negative memos are written by the author, who
is not a native writer of Roman Urdu -- the same limitation
docs/tier2-detection-findings.md section 5 records for the positive cases.

Usage:
    python scripts/probe_ner_precision.py [--out precision_results.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

# (name, model_id, quantize_int8, thresholds)
#
# The int8 row carries its own thresholds. probe_ner_runtime.py section 7.2
# found quantization compresses the score distribution ~20x toward zero, so
# scoring it at the fp32 thresholds would measure a model that predicts
# nothing and report perfect precision -- a false pass, and the opposite error
# to the false failure that same compression causes on the recall side. It is
# the leading candidate on cost (20.7 ms/record vs 40.9), and it emits 24% more
# spans per case than fp32, which is exactly the surplus that has to be shown
# to be harmless here or the speedup is not real.
CONFIGS = [
    ("urchade_fp32", "urchade/gliner_multi_pii-v1", False, (0.25, 0.40, 0.55)),
    ("urchade_int8", "urchade/gliner_multi_pii-v1", True, (0.002, 0.005, 0.01)),
    ("nvidia_fp32", "nvidia/gliner-PII", False, (0.25, 0.40, 0.55)),
]
LABELS = {
    "PERSON": ["person", "first_name", "last_name"],
    "ADDRESS": ["address", "street_address", "location"],
}

# --------------------------------------------------------------------------- #
# Negative corpus. Nothing here contains a person name or a street address.
# --------------------------------------------------------------------------- #

# Values shaped exactly like generator output (transactions.py:104). The
# identifier fields do hold PII -- but CNIC/IBAN/phone/email, which Tier 1
# already covers with checksums. A PERSON hit on them is a mislabel.
PIPELINE_FIELDS = [
    ("channel", "mobile_app"),
    ("channel", "branch"),
    ("channel", "atm"),
    ("channel", "raast"),
    ("cnic", "3520112345671"),
    ("cnic", "42101-9876543-2"),
    ("iban_from", "PK36HABB0000012345678901"),
    ("iban_to", "PK24MEZN0000098765432109"),
    ("phone", "03001234567"),
    ("phone", "+92 3211234567"),
    ("email", "ayesha.malik@example.com"),
    ("email", "noreply@hbl.com.pk"),
    ("amount_pkr", "45231.75"),
]

# The 5 of 10 templates at transactions.py:35 that embed no {name}. The last
# two carry a Tier 1 entity but still no person or address.
CLEAN_MEMOS = [
    "Zakat contribution",
    "Utility bill payment",
    "Loan installment",
    "Refund processed, notify at ayesha.malik@example.com",
    "Payment against order #40321, contact 03001234567",
]

# Roman Urdu / code-switched, no PII of any kind.
PK_NEGATIVE_PLAIN = [
    "Bijli ka bill ada kar diya",
    "Gas ka bill jama karwa diya hai",
    "Paisay bhej diye hain",
    "Kiraya agle mahine bhejunga",
    "Salary abhi tak nahi aayi",
    "Transfer complete ho gaya hai",
    "Invoice ka payment kar diya",
    "Qarza ki qist ada kar di",
    "Zakat ki raqam jama karwa di",
    "Rakam wapas bhej dein",
    "Account mein paisay aa gaye hain",
    "Bank ne charges kaat liye hain",
    "Mobile load karwa diya",
    "Pani ka bill pending hai",
    "Dukan ka hisab clear kar diya",
]

# Words that are ordinary Urdu nouns AND Pakistani first names, used here in
# their noun sense. transactions.py:19 puts these names in the generator to
# stress Tier 2; this is the same ambiguity pointed the other way, and it is
# where spurious PERSON hits are most likely.
PK_NEGATIVE_AMBIGUOUS = [
    "Noor ki roshni mein kaam hua",
    "Iman ke sath kaam karna chahiye",
    "Sana karne wale bohot hain",
    "Aman se masla hal ho gaya",
    "Sabr ka phal meetha hota hai",
    "Shan se rehna chahiye",
    "Fajar ke waqt bhej dena",
    "Noor wala bill ada kar diya",
    "Iman ki baat hai yeh",
    "Rehmat ho gayi hai",
]


def build_negatives():
    """(category, field_or_style, text) -- none contain PERSON or ADDRESS."""
    cases = []
    for field, value in PIPELINE_FIELDS:
        cases.append(("pipeline_field", field, value))
    for memo in CLEAN_MEMOS:
        cases.append(("clean_memo", "memo", memo))
    for memo in PK_NEGATIVE_PLAIN:
        cases.append(("pk_negative", "plain", memo))
    for memo in PK_NEGATIVE_AMBIGUOUS:
        cases.append(("pk_negative", "ambiguous", memo))
    return cases


def redacted_char_count(text, preds) -> int:
    """Characters that would be masked, counting overlaps once.

    Spans are unioned rather than summed because Tier 2 emits overlapping
    candidates and redaction masks a character once. Summing span lengths
    would report over-redaction above 100%.
    """
    covered = set()
    for start, end, _label in preds:
        covered |= set(range(max(0, start), min(len(text), end)))
    return len(covered)


def probe(model, cases, threshold) -> dict:
    agg = defaultdict(lambda: {"n": 0, "fp_inputs": 0, "n_pred": 0,
                               "chars": 0, "red_chars": 0})
    examples = defaultdict(list)

    for category, subkey, text in cases:
        preds = []
        for etype, labels in LABELS.items():
            for e in model.predict_entities(text, labels, threshold=threshold):
                preds.append((e["start"], e["end"], f"{etype}:{e['label']}"))

        red = redacted_char_count(text, preds)
        for key in ((category, "ALL"), (category, subkey)):
            d = agg[key]
            d["n"] += 1
            d["n_pred"] += len(preds)
            d["chars"] += len(text)
            d["red_chars"] += red
            d["fp_inputs"] += bool(preds)

        if preds and len(examples[category]) < 6:
            got = ", ".join(f"{text[s:e]!r}->{lab}" for s, e, lab in preds)
            examples[category].append(f"{text!r}  =>  {got}  ({red}/{len(text)} chars)")

    return {
        "cells": {
            "|".join(k): {
                "n": v["n"],
                "fp_input_rate": v["fp_inputs"] / v["n"],
                "preds_per_input": v["n_pred"] / v["n"],
                "over_redaction": v["red_chars"] / v["chars"] if v["chars"] else 0.0,
                "chars": v["chars"], "red_chars": v["red_chars"],
            }
            for k, v in agg.items()
        },
        "examples": dict(examples),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="precision_results.json")
    ap.add_argument("--only", nargs="*", default=None,
                    help="config names to run (default: all)")
    args = ap.parse_args(argv)

    from gliner import GLiNER

    cases = build_negatives()
    n_by_cat = defaultdict(int)
    for c, _s, _t in cases:
        n_by_cat[c] += 1
    print(f"{len(cases)} negative cases: {dict(n_by_cat)}\n", flush=True)

    configs = [c for c in CONFIGS if not args.only or c[0] in args.only]

    results = {}
    for name, model_id, do_quant, thresholds in configs:
        print("=" * 74, f"\n{name}  ({model_id}"
              f"{', int8' if do_quant else ''})\n", "=" * 74, flush=True)
        model = GLiNER.from_pretrained(model_id)
        model.eval()
        if do_quant:
            model.quantize(dtype="int8")
        entry = {"model_id": model_id, "int8": do_quant}
        for thr in thresholds:
            r = probe(model, cases, thr)
            entry[str(thr)] = r
            print(f"\n  threshold {thr}", flush=True)
            print(f"  {'category':<16} {'n':>4} {'fires':>8} {'over-redacted':>15} "
                  f"{'preds/input':>12}", flush=True)
            for cat in ("pipeline_field", "clean_memo", "pk_negative"):
                c = r["cells"].get(f"{cat}|ALL")
                if not c:
                    continue
                print(f"  {cat:<16} {c['n']:>4} {c['fp_input_rate']:>7.0%} "
                      f"{c['over_redaction']:>14.0%} {c['preds_per_input']:>12.2f}",
                      flush=True)
            for cat, ex in r["examples"].items():
                if ex:
                    print(f"\n    false positives [{cat}]:", flush=True)
                    for e in ex:
                        print(f"      {e}", flush=True)
        results[name] = entry
        del model
        print(flush=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
