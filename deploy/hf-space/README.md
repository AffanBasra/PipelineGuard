---
title: PipelineGuard
emoji: 🛡️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# PipelineGuard — PII detection and redaction for Pakistani-locale text

A two-tier detector for personal data in transaction narration, running live.

- **Tier 1 — rules.** CNIC, Pakistani IBAN (mod-97 checked), Pakistani phone
  numbers, email. Exact, and fast enough to be free.
- **Tier 2 — encoder.** A GLiNER model reads free text for names and addresses,
  which have no fixed format for a rule to match.

## What to try

**Playground.** Start from one of the documented cases in the dropdown. They
are chosen because each shows something specific — an address bridged across an
interior gap, a city trailing an address in Roman Urdu, and two honest failures
where the model is wrong.

**Batch scan.** Upload a small CSV and download the redaction report.

**Governance report.** A stored run over synthetic records, showing what the
pipeline reports to a compliance reader.

## Privacy

Your input is read into memory, scanned, and dropped when the scan finishes. It
is never written to disk, never logged, and never added to any database. Only
the redacted output and the counts stay on screen.

This is a demonstration on shared free hosting. **Please use synthetic data.**
Nothing here is a compliance determination.

## Notes

- First load takes 30–50 seconds while the container wakes and the encoder
  starts. Tier 1 rules answer immediately while that happens.
- The encoder runs on CPU here, roughly six times slower than on a GPU.
- Scans are serialised, so a large batch will make other visitors wait.

Source, design decisions and measurements:
<https://github.com/AffanBasra/PipelineGuard>

Questions, or want this taken down: affanbasra12@gmail.com
