"""Do the address findings survive contact with addresses nobody here wrote?

§3 and §6.2 measured a ~21 point coverage penalty on Urdu-form addresses,
replicated across two models. §5 recorded what sat underneath it: the author
wrote every one of those addresses. This scores four encoders on 7,371 real
OpenStreetMap addresses instead.

DESIGN. One variable changes. The sentence frames come from
probe_ner_locale.ADDRESS_TEMPLATES unchanged -- english, codeswitch,
roman_urdu -- with a real address substituted for the hand-written one. The gold
span needs no alignment: this script inserts the address, so it knows the
offsets exactly.

THE SPLIT THAT MATTERS. A home address is personal data; a shop's address
largely is not. So coverage is reported for residential and commercial
separately, not pooled. 78.7% of the corpus has no building type and is reported
as unknown rather than guessed into a bucket.

WHAT THIS CANNOT SAY. The corpus is Lahore block/phase forms; §6.2's addresses
were Islamabad sector forms. So a comparison against §6.2 changes provenance,
city and convention at once. Model-against-model on this corpus is controlled
and decides the fine-tuning question; the §6.2 comparison is indicative only.

Usage:
    python scripts/probe_address_real.py [--sample 400] [--only urchade]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_ner_locale import (  # noqa: E402
    ADDRESS_TEMPLATES,
    NAME_TEMPLATES,
    NAMES,
    char_coverage,
)

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "data" / "osm-addresses" / "addresses.json"

# (name, hf id, kind). GLiNER takes labels at call time; the token classifier
# has a fixed label set and needs a different adapter.
MODELS = [
    ("urchade", "urchade/gliner_multi_pii-v1", "gliner"),
    ("nvidia", "nvidia/gliner-PII", "gliner"),
    ("gliner_community", "gliner-community/gliner_medium-v2.5", "gliner"),
    ("xlmr_conll", "FacebookAI/xlm-roberta-large-finetuned-conll03-english",
     "token"),
]

GLINER_LABELS = {
    "address": ["address", "street_address", "location"],
    "person": ["person", "first_name", "last_name"],
}
# CoNLL03 has no address class. LOC is the closest it can offer for an address
# and ORG catches business addresses; PER is a genuine match for a name.
TOKEN_LABELS = {"address": ("LOC", "ORG"), "person": ("PER",)}

THRESHOLDS = (0.25, 0.40, 0.55)

# Reported separately. 'unknown' is carried because it is 78.7% of the corpus
# and dropping it would report a number computed on a fifth of the data.
KINDS = ("residential", "commercial", "unknown")


def load_corpus(sample: int, stratify: str = "kind",
                seed: int = 20260807) -> list[dict]:
    """Latin-script addresses, balanced across `stratify` where possible.

    Stratifying by kind leaves rare FORMS badly under-sampled: sector_code is
    2.8% of the corpus, so a kind-balanced sample of 300 held 13 of them. §15
    then claimed the form penalty was "measured on 2,592 sector-code addresses",
    which was the corpus count, not the scored count. Stratify by form to make
    a claim about forms.

    Script coverage is a different question from the one being asked, and the
    Urdu-script addresses are a small minority, so mixing them in would confound
    the comparison for no gain.
    """
    payload = json.loads(CORPUS.read_text(encoding="utf-8"))
    records = [r for r in payload["addresses"] if r["latin"]]

    buckets = sorted({r[stratify] for r in records})
    rng = random.Random(seed)
    per_bucket = max(1, sample // len(buckets))
    chosen = []
    for bucket in buckets:
        pool = [r for r in records if r[stratify] == bucket]
        rng.shuffle(pool)
        take = pool[:per_bucket]
        if len(take) < per_bucket:
            print(f"  note: only {len(take)} addresses for {stratify}="
                  f"{bucket}, wanted {per_bucket}", flush=True)
        chosen.extend(take)
    return chosen


def build_cases(records: list[dict]) -> list[tuple]:
    """(style, kind, form, address, text, gold_start, gold_end).

    Every address goes through every frame, so style is a within-address
    comparison rather than a between-address one.
    """
    cases = []
    for record in records:
        address = record["address"]
        for style, templates in ADDRESS_TEMPLATES.items():
            for template in templates:
                text = template.format(x=address)
                start = text.index(address)
                cases.append((style, record["kind"], record["form"], address,
                              text, start, start + len(address)))
    return cases


def build_person_cases() -> list[tuple]:
    """The same shape, over §2's synthetic names.

    Names stay synthetic on purpose. docs/decisions.md §1 forbids processing
    real personal data, and a real person's name is exactly that -- so unlike
    addresses, there is no honest way to source these externally. That makes
    the PERSON numbers comparable to §2 rather than independent of it, and the
    writeup has to say so.

    `kind` carries the name difficulty band (common / rare / ambiguous) so the
    same reporting code works for both entity types.
    """
    cases = []
    for style, templates in NAME_TEMPLATES.items():
        for template in templates:
            for band, name in NAMES:
                text = template.format(x=name)
                start = text.index(name)
                cases.append((style, band, "name", name,
                              text, start, start + len(name)))
    return cases


def load_gliner(model_id: str, entity: str):
    from gliner import GLiNER
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = GLiNER.from_pretrained(model_id, map_location=device)
    model.eval()
    labels = GLINER_LABELS[entity]

    def predict(texts: list[str], threshold: float):
        batch = model.batch_predict_entities(texts, labels, threshold=threshold)
        return [[(e["start"], e["end"], "") for e in ents] for ents in batch]

    return predict


def load_token_classifier(model_id: str, entity: str):
    """CoNLL03 has no address class, so ADDRESS is scored against LOC and ORG --
    the closest labels it can offer. That mismatch is part of the result rather
    than a flaw in the harness; §3 recorded the same for dslim/bert-base-NER.
    PER is a genuine match for a name, so the PERSON numbers are a fair test of
    this model and the ADDRESS numbers are not."""
    import torch
    from transformers import pipeline

    ner = pipeline("ner", model=model_id, aggregation_strategy="simple",
                   device=0 if torch.cuda.is_available() else -1)
    wanted = TOKEN_LABELS[entity]

    def predict(texts: list[str], threshold: float):
        out = []
        for result in ner(texts):
            out.append([
                (e["start"], e["end"], "")
                for e in result
                if e["entity_group"] in wanted and e["score"] >= threshold
            ])
        return out

    return predict


def score(predict, cases, threshold, batch_size=16) -> dict:
    agg = defaultdict(lambda: {"n": 0, "cov": 0.0, "hit": 0})
    for start in range(0, len(cases), batch_size):
        chunk = cases[start:start + batch_size]
        preds = predict([c[4] for c in chunk], threshold)
        for (style, kind, form, _addr, _text, gs, ge), spans in zip(chunk, preds):
            covered = char_coverage(gs, ge, spans)
            hit = any(gs < e and s < ge for s, e, _ in spans)
            for key in (("ALL", "ALL"), ("kind", kind), ("style", style),
                        ("form", form)):
                bucket = agg[key]
                bucket["n"] += 1
                bucket["cov"] += covered
                bucket["hit"] += hit
    return {
        f"{k[0]}:{k[1]}": {"n": v["n"], "coverage": v["cov"] / v["n"],
                           "any_hit": v["hit"] / v["n"]}
        for k, v in agg.items()
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=300,
                    help="TOTAL addresses, split evenly across the strata; "
                         "each is then run through every sentence frame")
    ap.add_argument("--stratify", choices=("kind", "form"), default="kind",
                    help="balance the sample by building type or address form. "
                         "Claims about forms need form stratification.")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--entity", choices=("address", "person"),
                    default="address")
    ap.add_argument("--thresholds", nargs="*", type=float, default=None,
                    help="override the GLiNER sweep; thresholds do not transfer "
                         "between checkpoints (see findings 6.1)")
    ap.add_argument("--out", default="address_real_results.json")
    args = ap.parse_args(argv)

    if args.entity == "person":
        cases = build_person_cases()
    else:
        if not CORPUS.exists():
            print(f"no corpus at {CORPUS}\n"
                  f"run scripts/build_address_corpus.py first", file=sys.stderr)
            return 1
        cases = build_cases(load_corpus(args.sample, args.stratify))

    by_kind = defaultdict(int)
    for case in cases:
        by_kind[case[1]] += 1
    # The bucket names are printed because they are the tell when entity and
    # corpus disagree: a person run that reports 'residential' is scoring
    # addresses with person labels, which looks like a terrible model rather
    # than a wiring bug.
    print(f"entity={args.entity}  {len(cases)} cases  {dict(by_kind)}\n",
          flush=True)

    results = {}
    for name, model_id, kind in MODELS:
        if args.only and name not in args.only:
            continue
        print("=" * 76, f"\n{name}  ({model_id})\n", "=" * 76, flush=True)
        try:
            loader = load_gliner if kind == "gliner" else load_token_classifier
            predict = loader(model_id, args.entity)
        except Exception as exc:  # noqa: BLE001 -- one bad model must not sink the run
            print(f"  FAILED to load: {type(exc).__name__}: {exc}", flush=True)
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue

        # A token classifier's score is a real probability and does not respond
        # to GLiNER's thresholds, so it is scored once.
        thresholds = (tuple(args.thresholds) if args.thresholds
                      else THRESHOLDS) if kind == "gliner" else (0.5,)
        entry = {"model_id": model_id, "kind": kind}
        for threshold in thresholds:
            t0 = time.perf_counter()
            entry[str(threshold)] = score(predict, cases, threshold)
            row = entry[str(threshold)]
            buckets = " ".join(
                f"{k.split(':')[1]} {v['coverage']:.1%}"
                for k, v in sorted(row.items()) if k.startswith("kind:"))
            print(f"  thr {threshold}  ALL {row['ALL:ALL']['coverage']:.1%}  "
                  f"{buckets}  ({time.perf_counter() - t0:.0f}s)", flush=True)
        results[name] = entry

    # Merge rather than overwrite. --only is the documented way to re-run a
    # model that failed on a network error, and overwriting would destroy the
    # results of the models that had succeeded.
    out = Path(args.out)
    if out.exists():
        try:
            merged = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            merged = {}
        merged.update(results)
        results = merged
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
