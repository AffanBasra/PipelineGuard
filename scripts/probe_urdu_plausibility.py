"""Is the hand-written Roman Urdu in these probes what Pakistanis actually write?

docs/tier2-detection-findings.md section 5 records the softest point in the
whole document:

    The Roman Urdu was written by the author, who is not a native writer of it.
    If the phrasing is unnatural, the models are being judged on text no real
    user would produce, and the address-form numbers deserve the most scrutiny
    since they turn on Ghar/Gali/Makan.

That caveat sits under the project's headline detection finding, so it is worth
closing rather than restating. This checks the probe vocabulary against public
Roman-Urdu corpora written by actual speakers.

Three corpora rather than one, so a word is judged attested by independent
sources rather than by whichever single dataset happened to be picked:

    community-datasets/roman_urdu               20,229 sentences
    Khubaib01/RomanUrdu-NLP-Sentiment-Corpus   134,053 messages
    hafiz-hassaan-saeed/Roman-Urdu-Toxic-Corpus 72,771 sentences

What this can and cannot establish: attestation shows the *words* are real and
tells you how common they are. It does not show the *sentences* are idiomatic --
word order, agreement and register are not tested here, and a fluent speaker
reading the templates would still be worth more than this script. It closes the
vocabulary half of the caveat and narrows the rest.

PRIVACY. These are scraped social-media corpora and may contain real people's
names. docs/decisions.md section 1 commits this project to processing no real
personal data, so the corpora are used strictly one-directionally: token counts
are computed, this project's OWN word list is looked up against them, and only
aggregate statistics about our words are printed or written. No corpus sentence
is ever stored, printed or committed, and the download is left in the HF cache
rather than copied into the repo.

Usage:
    python scripts/probe_urdu_plausibility.py [--out urdu_plausibility.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

CORPORA = [
    ("roman_urdu", "community-datasets/roman_urdu", "sentence"),
    ("sentiment", "Khubaib01/RomanUrdu-NLP-Sentiment-Corpus", "message"),
    ("toxic", "hafiz-hassaan-saeed/Roman-Urdu-Toxic-Corpus", "Roman_Urdu"),
]

# The words the findings actually turn on. Grouped so the report says which
# part of the argument each result supports.
VOCAB = {
    # Section 3's entire address finding rests on these three.
    "address_structural": ["ghar", "gali", "makan", "pata", "sector"],
    # Roman-Urdu memo templates in probe_ner_locale.py.
    "memo_verbs": ["bhej", "bhejunga", "bhaij", "diya", "diye", "kar", "karwa",
                   "jama", "ada", "hai", "hain", "gaya", "gayi", "rehta",
                   "dein", "karna", "chahiye", "hua", "liye"],
    "memo_nouns": ["paisay", "paise", "rakam", "raqam", "kiraya", "bijli",
                   "gas", "pani", "bill", "salary", "qarza", "qist", "zakat",
                   "hisab", "dukan", "mahine", "bhai", "roshni", "kaam",
                   "masla", "phal", "waqt", "baat"],
    # Name-homograph nouns: section 8.4 found these drive over-redaction.
    "name_homographs": ["noor", "iman", "sana", "aman", "sabr", "shan",
                        "fajar", "rehmat"],
}

_TOKEN = re.compile(r"[a-z]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def build_frequencies(corpus_id: str, column: str) -> tuple[Counter, int]:
    """Token counts for one corpus.

    Returns counts and total tokens. The Counter is the only thing that
    survives this function -- corpus text is never returned, stored or logged.
    """
    from datasets import load_dataset

    ds = load_dataset(corpus_id)
    counts: Counter = Counter()
    total = 0
    for split in ds.values():
        for row in split:
            value = row.get(column)
            if not isinstance(value, str):
                continue
            toks = tokenize(value)
            counts.update(toks)
            total += len(toks)
    return counts, total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="urdu_plausibility.json")
    args = ap.parse_args(argv)

    freqs = {}
    for name, corpus_id, column in CORPORA:
        print(f"loading {corpus_id} ...", flush=True)
        counts, total = build_frequencies(corpus_id, column)
        freqs[name] = (counts, total)
        print(f"  {name}: {total:,} tokens, {len(counts):,} distinct", flush=True)

    results = {"corpora": {n: {"tokens": t, "distinct": len(c)}
                           for n, (c, t) in freqs.items()},
               "vocab": {}}

    print()
    hdr = f"{'word':<14} {'corpora':>8} " + " ".join(
        f"{n:>12}" for n, _, _ in CORPORA)
    for group, words in VOCAB.items():
        print(f"\n[{group}]  (per-million frequency, '-' = absent)")
        print(hdr)
        print("-" * len(hdr))
        for w in words:
            row = {}
            n_attested = 0
            for name, _, _ in CORPORA:
                counts, total = freqs[name]
                c = counts.get(w, 0)
                row[name] = {"count": c, "per_million": 1e6 * c / total if total else 0}
                n_attested += bool(c)
            results["vocab"][w] = {"group": group, "corpora_attested": n_attested,
                                   **row}
            cells = " ".join(
                f"{row[n]['per_million']:>12.1f}" if row[n]["count"] else f"{'-':>12}"
                for n, _, _ in CORPORA)
            print(f"{w:<14} {n_attested:>8}/3 {cells}")

    # ---- per-sentence attestation -------------------------------------------
    # A single unattested word matters more than the vocabulary table suggests:
    # an out-of-vocabulary token is exactly the kind of thing a NER model
    # flags, so a false positive on a sentence containing one may be an
    # artifact of the author's Urdu rather than a property of the model.
    # This locates which probe sentences carry that risk.
    from probe_ner_locale import NAME_TEMPLATES, ADDRESS_TEMPLATES
    from probe_ner_precision import PK_NEGATIVE_PLAIN, PK_NEGATIVE_AMBIGUOUS

    def attested_in(word):
        return sum(1 for name, _, _ in CORPORA if freqs[name][0].get(word, 0))

    # English loanwords are normal in code-switched Urdu and should not be
    # counted as unattested Urdu -- they are attested, just as English.
    sentences = []
    for tmpl in NAME_TEMPLATES["roman_urdu"]:
        sentences.append(("probe_person_template", tmpl.replace("{x}", "")))
    for tmpl in ADDRESS_TEMPLATES["roman_urdu"]:
        sentences.append(("probe_address_template", tmpl.replace("{x}", "")))
    for s in PK_NEGATIVE_PLAIN:
        sentences.append(("pk_negative_plain", s))
    for s in PK_NEGATIVE_AMBIGUOUS:
        sentences.append(("pk_negative_ambiguous", s))

    print("\n" + "=" * 70)
    print("sentences containing a token attested in fewer than 3 corpora")
    print("=" * 70)
    flagged = []
    for kind, sent in sentences:
        weak = [(w, attested_in(w)) for w in tokenize(sent) if attested_in(w) < 3]
        if weak:
            flagged.append({"kind": kind, "sentence": sent,
                            "weak_tokens": {w: n for w, n in weak}})
            desc = ", ".join(f"{w}({n}/3)" for w, n in weak)
            print(f"  [{kind}] {sent!r}\n      -> {desc}")
    if not flagged:
        print("  none")
    results["flagged_sentences"] = flagged
    print(f"\n  {len(flagged)} of {len(sentences)} probe sentences affected")

    # ---- summary ------------------------------------------------------------
    print("\n" + "=" * 70)
    for group, words in VOCAB.items():
        n3 = sum(1 for w in words if results["vocab"][w]["corpora_attested"] == 3)
        n0 = sum(1 for w in words if results["vocab"][w]["corpora_attested"] == 0)
        print(f"{group:<20} {n3:>2}/{len(words):<3} in all three, "
              f"{n0} absent everywhere")
    print("=" * 70)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
