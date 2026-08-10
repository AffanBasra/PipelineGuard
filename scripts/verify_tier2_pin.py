"""Is this deployment's Tier 2 checkpoint actually pinned, and is the pin enforced?

§20 records why this matters. A HuggingFace model repo is a git repo, and
`gliner-community/gliner_medium-v2.5` gained fp16/bf16 variants on 2026-04-28
with no name change. The threshold this project ships (0.55) is an uncalibrated
sigmoid cutoff swept against one exact set of weights, and `_TUNED_FOR` compares
the model NAME -- so weights can move underneath it silently.

A pin nobody checks is a comment. This asserts three things:

  1. config points at the checkpoint the findings were measured against
  2. the hub actually serves that revision
  3. a WRONG revision is refused rather than quietly falling back to `main`

(3) is the one that matters. A pin that is not enforced is worse than no pin,
because it is believed.

Usage:
    venv\\Scripts\\python.exe scripts\\verify_tier2_pin.py           # checks 1-3
    venv\\Scripts\\python.exe scripts\\verify_tier2_pin.py --load    # also loads the model

Needs the tier2 extra (`pip install ".[tier2]"`). The app venv does not have it;
`venv` does.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipelineguard.config import settings  # noqa: E402
from pipelineguard.detectors.tier2_encoder import (  # noqa: E402
    _TUNED_FOR,
    _TUNED_FOR_REVISION,
    Tier2Detector,
)

SAMPLE = "Rent for Ayesha Malik, Ghar 61C, Adamjee Road, Rawalpindi"


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--load", action="store_true",
                    help="also load the model and run one detection "
                         "(downloads ~1.7 GB on a cold cache)")
    args = ap.parse_args(argv)

    print(f"model    : {settings.tier2_model}")
    print(f"revision : {settings.tier2_model_revision or '(unset)'}")
    print(f"threshold: {settings.tier2_threshold}\n")

    results = []

    results.append(check(
        "config points at the measured checkpoint",
        settings.tier2_model == _TUNED_FOR
        and settings.tier2_model_revision == _TUNED_FOR_REVISION,
        f"expected {_TUNED_FOR}@{_TUNED_FOR_REVISION[:12]}",
    ))

    detector = Tier2Detector(settings.tier2_model,
                             revision=settings.tier2_model_revision)
    results.append(check(
        "the revision survives resolution (not silently unpinned)",
        detector.resolved_revision() == _TUNED_FOR_REVISION,
        f"resolved to {detector.resolved_revision() or 'main (UNPINNED)'}",
    ))

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("\n  huggingface_hub not importable -- run this with the tier2 "
              "environment:\n    venv\\Scripts\\python.exe scripts\\verify_tier2_pin.py")
        return 1

    api = HfApi()
    try:
        info = api.model_info(settings.tier2_model,
                              revision=settings.tier2_model_revision)
        results.append(check("the hub serves this revision", True,
                             f"sha {info.sha[:12]}"))
    except Exception as exc:  # noqa: BLE001 -- any hub failure is a failed check
        results.append(check("the hub serves this revision", False,
                             f"{type(exc).__name__}"))

    # The check the others exist for. If a bad revision loads anyway, the pin is
    # decorative and every threshold claim in the findings doc rests on nothing.
    try:
        api.model_info(settings.tier2_model, revision="0" * 40)
        results.append(check("a WRONG revision is refused", False,
                             "the hub accepted a nonexistent commit"))
    except Exception as exc:  # noqa: BLE001
        results.append(check("a WRONG revision is refused", True,
                             type(exc).__name__))

    if args.load:
        print()
        try:
            live = Tier2Detector(settings.tier2_model,
                                 threshold=settings.tier2_threshold,
                                 device="cpu", batch_size=2,
                                 revision=settings.tier2_model_revision)
            live.load()
            found = [(f.entity_type, SAMPLE[f.span_start:f.span_end])
                     for f in live.detect(SAMPLE, "memo")]
            results.append(check("the pinned model loads and detects", bool(found)))
            for entity_type, text in found:
                print(f"        {entity_type:<12} {text!r}")
        except Exception as exc:  # noqa: BLE001
            results.append(check("the pinned model loads and detects", False,
                                 f"{type(exc).__name__}: {str(exc)[:80]}"))

    ok = all(results)
    print(f"\n{'all checks passed' if ok else 'CHECKS FAILED'} "
          f"({sum(results)}/{len(results)})")
    if not ok:
        print("A threshold is only valid for the weights it was swept against "
              "(findings §6.1, §20.1).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
