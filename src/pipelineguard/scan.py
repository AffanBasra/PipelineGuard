"""Scan free text with the shipped detectors and report what was redacted.

The detectors and the rewrite are the pipeline's own -- `RulesDetector`,
`Tier2Detector` and `processor.redact()` -- so a `ScanResult` describes what a
memo carrying this text would look like on txn.clean. Nothing here
re-implements detection or merging; a second copy of span arithmetic would
drift, and the drift would show as the UI claiming a redaction the pipeline
does not perform.

This module holds every part of the UI that can be asserted on. `ui.py` is
presentation only.
"""
from __future__ import annotations

import csv
import html
import io
import threading
from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from pipelineguard.models import Finding
from pipelineguard.processor import merge_spans, redact

# The field name is load-bearing: "memo" is in schema_rules.FREE_TEXT, which is
# what marks a value as free text the encoder should read.
DEFAULT_FIELD = "memo"

# GLiNER truncates at max_len tokens and says nothing, so text past the limit
# would be silently unscanned -- the worst failure this tool can have. We cut
# first and say so instead. See docs/decisions.md and README.
MAX_CHARS = 1_000

# Emerald, from the UI palette. Kept here rather than in ui.py because
# highlight_html writes the inline style, and a strict CSP leaves no stylesheet.
HIGHLIGHT = "#10B981"


@dataclass(frozen=True)
class ScanResult:
    """One scanned text: what was found, what the rewrite produced, how much of
    the identifying material it covered."""

    text: str
    findings: tuple[Finding, ...]
    spans: tuple[tuple[int, int, str], ...]
    redacted: str
    masked_chars: int
    identifying_chars: int
    truncated: bool = False

    @property
    def coverage(self) -> float:
        """Share of identifying characters inside a redacted span, 0.0-1.0.

        Alphanumeric only: punctuation and spaces carry no identity, so
        counting them would flatter every result.
        """
        if not self.identifying_chars:
            return 0.0
        return self.masked_chars / self.identifying_chars


_TIER2_LOCK = threading.Lock()


@contextmanager
def tier2_settings(detector, *, threshold: float | None = None,
                   extend_addresses: bool | None = None):
    """Apply the sidebar's encoder settings for one call, then put them back.

    Streamlit shares one cached detector across sessions and threads, and both
    settings are instance attributes read at call time -- so set-and-use must be
    atomic or one session reads another's controls.

    They are set on the detector rather than filtered afterwards because both
    change detection itself: address spans are widened and bridged during
    detection, and bridging keeps the highest score of the spans it joins. A
    span dropped by a later filter can still have pulled a bridge together, so
    filtering after the fact would report a span the pipeline never produces.
    """
    overrides: dict[str, object] = {}
    if threshold is not None:
        overrides["threshold"] = threshold
    if extend_addresses is not None:
        overrides["extend_addresses"] = extend_addresses
    if not overrides:
        yield detector
        return
    with _TIER2_LOCK:
        previous = {name: getattr(detector, name) for name in overrides}
        for name, value in overrides.items():
            setattr(detector, name, value)
        try:
            yield detector
        finally:
            for name, value in previous.items():
                setattr(detector, name, value)


def _keep(findings: Iterable[Finding],
          entity_types: Collection[str] | None) -> list[Finding]:
    """Drop findings the caller did not ask for, then sort by position.

    Filtering after detection is exact for entity types: the encoder runs one
    independent pass per label group, and only ADDRESS spans are widened or
    bridged, so removing a type cannot change another type's spans.
    """
    kept = [f for f in findings
            if entity_types is None or f.entity_type in entity_types]
    kept.sort(key=lambda f: (f.span_start, f.span_end))
    return kept


def _result(text: str, findings: list[Finding], truncated: bool) -> ScanResult:
    spans = merge_spans(findings)
    masked = sum(1 for s, e, _ in spans for i in range(s, e) if text[i].isalnum())
    return ScanResult(
        text=text,
        findings=tuple(findings),
        spans=tuple(spans),
        redacted=redact(text, findings),
        masked_chars=masked,
        identifying_chars=sum(1 for c in text if c.isalnum()),
        truncated=truncated,
    )


def clip(text: str, max_chars: int = MAX_CHARS) -> tuple[str, bool]:
    """Cut text to what the encoder can actually read. Returns (text, cut?)."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def scan_text(
    text: str,
    *,
    tier1,
    tier2=None,
    field: str = DEFAULT_FIELD,
    entity_types: Collection[str] | None = None,
    threshold: float | None = None,
    extend_addresses: bool | None = None,
    max_chars: int = MAX_CHARS,
) -> ScanResult:
    """Run the detectors over one text and apply the pipeline's rewrite."""
    text, truncated = clip(text, max_chars)
    findings = list(tier1.detect(text, field))
    if tier2 is not None:
        with tier2_settings(tier2, threshold=threshold,
                            extend_addresses=extend_addresses) as detector:
            findings += detector.detect(text, field)
    return _result(text, _keep(findings, entity_types), truncated)


def scan_batch(
    texts: Sequence[str],
    *,
    tier1,
    tier2=None,
    field: str = DEFAULT_FIELD,
    entity_types: Collection[str] | None = None,
    threshold: float | None = None,
    extend_addresses: bool | None = None,
    max_chars: int = MAX_CHARS,
    progress: Callable[[int, int], None] | None = None,
) -> list[ScanResult]:
    """Scan many texts, reporting progress as each chunk completes.

    Chunked here rather than left to `detect_batch` because that method offers
    no callback and a 500-row scan with no progress bar reads as a hang. The
    chunk boundaries match its own only because there is exactly one field per
    row; scanning two columns per row would break that equivalence.
    """
    clipped = [clip(t, max_chars) for t in texts]
    total = len(clipped)
    results: list[ScanResult] = []

    if tier2 is None:
        for text, truncated in clipped:
            findings = _keep(tier1.detect(text, field), entity_types)
            results.append(_result(text, findings, truncated))
            if progress:
                progress(len(results), total)
        return results

    size = max(1, getattr(tier2, "batch_size", 8))
    with tier2_settings(tier2, threshold=threshold,
                        extend_addresses=extend_addresses) as detector:
        for start in range(0, total, size):
            chunk = clipped[start:start + size]
            # detect_batch omits keys with no findings, so results are read back
            # by key. Zipping would shift every later row onto the wrong text.
            found = detector.detect_batch(
                {start + i: {field: text} for i, (text, _) in enumerate(chunk)}
            )
            for i, (text, truncated) in enumerate(chunk):
                findings = list(tier1.detect(text, field))
                findings += found.get(start + i, {}).get(field, [])
                results.append(
                    _result(text, _keep(findings, entity_types), truncated)
                )
            if progress:
                progress(len(results), total)
    return results


def highlight_html(text: str, spans: Sequence[tuple[int, int, str]]) -> str:
    """Wrap each redacted span in a <mark>, escaping everything else.

    Escaping is not decoration: Streamlit renders text as Markdown, so an
    unescaped `*` or backtick in a memo shifts the very span being pointed at.
    It also closes the injection hole on uploaded files.
    """
    out: list[str] = []
    cursor = 0
    for start, end, label in spans:
        out.append(html.escape(text[cursor:start]))
        out.append(
            f'<mark title="{html.escape(label, quote=True)}" '
            f'style="background:{HIGHLIGHT}33;color:#F8FAFC;'
            f'border-bottom:2px solid {HIGHLIGHT};'
            'padding:1px 2px;border-radius:3px">'
            f"{html.escape(text[start:end])}</mark>"
        )
        cursor = end
    out.append(html.escape(text[cursor:]))
    return (
        '<div style="white-space:pre-wrap;word-break:break-word;'
        'font-family:ui-monospace,SFMono-Regular,Consolas,monospace;'
        'line-height:1.7">' + "".join(out) + "</div>"
    )


def _decode(data: bytes) -> str:
    """UTF-8, falling back to cp1252 so an Excel export still opens."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def csv_columns(data: bytes) -> list[str]:
    """Header names, for the column picker. Empty if the file has no header."""
    reader = csv.reader(io.StringIO(_decode(data), newline=""))
    return [h.strip() for h in next(reader, [])]


def widest_column(data: bytes, sample: int = 50) -> str | None:
    """The column carrying the most text per row, or None if there are no rows.

    A better default than "first column", which in practice is an id: scanning
    it finds nothing, and an empty first result reads as a broken detector
    rather than as the wrong column.
    """
    names = csv_columns(data)
    if not names:
        return None
    reader = csv.DictReader(io.StringIO(_decode(data), newline=""))
    raw = reader.fieldnames or []
    totals = dict.fromkeys(names, 0)
    rows = 0
    for row in reader:
        for name, key in zip(names, raw):
            totals[name] += len(str(row.get(key) or ""))
        rows += 1
        if rows >= sample:
            break
    if not rows:
        return names[0]
    # Ties go to the leftmost column, which max() already does.
    return max(names, key=lambda name: totals[name])


def read_rows(data: bytes, filename: str,
              column: str | None = None) -> list[str]:
    """One text per row from an uploaded .csv or .txt.

    Blank rows are dropped rather than scanned: they produce a finding-free
    result that would dilute every batch percentage.
    """
    if filename.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(_decode(data), newline=""))
        names = [h.strip() for h in (reader.fieldnames or [])]
        if not names:
            return []
        chosen = column or names[0]
        if chosen not in names:
            raise ValueError(
                f"no column {chosen!r}; the file has {', '.join(names)}"
            )
        # DictReader keys come from the raw header, which may carry the spaces
        # we stripped when showing the picker.
        key = (reader.fieldnames or [])[names.index(chosen)]
        values = [(row.get(key) or "") for row in reader]
    else:
        values = _decode(data).splitlines()
    return [v.strip() for v in values if v and v.strip()]
