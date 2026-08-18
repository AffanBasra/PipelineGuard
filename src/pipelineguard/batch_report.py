"""Turn a batch of `ScanResult`s into the shipped governance report.

The report is not rewritten here. `report.render()` is a pure function over
`ReportData`, so this module's whole job is to build that data from an
in-memory scan instead of from Postgres, and to append what only a file scan
can say. A second report layout would drift from the first and would have to
re-argue every regulatory classification in `compliance.py`.

Two figures have no honest source in a file scan and are left empty rather than
invented: event timestamps (a CSV row has no event time) and the source topic.
`ReportData` renders both as absent.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Sequence

from pipelineguard import report
from pipelineguard.models import Finding, Tier
from pipelineguard.scan import ScanResult

# Redacted output is still text a reader can scan, and ADDRESS redaction is
# documented as routinely incomplete rather than binary (compliance.py, and
# docs/tier2-detection-findings.md sections 17-18 and 23).
_ADDRESS_CAVEAT = (
    "Rows below are the **redacted** output, not the input. Address redaction "
    "is the one entity type in this pipeline that is routinely incomplete "
    "rather than binary, so a residual fragment of an address may remain in a "
    "row that is otherwise masked. Treat this section as reviewable output, "
    "not as cleared data."
)


def disposition_of(findings: Iterable[Finding]) -> str:
    """What the processor would have done with this record.

    Mirrors the routing predicate in processor.py: only a rule finding below
    full confidence quarantines. Encoder scores never do, however low.
    """
    findings = list(findings)
    if any(f.tier == Tier.RULES and f.confidence < 1.0 for f in findings):
        return "quarantined"
    return "clean" if not findings else "redacted"


def _quarantine_triggers(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.tier == Tier.RULES and f.confidence < 1.0]


def _entities(results: Sequence[ScanResult]) -> list[report.EntityRow]:
    """One row per entity type: total mentions, and how many rows carried it."""
    mentions: Counter[str] = Counter()
    rows: Counter[str] = Counter()
    tiers: dict[str, list[int]] = {}
    confidences: dict[str, list[float]] = {}

    for result in results:
        seen: set[str] = set()
        for finding in result.findings:
            mentions[finding.entity_type] += 1
            seen.add(finding.entity_type)
            tiers.setdefault(finding.entity_type, []).append(int(finding.tier))
            confidences.setdefault(finding.entity_type, []).append(finding.confidence)
        for entity_type in seen:
            rows[entity_type] += 1

    return sorted(
        (
            report.EntityRow(
                entity_type=entity_type,
                mentions=count,
                messages=rows[entity_type],
                min_tier=min(tiers[entity_type]),
                max_tier=max(tiers[entity_type]),
                min_confidence=min(confidences[entity_type]),
            )
            for entity_type, count in mentions.items()
        ),
        key=lambda e: (-e.mentions, e.entity_type),
    )


def _entity_fields(results: Sequence[ScanResult]) -> list[tuple[str, str, int]]:
    counts: Counter[tuple[str, str]] = Counter()
    for result in results:
        for finding in result.findings:
            counts[(finding.entity_type, finding.field)] += 1
    return sorted(
        ((etype, field, n) for (etype, field), n in counts.items()),
        key=lambda row: (row[0], -row[2], row[1]),
    )


def _uncertain(results: Sequence[ScanResult], when: datetime,
               max_queue: int) -> tuple[list[report.ReviewItem], int]:
    """The review queue, keyed by row number.

    A CSV row has no message id and inventing a UUID would be worse than
    admitting it, so the queue says `row 12` and the reader can find it.
    """
    items: list[report.ReviewItem] = []
    total = 0
    for index, result in enumerate(results):
        if disposition_of(result.findings) != "quarantined":
            continue
        total += 1
        if len(items) >= max_queue:
            continue
        triggers = _quarantine_triggers(result.findings)
        items.append(
            report.ReviewItem(
                message_id=f"row {index}",
                processed_ts=when,
                reason="sub-threshold confidence",
                # Reused rather than re-derived: the wording is the report's,
                # and two copies of it would drift.
                detail=report._uncertain_detail(
                    ", ".join(sorted({f.entity_type for f in triggers})),
                    min((f.confidence for f in triggers), default=None),
                ),
            )
        )
    return items, total


def build_report_data(
    results: Sequence[ScanResult],
    *,
    source_name: str,
    started_at: datetime,
    finished_at: datetime,
    generated_at: datetime | None = None,
    max_queue: int = 50,
) -> report.ReportData:
    """Pack a finished batch into the shape `report.render()` consumes."""
    dispositions = Counter(disposition_of(r.findings) for r in results)
    tiers = Counter(
        max((int(f.tier) for f in r.findings), default=0) for r in results
    )
    uncertain, uncertain_total = _uncertain(results, started_at, max_queue)

    return report.ReportData(
        since=started_at,
        until=finished_at,
        generated_at=generated_at or datetime.now(timezone.utc),
        records=len(results),
        # A row in a file has no event time. Absent, not guessed.
        event_ts_min=None,
        event_ts_max=None,
        processed_ts_min=started_at if results else None,
        processed_ts_max=finished_at if results else None,
        by_topic=[],
        disposition=sorted(dispositions.items()),
        by_tier=sorted(tiers.items()),
        entities=_entities(results),
        entity_fields=_entity_fields(results),
        failures=[],
        failclosed=[],
        failclosed_total=0,
        uncertain=uncertain,
        uncertain_total=uncertain_total,
        source=f"uploaded file `{source_name}`, scanned locally in the PipelineGuard UI",
        # No broker, no audit table, no review topic. The renderers drop the
        # passages that describe those rather than assert them about a scan
        # that has none.
        from_stream=False,
    )


def _cell(text: str) -> str:
    """Make a value safe inside a Markdown table row."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def build_markdown(
    data: report.ReportData,
    results: Sequence[ScanResult],
    *,
    source_name: str,
    tier2_used: bool,
    threshold: float | None = None,
    entity_types: Sequence[str] | None = None,
    include_rows: bool = True,
) -> str:
    """The shipped report, plus the two sections only a file scan can add."""
    lines = [report.render(data).rstrip(), "", "## 10. About this scan", ""]

    detectors = "Tier 1 rules" + (" and the Tier 2 encoder" if tier2_used else " only")
    lines += report._table(
        ["setting", "value"],
        [
            ["Source file", f"`{_cell(source_name)}`"],
            ["Rows scanned", f"{len(results):,}"],
            ["Detectors", detectors],
            ["Confidence threshold",
             f"{threshold:.2f}" if tier2_used and threshold is not None else "--"],
            ["Entity types enabled",
             ", ".join(entity_types) if entity_types else "all"],
        ],
    )

    truncated = sum(1 for r in results if r.truncated)
    if truncated:
        lines += [
            "",
            f"**{truncated:,} row(s) were longer than the encoder's input limit "
            "and were cut before scanning.** The text beyond the cut was not "
            "examined, and any personal data in it is neither detected nor "
            "reported here.",
        ]

    identifying = sum(r.identifying_chars for r in results)
    masked = sum(r.masked_chars for r in results)
    if identifying:
        lines += [
            "",
            f"Across the batch, {masked:,} of {identifying:,} identifying "
            f"characters fall inside a redacted span "
            f"({masked / identifying:.1%}).",
            "",
            "That figure counts characters, not records. A record can be "
            "almost entirely masked and still carry the one fragment that "
            "identifies someone, so it measures coverage and not safety.",
        ]
    else:
        lines += ["", "No identifying characters were present in this batch."]

    if include_rows:
        lines += ["", "## 11. Redacted rows", "", f"> {_ADDRESS_CAVEAT}", ""]
        if results:
            lines += report._table(
                ["row", "outcome", "redacted text"],
                [
                    [str(i), disposition_of(r.findings), _cell(r.redacted)]
                    for i, r in enumerate(results)
                ],
            )
        else:
            lines += ["No rows were scanned."]

    return "\n".join(lines).rstrip() + "\n"


def markdown_to_pdf(markdown_text: str, *, title: str,
                    orientation: str = "landscape") -> bytes:
    """Render Markdown to PDF bytes.

    Imported lazily, like the encoder's gliner import, so this module stays
    usable without the `ui` extra installed.

    Splits on `report.PAGE_BREAK` before rendering, so the marker never reaches
    the HTML writer -- fpdf2 has no page-break element to hand it. Portrait is
    available because the six-column entity table is what forced landscape, and
    a summary without it reads better on a normal page.
    """
    from fpdf import FPDF
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark").enable("table")

    pdf = FPDF(orientation=orientation, unit="mm", format="A4")
    pdf.set_title(title)
    pdf.set_auto_page_break(auto=True, margin=12)
    for chunk in markdown_text.split(report.PAGE_BREAK):
        pdf.add_page()
        pdf.set_font("Helvetica", size=8)
        # Latin-1 is all the core fonts cover, and the report can carry a stray
        # non-Latin character from a scanned row. Losing one glyph beats raising.
        pdf.write_html(md.render(chunk).encode("latin-1", "replace").decode("latin-1"),
                       table_line_separators=True)
    return bytes(pdf.output())
