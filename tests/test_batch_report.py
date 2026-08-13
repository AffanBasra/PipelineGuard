"""Tests for building the governance report from an in-memory batch.

Pure: no database, no model, no PDF. `report.render()` is already tested
against its own data, so what matters here is whether the data handed to it
describes the scan truthfully -- especially the two places a file scan can
overstate itself, its source and its redacted rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipelineguard import batch_report, report, scan
from pipelineguard.detectors.tier1_rules import RulesDetector
from pipelineguard.models import Finding, Tier

T0 = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=8)

CNIC = "35202-1234567-1"


def finding(entity_type: str, tier: Tier = Tier.RULES, confidence: float = 1.0,
            start: int = 0, end: int = 4, field: str = "memo") -> Finding:
    return Finding(entity_type=entity_type, field=field, span_start=start,
                   span_end=end, tier=tier, confidence=confidence)


def result(*findings: Finding, text: str = "some memo text",
           truncated: bool = False) -> scan.ScanResult:
    """A ScanResult with the given findings. Span arithmetic is not the subject
    of these tests, so the redacted form is a placeholder."""
    return scan.ScanResult(
        text=text,
        findings=findings,
        spans=(),
        redacted="[REDACTED] memo" if findings else text,
        masked_chars=4 if findings else 0,
        identifying_chars=12,
        truncated=truncated,
    )


def build(*results, **kwargs) -> report.ReportData:
    kwargs.setdefault("source_name", "memos.csv")
    kwargs.setdefault("started_at", T0)
    kwargs.setdefault("finished_at", T1)
    kwargs.setdefault("generated_at", T1)
    return batch_report.build_report_data(list(results), **kwargs)


# --------------------------------------------------------------------------- #
# disposition
# --------------------------------------------------------------------------- #
def test_disposition_counts_clean_redacted_and_quarantined():
    data = build(
        result(),
        result(finding("CNIC")),
        result(finding("IBAN_PK", confidence=0.5)),
    )
    assert data.disposition == [("clean", 1), ("quarantined", 1), ("redacted", 1)]


def test_quarantine_ignores_tier2_confidence():
    """Only a sub-1.0 RULE finding quarantines. An encoder scoring 0.30 is
    still just a redaction -- copying the predicate loosely would flood the
    review queue with every uncertain name the model ever saw."""
    data = build(result(finding("PERSON_NAME", tier=Tier.ENCODER, confidence=0.30)))
    assert data.disposition == [("redacted", 1)]
    assert data.uncertain_total == 0


def test_zero_count_dispositions_are_omitted():
    """SQL GROUP BY never returns a zero row, so neither may this."""
    data = build(result(finding("CNIC")))
    assert data.disposition == [("redacted", 1)]


def test_empty_batch_has_no_timestamps():
    data = build()
    assert data.records == 0
    assert data.processed_ts_min is None and data.processed_ts_max is None


# --------------------------------------------------------------------------- #
# entities
# --------------------------------------------------------------------------- #
def test_mentions_count_findings_and_messages_count_rows():
    """Two CNICs in one row and one in another is 3 mentions across 2 rows.
    Conflating them would misreport how widely a type appears."""
    data = build(
        result(finding("CNIC", start=0, end=4), finding("CNIC", start=5, end=9)),
        result(finding("CNIC")),
    )
    (row,) = data.entities
    assert (row.mentions, row.messages) == (3, 2)


def test_entities_ordered_by_mentions_then_name():
    data = build(
        result(finding("CNIC"), finding("CNIC", start=5, end=9)),
        result(finding("EMAIL"), finding("ADDRESS", tier=Tier.ENCODER,
                                         confidence=0.7)),
    )
    assert [e.entity_type for e in data.entities] == ["CNIC", "ADDRESS", "EMAIL"]


def test_entity_row_carries_tier_range_and_lowest_confidence():
    data = build(
        result(finding("PERSON_NAME")),
        result(finding("PERSON_NAME", tier=Tier.ENCODER, confidence=0.61)),
    )
    (row,) = data.entities
    assert (row.min_tier, row.max_tier) == (1, 2)
    assert row.min_confidence == pytest.approx(0.61)


def test_entity_fields_use_the_field_the_finding_carried():
    data = build(result(finding("CNIC", field="memo")))
    assert data.entity_fields == [("CNIC", "memo", 1)]


# --------------------------------------------------------------------------- #
# tiers, topics, queues
# --------------------------------------------------------------------------- #
def test_by_tier_uses_the_highest_tier_per_row_and_zero_for_clean():
    data = build(
        result(),
        result(finding("CNIC")),
        result(finding("CNIC"), finding("PERSON_NAME", tier=Tier.ENCODER,
                                        confidence=0.8)),
    )
    assert data.by_tier == [(0, 1), (1, 1), (2, 1)]


def test_no_topic_and_no_event_times():
    """A CSV row has neither. Empty renders as absent; a guess would not."""
    data = build(result(finding("CNIC")))
    assert data.by_topic == []
    assert data.event_ts_min is None and data.event_ts_max is None


def test_failure_queues_are_empty_because_there_is_no_fail_closed_path():
    data = build(result(finding("IBAN_PK", confidence=0.5)))
    assert data.failures == [] and data.failclosed == []
    assert data.failclosed_total == 0


def test_uncertain_item_names_the_row_and_the_trigger():
    data = build(result(), result(finding("IBAN_PK", confidence=0.5)))
    (item,) = data.uncertain
    assert item.message_id == "row 1"
    assert item.reason == "sub-threshold confidence"
    assert "IBAN_PK" in item.detail and "0.50" in item.detail


def test_uncertain_is_capped_but_the_total_is_not():
    rows = [result(finding("IBAN_PK", confidence=0.5)) for _ in range(7)]
    data = build(*rows, max_queue=3)
    assert len(data.uncertain) == 3
    assert data.uncertain_total == 7


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def test_render_accepts_the_built_data():
    out = report.render(build(result(finding("CNIC"))))
    for heading in [f"## {n}." for n in range(1, 10)]:
        assert heading in out


def test_report_names_the_uploaded_file_not_the_audit_trail():
    """The figures came from a file scan. Claiming the audit trail produced
    them would be the same overstatement as calling the output 'audit cleared'."""
    out = report.render(build(result(finding("CNIC")), source_name="memos.csv"))
    assert "uploaded file `memos.csv`" in out
    assert "audit trail (`messages_processed`" not in out


def markdown(*results, **kwargs) -> str:
    source_name = kwargs.pop("source_name", "memos.csv")
    data = build(*results, source_name=source_name,
                 **{k: v for k, v in kwargs.items() if k == "max_queue"})
    return batch_report.build_markdown(
        data, list(results), source_name=source_name,
        tier2_used=kwargs.get("tier2_used", True),
        threshold=kwargs.get("threshold", 0.55),
        entity_types=kwargs.get("entity_types"),
        include_rows=kwargs.get("include_rows", True),
    )


def test_markdown_reports_the_run_settings():
    out = markdown(result(finding("CNIC")), threshold=0.55)
    assert "## 10. About this scan" in out
    assert "`memos.csv`" in out and "0.55" in out


def test_markdown_warns_about_incomplete_address_redaction():
    out = markdown(result(finding("ADDRESS", tier=Tier.ENCODER, confidence=0.8)))
    assert "## 11. Redacted rows" in out
    assert "routinely incomplete" in out


def test_markdown_counts_truncated_rows():
    out = markdown(result(finding("CNIC"), truncated=True))
    assert "were cut before scanning" in out


def test_markdown_omits_the_truncation_warning_when_nothing_was_cut():
    assert "were cut before scanning" not in markdown(result(finding("CNIC")))


def test_markdown_never_contains_an_original_pii_value():
    """The batch export carries redacted output only. A sentinel that survives
    into the report means the original text leaked into a shareable file."""
    tier1 = RulesDetector()
    scanned = scan.scan_text(f"Sent by {CNIC} today", tier1=tier1)
    data = build(scanned, source_name="memos.csv")
    out = batch_report.build_markdown(data, [scanned], source_name="memos.csv",
                                      tier2_used=False)
    assert CNIC not in out
    assert "[CNIC]" in out


def test_markdown_can_omit_the_rows_entirely():
    out = markdown(result(finding("CNIC")), include_rows=False)
    assert "## 11. Redacted rows" not in out


def test_markdown_escapes_pipes_so_the_table_survives():
    scanned = scan.ScanResult(text="a|b", findings=(), spans=(),
                              redacted="a|b", masked_chars=0,
                              identifying_chars=2)
    data = build(scanned)
    out = batch_report.build_markdown(data, [scanned], source_name="x.csv",
                                      tier2_used=False)
    assert "a\\|b" in out


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def test_markdown_to_pdf_produces_a_pdf():
    pytest.importorskip("fpdf")
    pytest.importorskip("markdown_it")
    out = batch_report.markdown_to_pdf(
        markdown(result(finding("CNIC"))), title="Redaction Report"
    )
    assert out.startswith(b"%PDF-")
    assert len(out) > 1000
