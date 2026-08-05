"""Can Tier 2 be made cheap enough to be worth tiering to?

probe_ner_sweep.py measured `urchade/gliner_multi_pii-v1` at 44.6 ms per record
batched, against a pipeline that costs 0.69 ms per record end to end. That is
65x the entire budget, and it means tiering only pays below ~1.55% escalation
while roughly half of all memos genuinely contain a name. No escalation
predicate can close a gap that size -- the cost has to come out of the model.

This measures the two standard ways to do that on CPU:

  torch int8   PyTorch dynamic quantization (FBGEMM kernels). No export step.
  onnx         ONNX Runtime graph, fp32 and int8.

Speed alone is not the result. gliner/model.py:562 warns that "stock
DeBERTa-based models lose accuracy with int8" absent quantization-aware
training, and gliner_multi_pii-v1 is exactly such a model. A 3x speedup that
quietly drops 20 points of address coverage is not a win for a redaction
firewall, so every configuration is scored on the same 237 cases as the other
probes and reported as a coverage delta against fp32.

Accuracy is measured at threshold 0.25, not the 0.4 the original probe used:
probe_ner_sweep.py section 6.1 found 0.25 is where this model actually
operates best, and a runtime change should be judged at the operating point
it would ship at.

Usage:
    python scripts/probe_ner_runtime.py [--out runtime_results.json]

Needs `torch`, `gliner`, `onnx`, `onnxruntime`. Writes ONNX graphs to
models/onnx/ (gitignored). The fp32 graph is ~1.1 GB.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_ner_locale import build_cases, char_coverage  # noqa: E402

MODEL_ID = "urchade/gliner_multi_pii-v1"
THRESHOLD = 0.25
LABELS = {
    "PERSON": ["person", "first_name", "last_name"],
    "ADDRESS": ["address", "street_address", "location"],
}
MEMO = "Transfer to Ayesha Malik, House 12, Street 4, F-8/3 Islamabad"

REPO = Path(__file__).resolve().parent.parent
ONNX_DIR = REPO / "models" / "onnx"


def score(model, cases) -> dict:
    """Coverage by entity type at THRESHOLD, plus predictions per case.

    preds_per_case is carried because it is the only precision-adjacent signal
    available here: if a quantized model keeps its coverage by emitting far
    more spans, that is not the same model even though the coverage column
    says it is.
    """
    agg = {}
    for etype, _style, _band, _v, text, gs, ge in cases:
        preds = [
            (e["start"], e["end"])
            for e in model.predict_entities(text, LABELS[etype], threshold=THRESHOLD)
        ]
        hits = [p for p in preds if gs < p[1] and p[0] < ge]
        d = agg.setdefault(etype, {"n": 0, "cov": 0.0, "hit": 0, "n_pred": 0})
        d["n"] += 1
        d["cov"] += char_coverage(gs, ge, [(a, b, "") for a, b in hits])
        d["hit"] += bool(hits)
        d["n_pred"] += len(preds)
    return {
        k: {"n": v["n"], "cov": v["cov"] / v["n"], "hit": v["hit"] / v["n"],
            "preds_per_case": v["n_pred"] / v["n"]}
        for k, v in agg.items()
    }


def time_single(model, text, warmup=3, n=20) -> dict:
    for _ in range(warmup):
        model.predict_entities(text, LABELS["ADDRESS"], threshold=THRESHOLD)
    s = []
    for _ in range(n):
        t = time.perf_counter()
        model.predict_entities(text, LABELS["ADDRESS"], threshold=THRESHOLD)
        s.append((time.perf_counter() - t) * 1000)
    s.sort()
    return {"p50": statistics.median(s), "p95": s[int(0.95 * n) - 1], "min": s[0]}


def time_batched(model, text, sizes=(1, 8, 32), n=8) -> dict:
    """Per-record cost at each batch size.

    probe_ner_sweep.py found batching saturates at 8, so 32 is carried only to
    confirm that still holds once the runtime changes -- an ONNX graph with
    different memory behaviour could move the saturation point.
    """
    out = {}
    if not hasattr(model, "batch_predict_entities"):
        return out
    for bs in sizes:
        batch = [text] * bs
        for _ in range(2):
            model.batch_predict_entities(batch, LABELS["ADDRESS"], threshold=THRESHOLD)
        s = []
        for _ in range(n):
            t = time.perf_counter()
            model.batch_predict_entities(batch, LABELS["ADDRESS"], threshold=THRESHOLD)
            s.append((time.perf_counter() - t) * 1000)
        total = statistics.median(s)
        out[f"batch_{bs}"] = {"batch_p50_ms": total, "per_record_ms": total / bs}
    return out


def measure(model, cases, label) -> dict:
    print(f"  [{label}] scoring {len(cases)} cases...", flush=True)
    t0 = time.perf_counter()
    acc = score(model, cases)
    print(f"  [{label}] scored in {time.perf_counter() - t0:.0f}s", flush=True)
    single = time_single(model, MEMO)
    batched = time_batched(model, MEMO)
    per_rec = batched.get("batch_8", {}).get("per_record_ms")
    line = (f"  [{label}] PERSON cov={acc['PERSON']['cov']:.1%}  "
            f"ADDRESS cov={acc['ADDRESS']['cov']:.1%}  "
            f"single p50={single['p50']:.1f} ms")
    if per_rec:
        line += f"  batch8={per_rec:.1f} ms/rec"
    print(line, flush=True)
    return {"accuracy": acc, "single": single, "batched": batched}


# Quantization crushes the score distribution toward zero without reordering
# it: fp32 scores 'Ayesha Malik' at 0.999, int8 at 0.044, while both still rank
# it above 'Transfer' and 'to'. So int8 measured at the fp32 threshold looks
# like total failure (0% coverage) when what has actually broken is
# calibration, not discrimination. These are the thresholds that recover it.
RECAL_THRESHOLDS = (0.002, 0.005, 0.01, 0.02, 0.05)


def recalibrate(model, cases, label) -> dict:
    """Coverage across the low thresholds a quantized model actually needs.

    Without this the int8 rows are reported as unusable, which is wrong and
    would have thrown away a 2x speedup for free.
    """
    out = {}
    print(f"  [{label}] recalibrating threshold", flush=True)
    print(f"  {'thr':>7} {'PERSON':>8} {'ADDRESS':>8} {'preds/case':>11}", flush=True)
    for thr in RECAL_THRESHOLDS:
        agg = {}
        for etype, _st, _b, _v, text, gs, ge in cases:
            preds = [
                (e["start"], e["end"])
                for e in model.predict_entities(text, LABELS[etype], threshold=thr)
            ]
            hits = [p for p in preds if gs < p[1] and p[0] < ge]
            d = agg.setdefault(etype, {"n": 0, "cov": 0.0, "n_pred": 0})
            d["n"] += 1
            d["cov"] += char_coverage(gs, ge, [(a, b, "") for a, b in hits])
            d["n_pred"] += len(preds)
        row = {k: {"cov": v["cov"] / v["n"],
                   "preds_per_case": v["n_pred"] / v["n"]}
               for k, v in agg.items()}
        total_preds = sum(v["preds_per_case"] for v in row.values())
        out[str(thr)] = row
        print(f"  {thr:>7} {row['PERSON']['cov']:>7.1%} "
              f"{row['ADDRESS']['cov']:>7.1%} {total_preds:>11.2f}", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="runtime_results.json")
    ap.add_argument("--skip-onnx", action="store_true")
    args = ap.parse_args(argv)

    from gliner import GLiNER

    cases = build_cases()
    results = {}

    # ---- fp32 torch: the baseline every other row is a delta against --------
    print("=" * 70, "\ntorch fp32 (baseline)\n", "=" * 70, flush=True)
    model = GLiNER.from_pretrained(MODEL_ID)
    model.eval()
    results["torch_fp32"] = measure(model, cases, "torch_fp32")

    # ---- torch dynamic int8 -------------------------------------------------
    print("=" * 70, "\ntorch int8 (dynamic quantization)\n", "=" * 70, flush=True)
    try:
        qmodel = GLiNER.from_pretrained(MODEL_ID)
        qmodel.eval()
        qmodel.quantize(dtype="int8")
        results["torch_int8"] = measure(qmodel, cases, "torch_int8")
        results["torch_int8"]["recalibrated"] = recalibrate(qmodel, cases, "torch_int8")
        del qmodel
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}", flush=True)
        results["torch_int8"] = {"error": f"{type(exc).__name__}: {exc}",
                                 "traceback": traceback.format_exc()[-2000:]}

    # ---- ONNX export, fp32 and int8 ----------------------------------------
    if not args.skip_onnx:
        print("=" * 70, "\nONNX export\n", "=" * 70, flush=True)
        try:
            ONNX_DIR.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            paths = model.export_to_onnx(ONNX_DIR, quantize=True)
            print(f"  exported in {time.perf_counter() - t0:.0f}s: {paths}", flush=True)
            results["_onnx_export"] = {k: str(v) for k, v in paths.items()}

            for key, fname in (("onnx_fp32", "model.onnx"),
                               ("onnx_int8", "model_quantized.onnx")):
                fpath = ONNX_DIR / fname
                if not fpath.exists():
                    print(f"  {key}: {fname} not produced, skipping", flush=True)
                    continue
                size_mb = fpath.stat().st_size / 1e6
                print(f"\n  loading {key} ({size_mb:.0f} MB)", flush=True)
                try:
                    om = GLiNER.from_pretrained(
                        str(ONNX_DIR), load_onnx_model=True, onnx_model_file=fname
                    )
                    r = measure(om, cases, key)
                    r["file_mb"] = size_mb
                    if key.endswith("int8"):
                        r["recalibrated"] = recalibrate(om, cases, key)
                    results[key] = r
                    del om
                except Exception as exc:
                    print(f"  {key} FAILED: {type(exc).__name__}: {exc}", flush=True)
                    results[key] = {"error": f"{type(exc).__name__}: {exc}",
                                    "traceback": traceback.format_exc()[-2000:]}
        except Exception as exc:
            print(f"  EXPORT FAILED: {type(exc).__name__}: {exc}", flush=True)
            results["_onnx_export"] = {"error": f"{type(exc).__name__}: {exc}",
                                       "traceback": traceback.format_exc()[-2000:]}

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nwrote {args.out}")

    # ---- summary ------------------------------------------------------------
    base = results.get("torch_fp32", {})
    b_rec = base.get("batched", {}).get("batch_8", {}).get("per_record_ms")
    print("\n" + "=" * 78)
    print(f"{'config':<14} {'batch8 ms/rec':>14} {'speedup':>9} "
          f"{'ADDRESS cov':>12} {'PERSON cov':>11}")
    print("-" * 78)
    for key in ("torch_fp32", "torch_int8", "onnx_fp32", "onnx_int8"):
        r = results.get(key)
        if not r or "error" in r:
            print(f"{key:<14} {'FAILED':>14}")
            continue
        rec = r["batched"].get("batch_8", {}).get("per_record_ms")
        sp = f"{b_rec / rec:.2f}x" if rec and b_rec else "-"
        print(f"{key:<14} {rec:>14.1f} {sp:>9} "
              f"{r['accuracy']['ADDRESS']['cov']:>11.1%} "
              f"{r['accuracy']['PERSON']['cov']:>10.1%}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
