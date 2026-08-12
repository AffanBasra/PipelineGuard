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
    _TUNED_FOR_BASE,
    _TUNED_FOR_BASE_REVISION,
    _TUNED_FOR_REVISION,
    Tier2Detector,
    cached_base_revisions,
)

SAMPLE = "Rent for Ayesha Malik, Ghar 61C, Adamjee Road, Rawalpindi"


def check(label: str, ok: bool, detail: str = "", hint: str = "") -> bool:
    """`detail` prints either way; `hint` only on failure, so a passing line
    never carries advice for a state it is not in."""
    suffix = f" -- {detail}" if detail else ""
    if not ok and hint:
        suffix += f"\n         {hint}"
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
    return ok


def skip(label: str, why: str) -> None:
    print(f"  [SKIP] {label} -- {why}")


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
    # Offline is the PINNED state (§25), so the hub checks cannot run and must
    # not be reported as failures. They also cannot be faked: offline raises
    # OfflineModeIsEnabled for a good revision and a bad one alike, so the
    # "wrong revision is refused" check would pass for the wrong reason.
    offline = os.getenv("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true"}
    if offline:
        skip("the hub serves this revision", "HF_HUB_OFFLINE=1")
        skip("a WRONG revision is refused",
             "HF_HUB_OFFLINE=1 -- offline refuses every revision equally, "
             "so this proves nothing; re-run online to check enforcement")
    else:
        try:
            info = api.model_info(settings.tier2_model,
                                  revision=settings.tier2_model_revision)
            results.append(check("the hub serves this revision", True,
                                 f"sha {info.sha[:12]}"))
        except Exception as exc:  # noqa: BLE001 -- any hub failure fails the check
            results.append(check("the hub serves this revision", False,
                                 f"{type(exc).__name__}"))

        # The check the others exist for. If a bad revision loads anyway the pin
        # is decorative, and every threshold claim in the findings rests on it.
        try:
            api.model_info(settings.tier2_model, revision="0" * 40)
            results.append(check("a WRONG revision is refused", False,
                                 "the hub accepted a nonexistent commit"))
        except Exception as exc:  # noqa: BLE001
            results.append(check("a WRONG revision is refused", True,
                                 type(exc).__name__))

    # ---- the OTHER repo, which the revision above cannot reach (§25) --------
    print()
    results.append(check(
        "config declares the backbone commit",
        settings.tier2_base_model == _TUNED_FOR_BASE
        and settings.tier2_base_revision == _TUNED_FOR_BASE_REVISION,
        f"expected {_TUNED_FOR_BASE}@{_TUNED_FOR_BASE_REVISION[:12]}",
    ))

    if offline:
        skip("the hub serves the backbone commit", "HF_HUB_OFFLINE=1")
    else:
        try:
            info = api.model_info(settings.tier2_base_model,
                                  revision=settings.tier2_base_revision)
            results.append(check("the hub serves the backbone commit", True,
                                 f"sha {info.sha[:12]}"))
        except Exception as exc:  # noqa: BLE001
            results.append(check("the hub serves the backbone commit", False,
                                 f"{type(exc).__name__}"))

    # The backbone is pinned by the CACHE, not by an argument, so these two are
    # what say whether the pin is real on THIS machine.
    cached = cached_base_revisions(settings.tier2_base_model)
    results.append(check(
        "the cache holds exactly the pinned backbone commit",
        cached == [settings.tier2_base_revision],
        f"cached {[r[:12] for r in cached] or 'none'}",
        hint="run `python -m pipelineguard.prefetch`",
    ))
    results.append(check(
        "HF_HUB_OFFLINE is set, so the cache is what loads",
        offline,
        "set" if offline else "unset",
        hint="unset means GLiNER resolves the backbone at `main` over the "
             "network, and the cache proves nothing",
    ))

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
