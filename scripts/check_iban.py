"""Interactive check of RulesDetector's IBAN_PK detection + mod-97 checksum.

Run:  python scripts/check_iban.py
Paste a real IBAN (Pakistani or otherwise, format-compatible) and see whether
it's detected, and whether the checksum passed (confidence 1.0) or failed
(confidence 0.5, per our "format matched but checksum failed" design).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipelineguard.detectors.tier1_rules import RulesDetector

if __name__ == "__main__":
    d = RulesDetector()
    while True:
        raw = input("IBAN (or blank to quit): ").strip()
        if not raw:
            break

        findings = d.detect(raw, "iban_test")
        if not findings:
            print("  -> no IBAN_PK match (format didn't match the regex)\n")
            continue

        for f in findings:
            matched_text = raw[f.span_start:f.span_end]
            checksum_ok = d._iban_checksum_ok(matched_text)
            print(f"  -> matched: {matched_text!r}")
            print(f"     checksum_ok={checksum_ok}  confidence={f.confidence}")
        print()