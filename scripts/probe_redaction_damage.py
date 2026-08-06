"""Where does a redacted memo stop being a memo?

Decision from the section 8 review: keep threshold 0.25 (coverage is not traded
away), accept the resulting over-redaction, and mitigate it with a review queue
instead. That turns "which records go to review" into a boundary that has to be
picked, and picking it by eye would put a made-up number in the routing path.

The constraint that shapes everything here: AT RUNTIME THERE IS NO GROUND TRUTH.
The processor cannot know whether a masked span was a real name or a false
positive -- if it could, it would not need the model. The only quantity it can
actually compute is how much of the memo the model masked. So the boundary must
be a function of masked fraction alone, and the honest question is not "can this
separate correct from incorrect redaction" (it cannot) but:

    at what masked fraction does the record stop being useful downstream,
    and who lands above that line?

Both corpora are scored, because a boundary tuned on one is meaningless:

  positive   probe_ner_locale.build_cases() -- every case contains a real
             PERSON or ADDRESS. Masking here is mostly CORRECT, so these are
             the cost side: every positive above the boundary is a reviewer
             looking at a record the pipeline already handled properly.

  negative   probe_ner_precision.build_negatives() -- no PERSON, no ADDRESS
             anywhere. Masking here is pure damage, so these are the yield
             side: the records a reviewer can actually repair.

Scored with BOTH label sets on every case, unlike probe_ner_locale which scores
each case only against its own entity type. Production runs both, so a PERSON
memo also absorbs whatever the ADDRESS labels fire on, and that surplus is part
of the damage being measured.

pipeline_field negatives are carried but EXCLUDED from the boundary analysis.
Tier 2 is scoped to free text now, so channel/cnic/iban values never reach the
model; leaving them in would let 13 cases the router never sees drag the
boundary around. They are reported separately as a check on that scoping
decision.

Usage:
    python scripts/probe_redaction_damage.py [--out damage_results.json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_ner_locale import build_cases  # noqa: E402
from probe_ner_precision import build_negatives, redacted_char_count  # noqa: E402

MODEL_ID = "urchade/gliner_multi_pii-v1"
THRESHOLD = 0.25          # the operating point chosen in section 6, held fixed
LABELS = {
    "PERSON": ["person", "first_name", "last_name"],
    "ADDRESS": ["address", "street_address", "location"],
}

# Candidate boundaries. Spread across the whole range rather than clustered
# near a guess, so the report shows the shape of the tradeoff instead of
# confirming a number picked in advance.
BOUNDARIES = (0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00)


def predict_spans(model, text):
    """Every span production would mask: both label sets, unioned later."""
    spans = []
    for etype, labels in LABELS.items():
        for e in model.predict_entities(text, labels, threshold=THRESHOLD):
            spans.append((e["start"], e["end"], etype))
    return spans


def type_conflict(text, spans) -> float:
    """Fraction of masked characters claimed as BOTH a person and an address.

    A second signal, independent of how much was masked. PERSON and ADDRESS are
    mutually exclusive in reality -- a run of characters is somebody's name or
    it is where they live, never both -- so a character carrying both labels is
    the model contradicting itself rather than finding two things. That makes
    it a candidate damage detector for the case masked fraction cannot see:
    partial over-redaction, where a negative is mangled but not destroyed and
    therefore looks exactly like a correct redaction by size alone.
    """
    by_type = {}
    for s, e, etype in spans:
        by_type.setdefault(etype, set()).update(range(max(0, s), min(len(text), e)))
    if len(by_type) < 2:
        return 0.0
    person, address = by_type.get("PERSON", set()), by_type.get("ADDRESS", set())
    union = person | address
    return len(person & address) / len(union) if union else 0.0


def measure_cases(model):
    """One row per case: masked fraction, plus what the masking landed on.

    surplus_frac is computed only for positives and only for the report -- it
    needs the gold span, so it is not available to the router. It answers a
    different question: of the damage on a record that genuinely needed
    redacting, how much was collateral.
    """
    rows = []

    for etype, style, band, value, text, gs, ge in build_cases():
        spans = predict_spans(model, text)
        masked = redacted_char_count(text, spans)
        gold = set(range(gs, ge))
        covered = set()
        for s, e, _ in spans:
            covered |= set(range(max(0, s), min(len(text), e)))
        surplus = len(covered - gold)
        rows.append({
            "kind": "positive",
            "group": f"{etype.lower()}_{style}",
            "style": style,
            "band": band,
            "text": text,
            "chars": len(text),
            "masked": masked,
            "masked_frac": masked / len(text),
            "gold_covered_frac": len(covered & gold) / len(gold) if gold else 0.0,
            "surplus_frac": surplus / len(text),
            "conflict_frac": type_conflict(text, spans),
        })

    for category, subkey, text in build_negatives():
        spans = predict_spans(model, text)
        masked = redacted_char_count(text, spans)
        rows.append({
            "kind": "negative",
            "group": category if category != "pk_negative" else f"pk_{subkey}",
            "category": category,
            "style": subkey,
            "text": text,
            "chars": len(text),
            "masked": masked,
            "masked_frac": masked / len(text) if text else 0.0,
            "gold_covered_frac": None,
            "surplus_frac": masked / len(text) if text else 0.0,
            "conflict_frac": type_conflict(text, spans),
        })

    return rows


def pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def distribution_table(rows):
    print("\n" + "=" * 86)
    print("masked fraction by group  (what share of the memo the model removed)")
    print("=" * 86)
    print(f"{'group':<24} {'n':>4} {'mean':>7} {'p50':>7} {'p90':>7} {'max':>7} "
          f"{'untouched':>10}")
    print("-" * 86)
    by_group = defaultdict(list)
    for r in rows:
        by_group[(r["kind"], r["group"])].append(r["masked_frac"])
    for (kind, group), vals in sorted(by_group.items()):
        untouched = sum(1 for v in vals if v == 0.0) / len(vals)
        print(f"{kind[:3]}/{group:<19} {len(vals):>4} {statistics.mean(vals):>6.0%} "
              f"{pct(vals, 0.5):>6.0%} {pct(vals, 0.9):>6.0%} {max(vals):>6.0%} "
              f"{untouched:>9.0%}")


def boundary_sweep(rows):
    """The actual decision table.

    flagged_neg   negatives above the boundary -- reviewer work with a payoff,
                  since these records were damaged and can be restored.
    flagged_pos   positives above it -- reviewer work with no payoff, since the
                  pipeline already did the right thing to these.
    yield         share of the review queue that is repairable damage. This is
                  the number that says whether a reviewer's time is worth
                  spending at this boundary.
    """
    pos = [r for r in rows if r["kind"] == "positive"]
    neg = [r for r in rows
           if r["kind"] == "negative" and r.get("category") != "pipeline_field"]
    total_neg_damaged = sum(1 for r in neg if r["masked_frac"] > 0)

    print("\n" + "=" * 86)
    print("boundary sweep  (flag a record for review when masked_frac >= B)")
    print(f"free-text cases only: {len(pos)} positive, {len(neg)} negative "
          f"({total_neg_damaged} of the negatives are damaged at all)")
    print("=" * 86)
    print(f"{'B':>6} {'flagged':>9} {'of pos':>9} {'of neg':>9} "
          f"{'damage caught':>14} {'queue yield':>12}")
    print("-" * 86)
    out = {}
    for b in BOUNDARIES:
        fp = sum(1 for r in pos if r["masked_frac"] >= b)
        fn = sum(1 for r in neg if r["masked_frac"] >= b)
        flagged = fp + fn
        caught = fn / total_neg_damaged if total_neg_damaged else 0.0
        yld = fn / flagged if flagged else 0.0
        share = flagged / (len(pos) + len(neg))
        out[str(b)] = {"flagged": flagged, "flagged_share": share,
                       "flagged_pos": fp, "flagged_neg": fn,
                       "damage_caught": caught, "queue_yield": yld}
        print(f"{b:>6.2f} {share:>8.0%} {fp / len(pos):>8.0%} "
              f"{fn / len(neg):>8.0%} {caught:>13.0%} {yld:>11.0%}")
    return out


def conflict_analysis(rows):
    """Does self-contradiction flag damage that size alone misses?

    Reported as a rate per group, then as the combined rule, because the two
    signals are only worth carrying together if the conflict catches records the
    saturation check does not.
    """
    pos = [r for r in rows if r["kind"] == "positive"]
    neg = [r for r in rows
           if r["kind"] == "negative" and r.get("category") != "pipeline_field"]

    print("\n" + "=" * 86)
    print("type conflict  (chars labelled BOTH person and address -- model contradicting itself)")
    print("=" * 86)
    print(f"{'group':<24} {'n':>4} {'any conflict':>14} {'mean frac':>11}")
    print("-" * 86)
    by_group = defaultdict(list)
    for r in pos + neg:
        by_group[(r["kind"], r["group"])].append(r["conflict_frac"])
    for (kind, group), vals in sorted(by_group.items()):
        rate = sum(1 for v in vals if v > 0) / len(vals)
        print(f"{kind[:3]}/{group:<19} {len(vals):>4} {rate:>13.0%} "
              f"{statistics.mean(vals):>10.0%}")

    print("\n" + "=" * 86)
    print("combined rule:  flag when masked_frac >= 1.0  OR  conflict_frac >= C")
    print("=" * 86)
    print(f"{'C':>6} {'flagged':>9} {'of pos':>9} {'of neg':>9} "
          f"{'damage caught':>14} {'queue yield':>12}")
    print("-" * 86)
    total_damaged = sum(1 for r in neg if r["masked_frac"] > 0)
    out = {}
    for c in (0.01, 0.10, 0.25, 0.50, 1.01):   # 1.01 = conflict disabled
        def flag(r):
            return r["masked_frac"] >= 1.0 or r["conflict_frac"] >= c
        fp = sum(1 for r in pos if flag(r))
        fn = sum(1 for r in neg if flag(r))
        flagged = fp + fn
        caught = fn / total_damaged if total_damaged else 0.0
        yld = fn / flagged if flagged else 0.0
        label = "off" if c > 1 else f"{c:.2f}"
        out[label] = {"flagged_pos": fp, "flagged_neg": fn,
                      "damage_caught": caught, "queue_yield": yld}
        print(f"{label:>6} {flagged / (len(pos) + len(neg)):>8.0%} "
              f"{fp / len(pos):>8.0%} {fn / len(neg):>8.0%} "
              f"{caught:>13.0%} {yld:>11.0%}")
    return out


def worst_offenders(rows, n=10):
    print("\n" + "=" * 86)
    print(f"most-damaged negatives  (no PII present, so every masked char is loss)")
    print("=" * 86)
    neg = [r for r in rows
           if r["kind"] == "negative" and r.get("category") != "pipeline_field"]
    for r in sorted(neg, key=lambda x: -x["masked_frac"])[:n]:
        if r["masked_frac"] == 0:
            break
        print(f"  {r['masked_frac']:>4.0%}  {r['text']!r}")

    print("\n" + "=" * 86)
    print("positives with the highest collateral  (real entity, plus surplus)")
    print("=" * 86)
    pos = [r for r in rows if r["kind"] == "positive"]
    for r in sorted(pos, key=lambda x: -x["surplus_frac"])[:n]:
        if r["surplus_frac"] == 0:
            break
        print(f"  masked {r['masked_frac']:>4.0%} (surplus {r['surplus_frac']:>4.0%})"
              f"  {r['text']!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="damage_results.json")
    args = ap.parse_args(argv)

    from gliner import GLiNER

    print(f"loading {MODEL_ID} (fp32, threshold {THRESHOLD})", flush=True)
    model = GLiNER.from_pretrained(MODEL_ID)
    model.eval()

    rows = measure_cases(model)
    print(f"scored {len(rows)} cases", flush=True)

    distribution_table(rows)
    sweep = boundary_sweep(rows)
    combined = conflict_analysis(rows)
    worst_offenders(rows)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"model": MODEL_ID, "threshold": THRESHOLD,
                   "boundaries": sweep, "combined_rule": combined,
                   "rows": rows}, fh, indent=2)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
