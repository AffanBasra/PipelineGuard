import ast
import csv
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

PATH = Path(__file__).resolve().parent.parent / "data" / "nemotron-pii" / "test-00000-of-00001.parquet"
OUTPUT = PATH.parent / "results.csv"

pf = pq.ParquetFile(PATH)
counts = Counter()

for batch in pf.iter_batches(columns=["spans"], batch_size=2000):
    for spans_str in batch.column("spans").to_pylist():
        spans = ast.literal_eval(spans_str)
        for span in spans:
            counts[span["label"]] += 1

total = sum(counts.values())
print(f"Total entity mentions: {total}")
print(f"Distinct entity types: {len(counts)}")
print()

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["label", "count"])
    for label, count in counts.most_common():
        writer.writerow([label, count])
        print(f"{count:>8}  {label}")

print(f"\nSaved results to {OUTPUT}")