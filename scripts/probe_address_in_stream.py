"""Should ADDRESS go back into Tier 2, now that the stream contains addresses?

§13 removed it on two grounds. Both have changed, and neither change is a reason
to restore it without measuring:

  "no memo template produces an address"   -- false since the generator emits
                                             them (generator/addresses.py)
  "30% false-positive rate over 200 memos" -- measured at urchade @ 0.25, and
                                             §6.1 established that thresholds do
                                             not transfer between checkpoints,
                                             so the number does not carry to
                                             gliner-community @ 0.55 either way

This measures the shipped path -- Tier2Detector, not a reimplementation of it --
so the answer describes what the processor would actually do.

FOUR THINGS, because restoring a label group can fail in four different ways:

  coverage        does it find the addresses that are there?
  false positives does it invent addresses in memos that have none?
  interference    does it damage PERSON, which is at 99.4% and is the entity
                  the pipeline actually exists for? §13.2 found ADDRESS labels
                  re-tagging names the PERSON labels had already claimed.
  cost            LABEL_GROUPS runs one forward pass PER GROUP, so a second
                  group is a second pass over every escalated record.

Coverage is reported over identifying characters only. A ', ' surviving between
two redacted spans is not a leak, and counting it as one understates every model
by ~10 points (§17).

Usage:
    python scripts/probe_address_in_stream.py [--count 400] [--threshold 0.55]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipelineguard.detectors.tier2_encoder import Tier2Detector  # noqa: E402
from pipelineguard.generator.addresses import make_address  # noqa: E402
from pipelineguard.generator.transactions import (  # noqa: E402
    ADDRESS_MEMO_TEMPLATES,
    MEMO_TEMPLATES,
    fake,
    make_cnic,
    make_name,
    make_phone,
)

PERSON_ONLY = {"PERSON_NAME": ["person", "first_name", "last_name"]}
WITH_ADDRESS = {
    "PERSON_NAME": ["person", "first_name", "last_name"],
    "ADDRESS": ["address", "street_address", "location"],
}
# One group, both label sets, so one forward pass covers both entities. This is
# the cheap option and it is measured rather than assumed: §13 recorded that
# combining drops PERSON 99.4% -> 90.9% because the labels compete for the same
# spans, but that was urchade, and §6.1 established nothing transfers between
# checkpoints. If it holds here, restoring ADDRESS is free; if not, it costs 2x.
COMBINED = {
    "PERSON_NAME": ["person", "first_name", "last_name",
                    "address", "street_address", "location"],
}


def build_stream(count: int, rng: random.Random) -> tuple[list, list]:
    """(address-bearing memos with gold spans, address-free memos).

    The gold span is exact because this builds the memo: it substitutes the
    address into the template and records where it landed. No alignment, and no
    chance of the §15.2 wiring bug where the corpus and the labels disagreed.
    """
    bearing, clean = [], []
    for _ in range(count):
        address = make_address(rng)
        template = rng.choice(ADDRESS_MEMO_TEMPLATES)
        name = make_name()
        text = template.format(name=name, phone=make_phone(), cnic=make_cnic(),
                               email=fake.email(), inv=rng.randint(1000, 99999),
                               address=address)
        start = text.index(address)
        # The name, when the template carries one, so PERSON coverage can be
        # scored on exactly the memos ADDRESS is also competing for.
        name_at = text.index(name) if name in text else None
        bearing.append((text, start, start + len(address),
                        name_at, None if name_at is None else name_at + len(name)))

    for _ in range(count):
        template = rng.choice(MEMO_TEMPLATES)
        name, phone, cnic = make_name(), make_phone(), make_cnic()
        email = fake.email()
        text = template.format(name=name, phone=phone, cnic=cnic, email=email,
                               inv=rng.randint(1000, 99999))
        # The PII this memo is SUPPOSED to lose. Recorded at build time rather
        # than recovered with a regex afterwards: the first version of this
        # probe matched only emails, phones and CNICs, so every correctly
        # detected name scored as over-redaction and both arms reported an
        # identical 21.5% that was almost entirely legitimate detection.
        intended = set()
        for value in (name, phone, cnic, email):
            at = text.find(value)
            if at >= 0:
                intended |= set(range(at, at + len(value)))
        clean.append((text, intended))
    return bearing, clean


def _runs(text: str, positions: set[int]) -> list[str]:
    """Contiguous substrings at the given positions, for readable examples."""
    out: list[str] = []
    previous = -2
    for position in sorted(positions):
        if position == previous + 1:
            out[-1] += text[position]
        else:
            out.append(text[position])
        previous = position
    return out


def identifying_coverage(text: str, start: int, end: int, spans) -> tuple[int, int]:
    """(covered, total) alphanumeric characters in [start, end)."""
    gold = {p for p in range(start, end) if text[p].isalnum()}
    covered = set()
    for span_start, span_end in spans:
        covered |= set(range(span_start, span_end)) & gold
    return len(covered), len(gold)


def run(detector: Tier2Detector, texts: list[str]) -> tuple[dict, float]:
    """{index: {entity_type: [(start, end)]}} and wall time in ms."""
    inputs = {i: {"memo": text} for i, text in enumerate(texts)}
    t0 = time.perf_counter()
    findings = detector.detect_batch(inputs)
    elapsed = (time.perf_counter() - t0) * 1000

    out = {}
    for index, fields in findings.items():
        by_type: dict[str, list] = {}
        for finding in fields.get("memo", []):
            by_type.setdefault(finding.entity_type, []).append(
                (finding.span_start, finding.span_end)
            )
        out[index] = by_type
    return out, elapsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=400,
                    help="memos of EACH kind: address-bearing and address-free")
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--model", default="gliner-community/gliner_medium-v2.5")
    ap.add_argument("--seed", type=int, default=20260810)
    ap.add_argument("--out", default="address_in_stream.json")
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    random.seed(args.seed)
    bearing, clean = build_stream(args.count, rng)
    print(f"{len(bearing)} address-bearing memos, {len(clean)} address-free\n"
          f"model={args.model} threshold={args.threshold}\n", flush=True)

    results = {}
    redacted_by_arm: dict[str, list[set]] = {}
    arms = (
        ("person_only", PERSON_ONLY, False),
        ("with_address", WITH_ADDRESS, False),
        ("with_address_extended", WITH_ADDRESS, True),
        ("combined_one_pass", COMBINED, False),
    )
    for name, groups, extend in arms:
        detector = Tier2Detector(args.model, threshold=args.threshold,
                                 label_groups=groups, extend_addresses=extend)
        detector.load()

        found_b, ms_b = run(detector, [b[0] for b in bearing])
        found_c, ms_c = run(detector, [c[0] for c in clean])

        addr_cov = addr_tot = 0
        person_cov = person_tot = 0
        for i, (text, gs, ge, ns, ne) in enumerate(bearing):
            spans = found_b.get(i, {})
            covered, total = identifying_coverage(
                text, gs, ge, spans.get("ADDRESS", []) + spans.get("PERSON_NAME", [])
            )
            addr_cov += covered
            addr_tot += total
            if ns is not None:
                covered, total = identifying_coverage(
                    text, ns, ne,
                    spans.get("PERSON_NAME", []) + spans.get("ADDRESS", [])
                )
                person_cov += covered
                person_tot += total

        # False positives, on memos built from the templates that carry no
        # address at all.
        fired = sum(1 for i in range(len(clean)) if found_c.get(i))
        addr_fired = sum(1 for i in range(len(clean))
                         if found_c.get(i, {}).get("ADDRESS"))
        over = chars = 0
        redacted: list[set] = []
        for i, (text, intended) in enumerate(clean):
            touched = set()
            for group in found_c.get(i, {}).values():
                for span_start, span_end in group:
                    touched |= set(range(span_start, span_end))
            redacted.append(touched)
            over += len(touched - intended)
            chars += len(text)
        redacted_by_arm[name] = redacted

        entry = {
            "address_coverage": addr_cov / addr_tot if addr_tot else None,
            "person_coverage": person_cov / person_tot if person_tot else None,
            "clean_memos_fired": fired / len(clean),
            "clean_memos_address_label_fired": addr_fired / len(clean),
            "over_redaction_chars": over / chars,
            "ms_per_record": (ms_b + ms_c) / (len(bearing) + len(clean)),
        }
        results[name] = entry
        print(f"[{name}]")
        print(f"  ADDRESS coverage (identifying chars) : "
              f"{entry['address_coverage']:.1%}")
        print(f"  PERSON coverage in the same memos    : "
              f"{entry['person_coverage']:.1%}")
        print(f"  fires on an address-free memo        : "
              f"{entry['clean_memos_fired']:.1%}")
        print(f"  ...via the ADDRESS labels            : "
              f"{entry['clean_memos_address_label_fired']:.1%}")
        print(f"  over-redacted characters             : "
              f"{entry['over_redaction_chars']:.1%}")
        print(f"  cost                                 : "
              f"{entry['ms_per_record']:.2f} ms/record\n", flush=True)

    # The number the decision actually turns on. An ADDRESS label that re-tags a
    # span PERSON already claimed costs nothing: merge_spans() unions them and
    # the redacted output is byte-identical. Only characters that ADDRESS
    # redacts and PERSON alone would not are real damage, and §13.2's headline
    # "30% false-positive rate" did not separate the two.
    # Intended PII is excluded from the difference too. Adding the ADDRESS
    # labels also made the encoder catch emails the PERSON labels had missed;
    # that is Tier 1's job and it is not damage, but the first version of this
    # block counted it and reported 3.5% where the true figure is lower.
    before, after = redacted_by_arm["person_only"], redacted_by_arm["with_address"]
    extra = [(a - b) - intended
             for a, b, (_text, intended) in zip(after, before, clean)]
    new_chars = sum(len(e) for e in extra)
    memos_changed = sum(1 for e in extra if e)
    total_chars = sum(len(text) for text, _ in clean)
    examples = [
        (clean[i][0], _runs(clean[i][0], extra[i]))
        for i in range(len(clean)) if extra[i]
    ][:6]

    print("INCREMENTAL DAMAGE of adding the ADDRESS labels, on address-free memos")
    print(f"  memos redacted differently : {memos_changed / len(clean):.1%}")
    print(f"  characters newly redacted  : {new_chars / total_chars:.1%}")
    print(f"  cost multiplier            : "
          f"{results['with_address']['ms_per_record'] / results['person_only']['ms_per_record']:.2f}x")
    for text, runs in examples:
        print(f"    {text!r}\n       newly redacted: {runs}")

    # The span-extension rule reaches OUTSIDE the span the model chose, so it is
    # the one change here that can damage text no model ever flagged. Measured
    # against the same arm without it, on memos containing no address at all.
    base = redacted_by_arm["with_address"]
    stretched = redacted_by_arm["with_address_extended"]
    ext_extra = [(a - b) - intended
                 for a, b, (_text, intended) in zip(stretched, base, clean)]
    print("\nSPAN EXTENSION, incremental damage on address-free memos")
    print(f"  memos redacted differently : "
          f"{sum(1 for e in ext_extra if e) / len(clean):.1%}")
    print(f"  characters newly redacted  : "
          f"{sum(len(e) for e in ext_extra) / total_chars:.1%}")
    for i in [i for i in range(len(clean)) if ext_extra[i]][:6]:
        print(f"    {clean[i][0]!r}\n       newly redacted: "
              f"{_runs(clean[i][0], ext_extra[i])}")

    Path(args.out).write_text(json.dumps({
        "model": args.model, "threshold": args.threshold,
        "count": args.count, "seed": args.seed, "results": results,
        "incremental": {
            "memos_changed": memos_changed / len(clean),
            "chars_newly_redacted": new_chars / total_chars,
        },
    }, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
