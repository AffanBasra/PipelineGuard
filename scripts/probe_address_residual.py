"""Where, inside an address, do the missed characters actually sit?

§14.4 recorded the shape of the address problem: any-hit ~99% against coverage
~85%. The models find the address and drop part of it. A partly-redacted address
looks redacted and is not, so the residual -- not the hit rate -- is what a
firewall lives or dies on.

That finding says nothing about WHERE the residual is, and the answer decides
whether a rule can fix it. Three possibilities, with very different consequences:

  leading    the house number before the street ('14, Hill Road, F-6/3') --
             recoverable by extending the span left over one token
  trailing   the city at the end ('..., Islamabad') -- recoverable by extending
             right
  interior   a gap in the middle -- NOT recoverable by extending anything, and
             the only one of the three that argues for fine-tuning

This measures the split, and the leading+trailing share is a CEILING on what any
span-extension rule can recover. It is deliberately run before that rule is
written: a rule designed against an assumed failure mode measures its own
assumption.

SEPARATORS ARE NOT LEAKS, and this is why the script reports two coverages.
When a model returns two adjacent spans for one address, char_coverage scores
the ', ' between them as missed. Redaction then emits '[ADDRESS], [ADDRESS]' --
a comma survives, and nothing identifying does. Measured here, 53% of interior
misses are punctuation and whitespace alone. So:

  raw          every gold character, the §14-§16 metric, kept for comparability
  effective    alphanumeric gold characters only -- the ones that can identify
               a person or a dwelling

Every number in §14, §15 and §16 is a raw number and therefore understates the
models. The ranking between them is largely unaffected, since all four pay the
same penalty; the distance to 100% is not.

Usage:
    python scripts/probe_address_residual.py [--sample 300] [--threshold 0.55]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_address_real import build_cases, load_corpus  # noqa: E402

DEFAULT_MODEL = "gliner-community/gliner_medium-v2.5"
LABELS = ["address", "street_address", "location"]


def is_identifying(ch: str) -> bool:
    """A character that can carry identity. Punctuation and whitespace cannot:
    a surviving ', ' between two redacted spans names nobody."""
    return ch.isalnum()


def residual(text: str, gold_start: int, gold_end: int, spans) -> dict:
    """Where the missed characters sit, counted raw and identifying-only.

    Leading and trailing are measured from the outermost COVERED character, so
    an address whose middle is found and whose two ends are dropped reports
    both -- the case a two-sided extension rule is for.
    """
    gold = set(range(gold_start, gold_end))
    ident = {p for p in gold if is_identifying(text[p])}
    covered = set()
    for start, end, _ in spans:
        covered |= set(range(start, end)) & gold

    out = {
        "n": 1,
        "gold": len(gold), "gold_ident": len(ident),
        "covered": len(covered), "covered_ident": len(covered & ident),
        "leading": 0, "trailing": 0, "interior": 0, "not_found": 0,
        "sep_missed": len((gold - covered) - ident),
        "no_hit": int(not covered),
        "perfect_ident": int(ident <= covered),
    }
    missed_ident = ident - covered
    if not covered:
        out["not_found"] = len(missed_ident)
        return out

    first, last = min(covered), max(covered)
    for position in missed_ident:
        if position < first:
            out["leading"] += 1
        elif position > last:
            out["trailing"] += 1
        else:
            out["interior"] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--stratify", choices=("kind", "form"), default="form")
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--extend", action="store_true",
                    help="apply tier2_encoder.extend_address_span to every span "
                         "before scoring, to measure what the rule recovers")
    ap.add_argument("--out", default="address_residual.json")
    args = ap.parse_args(argv)

    from gliner import GLiNER
    import torch

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from pipelineguard.detectors.tier2_encoder import extend_address_span

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"model={args.model} thr={args.threshold} device={device} "
          f"extend={args.extend}", flush=True)
    model = GLiNER.from_pretrained(args.model, map_location=device)
    model.eval()

    cases = build_cases(load_corpus(args.sample, args.stratify))
    print(f"{len(cases)} cases  "
          f"{dict(Counter(c[2] for c in cases))}\n", flush=True)

    totals = Counter()
    by_form = defaultdict(Counter)
    # Kept for the writeup: a rule argued from aggregate percentages and no
    # examples is how §15's sector-code claim got published wrong. One example
    # per distinct address, because each address runs through 12 sentence frames
    # and would otherwise fill the list with itself.
    examples = defaultdict(list)
    seen: set[tuple[str, str]] = set()

    for start in range(0, len(cases), args.batch_size):
        chunk = cases[start:start + args.batch_size]
        batch = model.batch_predict_entities(
            [c[4] for c in chunk], LABELS, threshold=args.threshold
        )
        for (_style, _kind, form, addr, text, gs, ge), ents in zip(chunk, batch):
            spans = [(e["start"], e["end"], "") for e in ents]
            if args.extend:
                spans = [(*extend_address_span(text, s, e), label)
                         for s, e, label in spans]
            row = residual(text, gs, ge, spans)
            totals.update(row)
            by_form[form].update(row)

            for part in ("leading", "trailing", "interior"):
                if row[part] and (part, addr) not in seen and len(examples[part]) < 6:
                    seen.add((part, addr))
                    covered = set()
                    for s, e, _ in spans:
                        covered |= set(range(s, e)) & set(range(gs, ge))
                    leaked = "".join(
                        text[p] if p not in covered else "·"
                        for p in range(gs, ge)
                    )
                    examples[part].append(f"{addr!r}\n         survives: {leaked!r}")

    n = totals["n"]
    raw_cov = totals["covered"] / totals["gold"]
    eff_cov = totals["covered_ident"] / totals["gold_ident"]
    missed = totals["gold_ident"] - totals["covered_ident"]
    edge = totals["leading"] + totals["trailing"]

    print(f"cases {n:,}   no-hit {totals['no_hit'] / n:.1%}   "
          f"fully covered (identifying chars) {totals['perfect_ident'] / n:.1%}")
    print(f"\n  raw coverage        {raw_cov:>7.1%}   (the §14-§16 metric)")
    print(f"  effective coverage  {eff_cov:>7.1%}   (alphanumeric only)")
    print(f"  separator-only chars scored as missed by the raw metric: "
          f"{totals['sep_missed']:,}")

    print(f"\nmissed IDENTIFYING characters: {missed:,} of {totals['gold_ident']:,}")
    for part in ("leading", "trailing", "interior", "not_found"):
        share = totals[part] / missed if missed else 0.0
        print(f"  {part:<18} {totals[part]:>8,}  {share:>6.1%}")

    ceiling = edge / missed if missed else 0.0
    print(f"\n  CEILING for a span-extension rule: {ceiling:.1%} of the missed "
          f"identifying\n  characters ({edge:,} chars). Recovering ALL of them "
          f"would take effective\n  coverage {eff_cov:.1%} -> "
          f"{(totals['covered_ident'] + edge) / totals['gold_ident']:.1%}.")

    print("\nby form (shares are of missed identifying characters):")
    print(f"  {'form':<14}{'n':>7}{'raw':>8}{'eff':>8}{'lead':>8}"
          f"{'trail':>8}{'inner':>8}{'none':>8}")
    for form, c in sorted(by_form.items()):
        miss = c["gold_ident"] - c["covered_ident"] or 1
        print(f"  {form:<14}{c['n']:>7,}{c['covered'] / c['gold']:>8.1%}"
              f"{c['covered_ident'] / c['gold_ident']:>8.1%}"
              f"{c['leading'] / miss:>8.1%}{c['trailing'] / miss:>8.1%}"
              f"{c['interior'] / miss:>8.1%}{c['not_found'] / miss:>8.1%}")

    for part in ("leading", "trailing", "interior"):
        if examples[part]:
            print(f"\n{part} examples ('·' = redacted, anything else survives):")
            for row in examples[part]:
                print(f"    {row}")

    Path(args.out).write_text(json.dumps({
        "model": args.model, "threshold": args.threshold, "cases": n,
        "raw_coverage": raw_cov, "effective_coverage": eff_cov,
        "missed_identifying": missed,
        "separator_chars_scored_missed": totals["sep_missed"],
        "leading": totals["leading"], "trailing": totals["trailing"],
        "interior": totals["interior"], "not_found": totals["not_found"],
        "extension_ceiling": ceiling,
        "by_form": {f: dict(c) for f, c in by_form.items()},
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
