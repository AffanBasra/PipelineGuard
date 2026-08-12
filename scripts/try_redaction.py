"""Type free text, see exactly what the pipeline would redact.

The detectors and the rewrite are the shipped ones -- RulesDetector,
Tier2Detector and processor.redact() -- so what this prints is what a memo
carrying this text would look like on txn.clean.

Tier 2 is optional. Without it you see the rules only, which is what the
processor does when TIER2_ENABLED=false.

Usage:
    python scripts/try_redaction.py "Transfer to Ayesha Malik, House 12, Karachi"
    python scripts/try_redaction.py --tier2 "Deliver to C-21, Block J, Karachi"
    python scripts/try_redaction.py --tier2          # interactive, one line at a time
    echo "..." | python scripts/try_redaction.py --tier2
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import warnings
from pathlib import Path

# Set before anything imports huggingface_hub or transformers, which read these
# at import time. This is an inspection tool: progress bars for an already-warm
# cache and deprecation notices from inside GLiNER drown the answer.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipelineguard.config import settings          # noqa: E402
from pipelineguard.detectors.tier1_rules import RulesDetector  # noqa: E402
from pipelineguard.models import Tier              # noqa: E402
from pipelineguard.processor import merge_spans, redact        # noqa: E402

FIELD = "memo"


def load_tier2(device: str):
    from pipelineguard.detectors.tier2_encoder import Tier2Detector

    detector = Tier2Detector(
        settings.tier2_model,
        threshold=settings.tier2_threshold,
        device=device,
        batch_size=settings.tier2_batch_size,
        revision=settings.tier2_model_revision,
    )
    detector.load()
    return detector


def show(text: str, tier1: RulesDetector, tier2) -> None:
    findings = list(tier1.detect(text, FIELD))
    if tier2 is not None:
        findings += tier2.detect(text, FIELD)

    print(f"\n  IN  : {text}")
    print(f"  OUT : {redact(text, findings)}")

    if not findings:
        print("  --- nothing detected")
        if tier2 is None:
            # The most likely reason, and the least obvious one. The rules match
            # formats -- CNIC, IBAN, phone, email. A name or an address in free
            # text has no format, which is the whole reason Tier 2 exists.
            print("  --- rules only: they match FORMATS (CNIC, IBAN, phone,"
                  " email).\n      Names and addresses need --tier2.")
        return

    print(f"  --- {len(findings)} finding(s), before the spans are merged")
    for finding in sorted(findings, key=lambda x: (x.span_start, x.span_end)):
        tier = "rules" if finding.tier == Tier.RULES else "encoder"
        conf = ("" if finding.tier == Tier.RULES
                else f"  conf={finding.confidence:.2f}")
        print(f"       {tier:<8} {finding.entity_type:<12} "
              f"[{finding.span_start:>3},{finding.span_end:>3}) "
              f"{text[finding.span_start:finding.span_end]!r}{conf}")

    merged = merge_spans(findings)
    if len(merged) != len(findings):
        print(f"  --- merged into {len(merged)} span(s)")
        for start, end, label in merged:
            print(f"       {label:<24} {text[start:end]!r}")

    covered = sum(1 for s, e, _ in merged for p in range(s, e) if text[p].isalnum())
    total = sum(1 for c in text if c.isalnum())
    print(f"  --- {covered}/{total} identifying characters redacted "
          f"({covered / total:.0%})" if total else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("text", nargs="*", help="text to redact; omit to read stdin")
    ap.add_argument("--tier2", action="store_true",
                    help="also run the encoder (needs `pip install '.[tier2]'`)")
    ap.add_argument("--device", default=settings.tier2_device,
                    help="cpu | cuda | auto (default from config)")
    args = ap.parse_args(argv)

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tier1 = RulesDetector()
    tier2 = None
    if args.tier2:
        try:
            tier2 = load_tier2(args.device)
        except ImportError:
            print("Tier 2 needs torch and gliner: pip install '.[tier2]'",
                  file=sys.stderr)
            return 2
        print(f"tier 2: {settings.tier2_model}@{settings.tier2_model_revision[:12]} "
              f"on {tier2.device}, threshold {settings.tier2_threshold}")

    if args.text:
        show(" ".join(args.text), tier1, tier2)
        return 0

    if not sys.stdin.isatty():
        for line in sys.stdin:
            if line.strip():
                show(line.rstrip("\n"), tier1, tier2)
        return 0

    print("Type a line and press Enter. Ctrl-C to stop.")
    while True:
        try:
            line = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip():
            show(line, tier1, tier2)


if __name__ == "__main__":
    sys.exit(main())