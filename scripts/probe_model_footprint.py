"""Can a smaller checkpoint fit a 1 GB host without losing coverage?

Streamlit Community Cloud caps an app at roughly 1 GB of RAM. The shipped
checkpoint, `gliner-community/gliner_medium-v2.5`, was chosen on a machine with
no such ceiling (findings section 11), so the question is whether
`gliner_small-v2.5` buys enough headroom to be worth what it costs in coverage.

Three things are measured, and one of them needs a warning attached:

  memory     Resident set size (psutil), because torch allocates outside
             Python's allocator and `tracemalloc` cannot see it. tracemalloc
             is reported anyway since it was asked for, labelled as the
             Python-only figure it is -- expect it to under-report by ~10x.

  latency    Median ms per record over the shipped batched path, on CPU.
             CPU because the target host has no GPU; a CUDA number here would
             be measuring the wrong machine.

  coverage   The 237 locale cases from probe_ner_locale.py, scored with its
             `char_coverage`. Reusing that case set is the point: a new test
             set would produce numbers that cannot be compared with any
             committed figure.

**The threshold does not transfer between checkpoints.** Section 6 of
probe_ner_sweep.py measured `nvidia/gliner-PII` firing on 68% of clean
Pakistani text at this model's operating point. Scoring `small` at the
`medium` threshold therefore measures the wrong thing, so `--thresholds`
sweeps several and the table reports each. A single-threshold run is a
smoke test, not a decision.

Each model is measured in its own subprocess. Loading both into one process
would charge the second one for the first one's allocator arenas.

Usage:
    python scripts/probe_model_footprint.py
    python scripts/probe_model_footprint.py --thresholds 0.25,0.35,0.45,0.55
    python scripts/probe_model_footprint.py --records 50 --repeats 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

# (label, model id, revision). Revisions pinned for the same reason the shipped
# one is: these repos gained fp16/bf16 variants without changing their names.
MODELS = [
    ("gliner_medium-v2.5", "gliner-community/gliner_medium-v2.5",
     "88c3b98b57ad5e7d66fb209ed61c53f4b1fd05da"),
    ("gliner_small-v2.5", "gliner-community/gliner_small-v2.5",
     "f227d3cd637bd4e6757ae143935316d062393341"),
]

MB = 1024 * 1024


# --------------------------------------------------------------------------- #
# Child process: measure one model and print one JSON object
# --------------------------------------------------------------------------- #
class PeakRSS:
    """Sample RSS on a thread, because the peak is not observable afterwards.

    A forward pass allocates and frees inside one call, so reading RSS before
    and after would report the trough and miss the spike this whole script
    exists to find.
    """

    def __init__(self, interval: float = 0.005) -> None:
        import psutil

        self._proc = psutil.Process()
        self._interval = interval
        self._stop = threading.Event()
        self.peak = 0

    def __enter__(self) -> PeakRSS:
        self.peak = self._proc.memory_info().rss

        def sample() -> None:
            while not self._stop.wait(self._interval):
                self.peak = max(self.peak, self._proc.memory_info().rss)

        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self.peak = max(self.peak, self._proc.memory_info().rss)


def _coverage_by_type(detector, cases, field: str = "memo") -> dict:
    """Character coverage per entity type over probe_ner_locale's case set."""
    from probe_ner_locale import char_coverage

    # Detector labels are PERSON_NAME / ADDRESS; the locale probe says PERSON.
    canonical = {"PERSON": "PERSON_NAME", "ADDRESS": "ADDRESS"}
    inputs = {i: {field: c[4]} for i, c in enumerate(cases)}
    found = detector.detect_batch(inputs)

    scores: dict[str, list[float]] = {}
    hits: dict[str, list[bool]] = {}
    for i, case in enumerate(cases):
        entity_type, _style, _band, _value, _text, gold_s, gold_e = case
        wanted = canonical[entity_type]
        preds = [(f.span_start, f.span_end, f.entity_type)
                 for f in found.get(i, {}).get(field, [])
                 if f.entity_type == wanted]
        cov = char_coverage(gold_s, gold_e, preds)
        scores.setdefault(entity_type, []).append(cov)
        hits.setdefault(entity_type, []).append(cov > 0.0)

    return {
        etype: {
            "coverage": 100.0 * statistics.fmean(vals),
            "any_hit": 100.0 * statistics.fmean(hits[etype]),
            "complete": 100.0 * statistics.fmean([v >= 1.0 for v in vals]),
            "cases": len(vals),
        }
        for etype, vals in scores.items()
    }


def measure(model_id: str, revision: str, thresholds: list[float],
            records: int, repeats: int, batch_size: int) -> dict:
    import gc
    import tracemalloc

    import psutil

    from probe_ner_locale import build_cases

    proc = psutil.Process()
    # torch and gliner are imported HERE, before the baseline, on purpose. They
    # are imported lazily inside `load()`, so a baseline taken before them
    # charges the interpreter's torch import -- CUDA runtime libraries included
    # -- to the model weights, which is how the first run of this script
    # reported a 44M-parameter checkpoint as 1.8 GB.
    import gliner  # noqa: F401
    import torch

    from pipelineguard.detectors.tier2_encoder import Tier2Detector
    from pipelineguard.generator.transactions import make_memo

    gc.collect()
    baseline = proc.memory_info().rss

    tracemalloc.start()
    detector = Tier2Detector(model_id, threshold=thresholds[0], device="cpu",
                             batch_size=batch_size, revision=revision)
    with PeakRSS() as load_watcher:
        detector.load()
    py_current, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # `load()` ends with a warm-up batch, so the reading straight after it is a
    # high-water mark, not a resting one. Settle first: Windows trims the
    # working set on its own schedule, which is what made peak read lower than
    # resting on the first run.
    gc.collect()
    time.sleep(1.0)
    gc.collect()
    resting = proc.memory_info().rss

    # A memo with no free text measures nothing, so the blank rate is off here.
    memos = [make_memo(blank_rate=0.0) for _ in range(records)]
    inputs = {i: {"memo": text} for i, text in enumerate(memos)}

    detector.detect_batch(inputs)          # warm, discarded
    timings = []
    with PeakRSS() as watcher:
        for _ in range(repeats):
            start = time.perf_counter()
            detector.detect_batch(inputs)
            timings.append((time.perf_counter() - start) / records * 1000.0)
    peak = watcher.peak

    cases = build_cases()
    accuracy = {}
    for threshold in thresholds:
        detector.threshold = threshold
        accuracy[f"{threshold:.2f}"] = _coverage_by_type(detector, cases)

    return {
        "model": model_id,
        "revision": revision,
        "torch": torch.__version__,
        "baseline_mb": baseline / MB,
        "resting_mb": resting / MB,
        "load_peak_mb": load_watcher.peak / MB,
        "peak_mb": max(peak, resting) / MB,
        "model_mb": (resting - baseline) / MB,
        "inference_headroom_mb": (peak - resting) / MB,
        "tracemalloc_peak_mb": py_peak / MB,
        "latency_ms": statistics.median(timings),
        "latency_all_ms": timings,
        "records": records,
        "accuracy": accuracy,
    }


# --------------------------------------------------------------------------- #
# Parent process: run the children, print the comparison
# --------------------------------------------------------------------------- #
def run_child(model_id: str, revision: str, args) -> dict | None:
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--measure", model_id, "--revision", revision,
           "--thresholds", args.thresholds,
           "--records", str(args.records),
           "--repeats", str(args.repeats),
           "--batch-size", str(args.batch_size)]
    print(f"  measuring {model_id} ...", flush=True)
    done = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    for line in done.stdout.splitlines():
        if line.startswith("{"):
            return json.loads(line)
    print(f"  FAILED ({done.returncode})")
    print("  " + "\n  ".join((done.stderr or "no stderr").splitlines()[-12:]))
    return None


def _fmt(value: float, width: int = 8, places: int = 1) -> str:
    return f"{value:>{width},.{places}f}"


def report(results: list[dict], thresholds: list[float], limit_mb: float) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        Console = None

    shipped = thresholds[0]
    rows = []
    for r in results:
        acc = r["accuracy"][f"{shipped:.2f}"]
        rows.append({
            "model": r["model"].split("/")[-1],
            "baseline": r["baseline_mb"],
            "resting": r["resting_mb"],
            "peak": r["peak_mb"],
            "weights": r["model_mb"],
            "spike": r["inference_headroom_mb"],
            "latency": r["latency_ms"],
            "person": acc.get("PERSON", {}).get("coverage", float("nan")),
            "address": acc.get("ADDRESS", {}).get("coverage", float("nan")),
            "headroom": limit_mb - r["peak_mb"],
        })

    if Console is None:
        print(f"\n{'model':<22}{'rest MB':>10}{'peak MB':>10}{'ms/rec':>9}"
              f"{'PERSON%':>10}{'ADDRESS%':>10}{'headroom':>10}")
        for row in rows:
            print(f"{row['model']:<22}{_fmt(row['resting'], 10)}"
                  f"{_fmt(row['peak'], 10)}{_fmt(row['latency'], 9, 1)}"
                  f"{_fmt(row['person'], 10)}{_fmt(row['address'], 10)}"
                  f"{_fmt(row['headroom'], 10)}")
        return

    console = Console()
    table = Table(title=f"CPU footprint at threshold {shipped:.2f} "
                        f"(host limit {limit_mb:,.0f} MB)",
                  header_style="bold")
    table.add_column("model")
    table.add_column("torch alone MB", justify="right")
    table.add_column("+weights MB", justify="right")
    table.add_column("resting MB", justify="right")
    table.add_column("peak MB", justify="right")
    table.add_column("ms/record", justify="right")
    table.add_column("PERSON cov %", justify="right")
    table.add_column("ADDRESS cov %", justify="right")
    table.add_column("headroom MB", justify="right")
    for row in rows:
        fits = row["headroom"] > 0
        table.add_row(
            row["model"],
            f"{row['baseline']:,.0f}", f"{row['weights']:,.0f}",
            f"{row['resting']:,.0f}", f"{row['peak']:,.0f}",
            f"{row['latency']:,.1f}",
            f"{row['person']:.1f}", f"{row['address']:.1f}",
            f"[green]{row['headroom']:,.0f}[/green]" if fits
            else f"[red]{row['headroom']:,.0f}[/red]",
        )
    console.print(table)
    console.print(
        f"[dim]torch {results[0].get('torch', '?')} — "
        "'torch alone' is the interpreter with torch and gliner imported and no "
        "weights loaded. On a CUDA build it carries runtime libraries the "
        "deployment target does not install.[/dim]")

    if len(thresholds) > 1:
        sweep = Table(title="Coverage by threshold -- the operating point does "
                            "not transfer between checkpoints",
                      header_style="bold")
        sweep.add_column("model")
        sweep.add_column("threshold", justify="right")
        sweep.add_column("PERSON cov %", justify="right")
        sweep.add_column("PERSON complete %", justify="right")
        sweep.add_column("ADDRESS cov %", justify="right")
        sweep.add_column("ADDRESS complete %", justify="right")
        for r in results:
            for threshold in thresholds:
                acc = r["accuracy"][f"{threshold:.2f}"]
                person = acc.get("PERSON", {})
                address = acc.get("ADDRESS", {})
                sweep.add_row(
                    r["model"].split("/")[-1], f"{threshold:.2f}",
                    f"{person.get('coverage', 0):.1f}",
                    f"{person.get('complete', 0):.1f}",
                    f"{address.get('coverage', 0):.1f}",
                    f"{address.get('complete', 0):.1f}",
                )
        console.print(sweep)

    console.print(
        "\n[bold]coverage[/bold] is the fraction of each gold span's characters "
        "the detector claimed; [bold]complete[/bold] is the share of cases "
        "where that reached 100%. A partial hit is a redaction failure, so "
        "complete is the operational number.")
    console.print(
        "[bold]tracemalloc[/bold] peaks: "
        + ", ".join(f"{r['model'].split('/')[-1]} {r['tracemalloc_peak_mb']:,.0f} MB"
                    for r in results)
        + " -- Python allocations only, which is why they are far below RSS.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measure", help=argparse.SUPPRESS)
    parser.add_argument("--revision", help=argparse.SUPPRESS)
    parser.add_argument("--thresholds", default="0.55",
                        help="comma-separated, first one is the headline")
    parser.add_argument("--records", type=int, default=50,
                        help="memos per inference batch")
    parser.add_argument("--repeats", type=int, default=3,
                        help="timed passes; the median is reported")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--limit-mb", type=float, default=1024.0,
                        help="host memory ceiling, for the headroom column")
    parser.add_argument("--out", type=Path, help="also write the raw JSON here")
    args = parser.parse_args(argv)

    thresholds = [float(t) for t in args.thresholds.split(",")]

    if args.measure:
        print(json.dumps(measure(args.measure, args.revision, thresholds,
                                 args.records, args.repeats, args.batch_size)))
        return 0

    print(f"{len(MODELS)} models, {args.records} records per pass, "
          f"{args.repeats} timed passes, CPU\n")
    results = [r for _label, model_id, revision in MODELS
               if (r := run_child(model_id, revision, args))]
    if not results:
        print("nothing measured")
        return 1

    report(results, thresholds, args.limit_mb)
    if args.out:
        args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nraw results -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
