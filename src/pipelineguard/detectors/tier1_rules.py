"""Tier 1: regex + checksum rules.  <<< AFFAN'S IMPLEMENTATION — learning task >>>

Your job: implement RulesDetector so it satisfies the Detector protocol
(detectors/base.py) and passes the acceptance checks at the bottom.

Entities to detect (entity_type strings must match these exactly — the audit
schema and future benchmarks key on them):

  CNIC      — 13 digits. Canonical: 12345-1234567-1. Also match the bare
              13-digit run (3520212345671). VALIDATION beyond the regex:
              first digit of the first group is 1-7 (province code range),
              last digit encodes gender (odd=male, even=female) — you don't
              need to store gender, but a 13-digit run whose first digit is
              0/8/9 should NOT be reported as CNIC. This validate-after-match
              step is what separates a rules engine from a grep.
  IBAN_PK   — "PK" + 2 digits + 4 alnum bank code + 16 digits (24 chars).
              Stretch (recommended, it's a great interview detail): verify the
              ISO 7064 mod-97 check digits instead of trusting the format.
  PHONE_PK  — Pakistani mobiles in the wild formats the generator emits:
              03XXYYYYYYY, +92 3XX YYYYYYY, +923XXYYYYYYY. Normalize before
              validating; prefix after country code must start with 3.
  EMAIL     — a sane RFC-lite pattern is fine; don't gold-plate this one.

Design constraints:
  * detect() must be pure (no I/O) and fast — this tier's benchmark story is
    "microseconds per field". Compile patterns once at module or __init__ time,
    never inside detect().
  * Return character spans into the ORIGINAL text (span_start/span_end), even
    if you normalized a copy for validation. Redaction slices the original.
  * confidence: 1.0 for hits that pass validation. If you choose to also emit
    format-matched-but-checksum-failed candidates, give them lower confidence
    — but decide and be consistent; the processor treats <1.0 as escalatable.
  * Overlapping matches: keep the longest span (a CNIC inside a longer digit
    run shouldn't double-report).

Testing hint: generator/transactions.py is your fixture factory — every
make_transaction() payload contains ≥5 plantable entities with known positions
if you build the memo yourself in tests.
"""
from __future__ import annotations

from pipelineguard.models import Finding, Tier
import re

from email_validator import EmailNotValidError, validate_email


class RulesDetector:
    name = "tier1_rules"

    # Punctuation that free-text memos tack onto an embedded email
    # ("notify at affan@example.com.", "(affan@example.com)") but that
    # isn't actually part of the address.
    _LEADING_PUNCT = "([{'\""
    _TRAILING_PUNCT = ".,;:!?)]}'\""

    def __init__(self) -> None:
        # Loose candidate finder: email_validator does the real rejection,
        # this regex's only job is locating "@"-containing tokens and their spans.
        self._email_candidate_re = re.compile(r"\S+@\S+")
        self._cnic_canonical_re = re.compile(r"\b\d{5}-\d{7}-\d\b")
        self._cnic_bare_re = re.compile(r"\b\d{13}\b")
        self._iban_re = re.compile(r"\bPK\d{2}[A-Z0-9]{4}\d{16}\b")
        self._phone_re = re.compile(
            r"(?<!\d)(?:(?:\+|00)92|0)[\s-]?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)"
        )

    def detect(self, text: str, field: str) -> list[Finding]:
        results: list[Finding] = []
        results.extend(self._detect_email(text, field))
        results.extend(self._detect_cnic(text, field))
        results.extend(self._detect_iban(text, field))
        results.extend(self._detect_phone(text, field))
        return self._dedupe_overlaps(results)

    @staticmethod
    def _dedupe_overlaps(findings: list[Finding]) -> list[Finding]:
        # Longest span first, so within any overlapping cluster the longest
        # one always gets first claim on that stretch of text.
        by_length_desc = sorted(
            findings, key=lambda x: x.span_end - x.span_start, reverse=True
        )
        accepted: list[Finding] = []
        for candidate in by_length_desc:
            overlaps_accepted = any(
                candidate.span_start < a.span_end and a.span_start < candidate.span_end
                for a in accepted
            )
            if not overlaps_accepted:
                accepted.append(candidate)
        return sorted(accepted, key=lambda x: x.span_start)

    def _detect_email(self, text: str, field: str) -> list[Finding]:
        results: list[Finding] = []
        for match in self._email_candidate_re.finditer(text):
            candidate = match.group()
            start, end = match.start(), match.end()

            # trim leading junk, push start forward by however much we trimmed
            trimmed = candidate.lstrip(self._LEADING_PUNCT)
            start += len(candidate) - len(trimmed)
            candidate = trimmed

            # trim trailing junk, pull end back to match
            trimmed = candidate.rstrip(self._TRAILING_PUNCT)
            end = start + len(trimmed)
            candidate = trimmed

            try:
                validate_email(candidate, check_deliverability=False)
            except EmailNotValidError:
                continue

            results.append(
                Finding(
                    entity_type="EMAIL",
                    field=field,
                    span_start=start,
                    span_end=end,
                    tier=Tier.RULES,
                    confidence=1.0,
                )
            )
        return results
    def _detect_cnic(self, text: str, field: str) -> list[Finding]:
        results: list[Finding] = []
        for pattern in (self._cnic_canonical_re, self._cnic_bare_re):
            for match in pattern.finditer(text):
                digits = match.group().replace("-", "")
                if digits[0] not in "1234567":
                    continue
                results.append(
                    Finding(
                        entity_type="CNIC",
                        field=field,
                        span_start=match.start(),
                        span_end=match.end(),
                        tier=Tier.RULES,
                        confidence=1.0,
                    )
                )
        return results

    def _detect_iban(self, text: str, field: str) -> list[Finding]:
        results: list[Finding] = []
        for match in self._iban_re.finditer(text):
            confidence = 1.0 if self._iban_checksum_ok(match.group()) else 0.5
            results.append(
                Finding(
                    entity_type="IBAN_PK",
                    field=field,
                    span_start=match.start(),
                    span_end=match.end(),
                    tier=Tier.RULES,
                    confidence=confidence,
                )
            )
        return results

    @staticmethod
    def _iban_checksum_ok(candidate: str) -> bool:
        rearranged = candidate[4:] + candidate[:4]
        digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
        return int(digits) % 97 == 1

    def _detect_phone(self, text: str, field: str) -> list[Finding]:
        results: list[Finding] = []
        for match in self._phone_re.finditer(text):
            digits_only = re.sub(r"\D", "", match.group())

            if digits_only.startswith("0092"):
                digits_only = digits_only[4:]
            elif digits_only.startswith("92"):
                digits_only = digits_only[2:]
            elif digits_only.startswith("0"):
                digits_only = digits_only[1:]

            if len(digits_only) != 10 or digits_only[0] != "3":
                continue

            results.append(
                Finding(
                    entity_type="PHONE_PK",
                    field=field,
                    span_start=match.start(),
                    span_end=match.end(),
                    tier=Tier.RULES,
                    confidence=1.0,
                )
            )
        return results

    # ---------------------------------------------------------------------------
# Acceptance checks — make these pass, then we wire the processor to it.
# Run:  python -m pipelineguard.detectors.tier1_rules
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    d = RulesDetector()

    def types(text: str) -> list[str]:
        return sorted(f.entity_type for f in d.detect(text, "memo"))

    assert types("CNIC 35202-1234567-1 on file") == ["CNIC"]
    assert types("id 3520212345671 bare") == ["CNIC"]
    assert types("id 9520212345671 bad province") == []          # validation, not just regex
    assert types("send to PK36MEZN0001234567891234") == ["IBAN_PK"]
    assert types("call 03001234567 or +92 300 1234567") == ["PHONE_PK", "PHONE_PK"]
    assert types("mail affan@example.com") == ["EMAIL"]
    spans = d.detect("x 35202-1234567-1 y", "memo")
    assert spans[0].span_start == 2 and spans[0].span_end == 17   # spans index original text
    print("tier1 acceptance checks: all passed")
