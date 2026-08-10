"""Is there anything for Tier 3 to escalate on?

`decisions.md` §11 settles Tier 3's shape: genuine escalation, same span,
promoted on uncertainty. §12 pre-registers the failure condition -- *"if it
exceeds ~1-2% of messages the cost argument for tiering collapses"*.

Both assume an uncertainty signal exists. This project has tried once and
failed: §10.1 found masked fraction does not separate good redaction from bad,
and does it backwards. The only untested candidate is the encoder's own
confidence score, and Tier 3 cannot be designed until it is tested.

THREE QUESTIONS, in the order that can kill the tier fastest:

  1. Is confidence predictive?   Are low-confidence spans wrong more often than
                                 high-confidence ones? If the curve is flat,
                                 there is no trigger and Tier 3 has no design.

  2. Can the trigger SEE the       A firewall's dangerous error is the leak, not
     dangerous error?              the over-redaction. A leak is a span the
                                   encoder never produced -- so there is no
                                   score to be low. This asks what share of
                                   leaking records carry any low-confidence span
                                   at all.

  3. What does it cost?           Share of records escalated at each threshold.
                                  Above ~2%, §12 says tiering stops paying.

Question 2 is the one that matters and it is structural. Confidence-based
escalation can only ever reach errors the model already made a span for.

Usage:
    python scripts/probe_tier3_trigger.py [--count 600]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from probe_address_in_stream import WITH_ADDRESS, build_stream  # noqa: E402

from pipelineguard.config import settings  # noqa: E402
from pipelineguard.detectors.tier1_rules import RulesDetector  # noqa: E402
from pipelineguard.detectors.tier2_encoder import (  # noqa: E402
    extend_address_span,
)

# Where a graded band would plausibly sit. The shipped threshold is 0.55, so
# every span scored here is already one production would act on.
BANDS = [(0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95), (0.95, 1.01)]


def band_of(score: float) -> str:
    for low, high in BANDS:
        if low <= score < high:
            return f"{low:.2f}-{high:.2f}"
    return "other"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=600,
                    help="memos of each kind: address-bearing and address-free")
    ap.add_argument("--out", default="tier3_trigger.json")
    args = ap.parse_args(argv)

    from gliner import GLiNER
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = GLiNER.from_pretrained(settings.tier2_model,
                                   revision=settings.tier2_model_revision,
                                   map_location=device)
    model.eval()
    tier1 = RulesDetector()

    rng = random.Random(20260810)
    random.seed(20260810)
    bearing, clean = build_stream(args.count, rng)

    # (text, {gold identifying positions}) for both populations. Address-bearing
    # memos carry an address and usually a name; address-free memos carry the
    # structured PII the templates injected.
    records = []
    for text, gs, ge, ns, ne in bearing:
        gold = {p for p in range(gs, ge) if text[p].isalnum()}
        if ns is not None:
            gold |= {p for p in range(ns, ne) if text[p].isalnum()}
        records.append((text, gold))
    for text, intended in clean:
        records.append((text, {p for p in intended if text[p].isalnum()}))

    print(f"{len(records)} memos  model={settings.tier2_model}@"
          f"{settings.tier2_model_revision[:12]}  device={device}\n", flush=True)

    span_stats = defaultdict(lambda: {"n": 0, "correct": 0})
    per_record = []          # (leaked_chars, min_score, n_spans)
    fp_scores, tp_scores = [], []

    for start in range(0, len(records), 16):
        chunk = records[start:start + 16]
        texts = [r[0] for r in chunk]
        by_index: dict[int, list] = defaultdict(list)
        for entity_type, labels in WITH_ADDRESS.items():
            for i, ents in enumerate(model.batch_predict_entities(
                    texts, labels, threshold=settings.tier2_threshold)):
                for e in ents:
                    s, x = ((extend_address_span(texts[i], e["start"], e["end"]))
                            if entity_type == "ADDRESS"
                            else (e["start"], e["end"]))
                    by_index[i].append((s, x, float(e["score"])))

        for i, (text, gold) in enumerate(chunk):
            spans = by_index.get(i, [])
            # Tier 1 redacts the same memo, and a leak has to survive BOTH
            # tiers to be a leak. Scoring Tier 2 alone reported 6.1% of records
            # leaking when production leaks 0.25%, because phones, CNICs and
            # emails the rules already remove were being counted as misses.
            covered = {p
                       for f in tier1.detect(text, "memo")
                       for p in range(f.span_start, f.span_end)} & gold
            scores = []
            for s, x, score in spans:
                positions = set(range(s, x))
                covered |= positions & gold
                # A span is CORRECT if it touches gold PII at all. Deliberately
                # generous: the question is whether confidence flags spans that
                # are wrong, and a strict boundary test would score correct
                # detections as errors and flatter the signal.
                correct = bool(positions & gold)
                bucket = span_stats[band_of(score)]
                bucket["n"] += 1
                bucket["correct"] += correct
                (tp_scores if correct else fp_scores).append(score)
                scores.append(score)

            per_record.append((len(gold - covered),
                               min(scores) if scores else None,
                               len(spans)))

    # ---- 1. is confidence predictive? -----------------------------------
    print("1. PRECISION BY CONFIDENCE BAND")
    print(f"   {'band':<14}{'spans':>8}{'precision':>12}")
    for low, high in BANDS:
        key = f"{low:.2f}-{high:.2f}"
        b = span_stats[key]
        if b["n"]:
            print(f"   {key:<14}{b['n']:>8,}{b['correct'] / b['n']:>11.1%}")
    total = sum(b["n"] for b in span_stats.values())
    wrong = sum(b["n"] - b["correct"] for b in span_stats.values())
    print(f"   {'ALL':<14}{total:>8,}{1 - wrong / total:>11.1%}"
          f"   ({wrong:,} wrong spans)")

    # ---- 2. can the trigger see a leak? ---------------------------------
    leaking = [r for r in per_record if r[0] > 0]
    clean_recs = [r for r in per_record if r[0] == 0]
    print(f"\n2. CAN THE TRIGGER SEE THE LEAK?")
    print(f"   records leaking identifying characters: {len(leaking):,} of "
          f"{len(per_record):,} ({len(leaking) / len(per_record):.1%})")
    for threshold in (0.65, 0.75, 0.85):
        flagged_leak = sum(1 for lk, lo, _ in leaking
                           if lo is not None and lo < threshold)
        flagged_ok = sum(1 for lk, lo, _ in clean_recs
                         if lo is not None and lo < threshold)
        recall = flagged_leak / len(leaking) if leaking else 0
        escalated = (flagged_leak + flagged_ok) / len(per_record)
        print(f"   escalate if any span < {threshold:.2f} : "
              f"catches {recall:>5.1%} of leaking records, "
              f"escalates {escalated:>5.1%} of the stream")
    no_span = sum(1 for lk, lo, n in leaking if n == 0)
    print(f"   leaking records with NO span at all: {no_span:,} "
          f"({no_span / len(leaking):.1%}) -- invisible to any confidence rule")

    # ---- 3. what does it cost? ------------------------------------------
    print("\n3. ESCALATION RATE (§12: above ~2% tiering stops paying)")
    for threshold in (0.60, 0.65, 0.70, 0.75, 0.85):
        rate = sum(1 for _lk, lo, _n in per_record
                   if lo is not None and lo < threshold) / len(per_record)
        verdict = "ok" if rate <= 0.02 else "COLLAPSES the cost argument"
        print(f"   < {threshold:.2f} : {rate:>6.1%} of records   {verdict}")

    payload = {
        "model": settings.tier2_model,
        "revision": settings.tier2_model_revision,
        "records": len(per_record),
        "precision_by_band": {k: dict(v) for k, v in span_stats.items()},
        "leaking_records": len(leaking),
        "leaking_with_no_span": no_span,
        "mean_score_correct": sum(tp_scores) / len(tp_scores) if tp_scores else None,
        "mean_score_wrong": sum(fp_scores) / len(fp_scores) if fp_scores else None,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nmean confidence -- correct spans "
          f"{payload['mean_score_correct']:.3f}, wrong spans "
          f"{payload['mean_score_wrong']:.3f}"
          if fp_scores else "\nno wrong spans to compare")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
