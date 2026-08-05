"""Model selection for Tier 2: is the choice in probe_ner_locale.py stable?

That probe scored three models at a single hardcoded threshold (0.4) and
concluded `nvidia/gliner-PII` was best. Three things were never checked, and
each of them can overturn that:

  1. the ADDRESS 2x2 was only ever transcribed for nvidia, so "the finding
     rests on one model" was an assumption, not a measurement
  2. 0.4 was never varied, so a threshold-calibration artifact and a real
     model difference are indistinguishable
  3. latency was never measured at all -- and Tier 2's cost per record is the
     number the whole tiering argument rests on

This sweeps threshold, measures per-call and batched latency, and reports the
parameter split and tokenizer fragmentation of each backbone.

Predictions are re-run per threshold rather than filtered from one
low-threshold call. GLiNER's greedy span decoding is threshold-dependent: a
span scoring above 0.25 can suppress an overlapping one that would have
survived at 0.55, so filtering a single call would not be equivalent and would
quietly overstate the high-threshold numbers.

Cases, gold spans and the coverage metric are imported from probe_ner_locale
rather than redefined, so the two probes cannot drift apart. Run that script
first if you want the committed per-model tables; this one answers whether
those tables' conclusion survives.

Usage:
    python scripts/probe_ner_sweep.py [--out results.json]

Needs `torch`, `transformers`, `gliner`. Set HF_HUB_DISABLE_XET=1 or the
download hangs.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_ner_locale import build_cases, char_coverage  # noqa: E402

MODELS = ["urchade/gliner_multi_pii-v1", "nvidia/gliner-PII"]

# 0.4 is what probe_ner_locale.py hardcodes; 0.25 and 0.55 bracket it. NVIDIA
# evaluate their own model at 0.3, which is inside this range and below the
# value the original probe scored them at -- itself worth knowing.
THRESHOLDS = (0.25, 0.40, 0.55)

LABELS = {
    "PERSON": ["person", "first_name", "last_name"],
    "ADDRESS": ["address", "street_address", "location"],
}

# 60 chars is this pipeline's memo field. 601 and 969 are the Nemotron median
# unstructured and structured document lengths (docs/handoff.md S6), used so
# the latency curve covers the corpus the evaluation plan targets and not just
# the short field.
_FILLER = (
    "Customer resides at House 12, Street 4, F-8/3 Islamabad and the "
    "account was reviewed by Ayesha Malik on the fourteenth. "
)


def latency_inputs() -> dict[str, str]:
    short = "Transfer to Ayesha Malik, House 12, Street 4, F-8/3 Islamabad"
    return {
        "memo_60": short,
        "nemotron_unstructured_601": (_FILLER * 12)[:601],
        "nemotron_structured_969": (_FILLER * 20)[:969],
    }


def param_report(model) -> dict:
    """Total / embedding / compute-bearing parameter counts.

    The split matters more than the total. A vocabulary embedding is a lookup
    table, not a matmul -- it inflates file size and total parameter count
    without costing anything per token. Comparing a multilingual backbone
    (250k vocab) against an English-only one (128k) on total parameters alone
    therefore understates the real compute gap by a wide margin.
    """
    total = sum(p.numel() for p in model.parameters())
    emb = sum(
        p.numel()
        for name, p in model.named_parameters()
        if "word_embeddings" in name or "embed_tokens" in name
    )
    return {"total": total, "word_embeddings": emb, "non_embedding": total - emb}


def _tokenizer_of(model):
    for path in ("token_rep_layer.tokenizer",
                 "data_processor.transformer_tokenizer"):
        obj = model
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    return None


def tokenizer_fragmentation(model, texts_by_style) -> dict | None:
    """Tokens per whitespace-word, by sentence style.

    Tests the obvious mechanism for a Roman-Urdu penalty: if a backbone's
    vocabulary has never seen these words, it shatters them into subwords and
    the span head has more pieces to reassemble. Measured rather than assumed,
    because it is equally plausible a priori that the multilingual vocabulary
    handles them fine.
    """
    tok = _tokenizer_of(model)
    if tok is None:
        return None
    out = {}
    for style, sents in texts_by_style.items():
        n_tok = sum(len(tok.tokenize(s)) for s in sents)
        n_word = sum(len(s.split()) for s in sents)
        out[style] = {"tokens": n_tok, "words": n_word,
                      "tokens_per_word": n_tok / n_word}
    return out


def address_form_fragmentation(model) -> dict | None:
    """Fragmentation of the address FORMS specifically.

    Separate from the above because the sentence-level measurement is diluted:
    it counts the template frame and the names, when the variable actually
    under test is House/Street/Flat vs Ghar/Gali/Makan. Averaging those away
    would answer a question nobody asked.
    """
    tok = _tokenizer_of(model)
    if tok is None:
        return None
    from probe_ner_locale import ADDRESSES

    out = {}
    for form in ("english_form", "urdu_form"):
        addrs = [a for f, a in ADDRESSES if f == form]
        out[form] = {"n": len(addrs),
                     "tokens": sum(len(tok.tokenize(a)) for a in addrs)}
    out["structural_nouns"] = {
        w: tok.tokenize(w)
        for w in ("House", "Street", "Flat", "Ghar", "Gali", "Makan")
    }
    return out


def sweep_accuracy(model, cases, threshold) -> dict:
    agg = defaultdict(lambda: {"n": 0, "exact": 0, "hit": 0, "cov": 0.0,
                               "n_pred": 0})
    for etype, style, band, _value, text, gs, ge in cases:
        preds = [
            (e["start"], e["end"])
            for e in model.predict_entities(text, LABELS[etype],
                                            threshold=threshold)
        ]
        hits = [p for p in preds if gs < p[1] and p[0] < ge]
        cov = char_coverage(gs, ge, [(a, b, "") for a, b in hits])
        for key in ((etype, style, "ALL"), (etype, style, band),
                    (etype, "ALL", band), (etype, "ALL", "ALL")):
            d = agg[key]
            d["n"] += 1
            d["cov"] += cov
            d["n_pred"] += len(preds)
            d["exact"] += any(a == gs and b == ge for a, b in hits)
            d["hit"] += bool(hits)
    return {
        "|".join(k): {
            "n": v["n"],
            "exact": v["exact"] / v["n"],
            "hit": v["hit"] / v["n"],
            "cov": v["cov"] / v["n"],
            "preds_per_case": v["n_pred"] / v["n"],
        }
        for k, v in agg.items()
    }


def measure_latency(model, text, warmup=3, n=20) -> dict:
    """p50/p95 of n timed calls after `warmup` discarded ones.

    The warm-up is not optional: the first call through a torch graph pays
    lazy initialisation that has nothing to do with steady-state cost, and
    including it inflates p50 on a 20-sample window by enough to change the
    conclusion.
    """
    for _ in range(warmup):
        model.predict_entities(text, LABELS["ADDRESS"], threshold=0.4)
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        model.predict_entities(text, LABELS["ADDRESS"], threshold=0.4)
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return {
        "chars": len(text), "n": n,
        "p50": statistics.median(samples),
        "p95": samples[int(0.95 * n) - 1],
        "mean": statistics.mean(samples),
        "min": samples[0], "max": samples[-1],
    }


def measure_batched(model, text, sizes=(1, 8, 32)) -> dict:
    """Per-record cost at several batch sizes.

    Caveat that has to travel with these numbers: the batch is one string
    repeated. GLiNER pads to the longest sequence in a batch, so a real batch
    of mixed-length memos costs more per record than this measures. This is a
    best case, not an estimate.
    """
    out = {}
    if not hasattr(model, "batch_predict_entities"):
        return out
    for bs in sizes:
        batch = [text] * bs
        for _ in range(2):
            model.batch_predict_entities(batch, LABELS["ADDRESS"], threshold=0.4)
        samples = []
        for _ in range(8):
            t = time.perf_counter()
            model.batch_predict_entities(batch, LABELS["ADDRESS"], threshold=0.4)
            samples.append((time.perf_counter() - t) * 1000)
        total = statistics.median(samples)
        out[f"batch_{bs}"] = {"batch_size": bs, "batch_p50_ms": total,
                              "per_record_ms": total / bs}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="sweep_results.json")
    ap.add_argument("--models", nargs="*", default=MODELS)
    args = ap.parse_args(argv)

    from gliner import GLiNER

    cases = build_cases()
    print(f"{len(cases)} cases x {len(THRESHOLDS)} thresholds x "
          f"{len(args.models)} models\n", flush=True)

    by_style = defaultdict(list)
    for etype, style, _band, _v, text, _s, _e in cases:
        if etype == "PERSON":
            by_style[style].append(text)

    results = {}
    for model_id in args.models:
        print("=" * 70, f"\n{model_id}\n", "=" * 70, flush=True)
        t0 = time.perf_counter()
        model = GLiNER.from_pretrained(model_id)
        model.eval()
        load_s = time.perf_counter() - t0
        print(f"  loaded in {load_s:.1f}s", flush=True)

        entry = {
            "load_seconds": load_s,
            "params": param_report(model),
            "tokenizer": tokenizer_fragmentation(model, by_style),
            "address_forms": address_form_fragmentation(model),
            "cells": {},
            "latency": {},
        }
        p = entry["params"]
        print(f"  params: total={p['total']:,} emb={p['word_embeddings']:,} "
              f"compute-bearing={p['non_embedding']:,}", flush=True)

        for thr in THRESHOLDS:
            t_start = time.perf_counter()
            entry["cells"][str(thr)] = sweep_accuracy(model, cases, thr)
            elapsed = time.perf_counter() - t_start
            a = entry["cells"][str(thr)]
            print(f"  thr={thr}: PERSON cov={a['PERSON|ALL|ALL']['cov']:.0%}  "
                  f"ADDRESS cov={a['ADDRESS|ALL|ALL']['cov']:.0%}  "
                  f"({elapsed:.0f}s, {elapsed / len(cases) * 1000:.0f} ms/case)",
                  flush=True)

        for name, text in latency_inputs().items():
            m = measure_latency(model, text)
            entry["latency"][name] = m
            print(f"  latency {name:<28} p50={m['p50']:7.1f} ms  "
                  f"p95={m['p95']:7.1f} ms", flush=True)

        entry["latency"].update(measure_batched(model, latency_inputs()["memo_60"]))
        for k, v in entry["latency"].items():
            if k.startswith("batch_"):
                print(f"  batch {v['batch_size']:>3}: {v['batch_p50_ms']:8.1f} ms "
                      f"total  {v['per_record_ms']:7.2f} ms/record", flush=True)

        results[model_id] = entry
        del model
        print(flush=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
