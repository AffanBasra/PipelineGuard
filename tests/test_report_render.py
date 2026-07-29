"""Tests for the report's pure layer: rendering `ReportData` as Markdown.

No database and no fake connection. `render()` is a pure function over plain
data, so these tests construct the data directly. Whether the SQL produces
that data correctly is a different question, answered by
tests/integration/test_report_sql.py against a real Postgres -- handing canned
rows to a fake cursor here would only have re-asserted the fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipelineguard import report
from pipelineguard.report import EntityRow, ReportData, ReviewItem

T0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def make_data(**overrides) -> ReportData:
    """A populated report, overridable per test."""
    defaults = dict(
        since=T0,
        until=T0 + timedelta(hours=1),
        generated_at=T0 + timedelta(hours=2),
        records=1000,
        event_ts_min=T0,
        event_ts_max=T0 + timedelta(minutes=50),
        processed_ts_min=T0,
        processed_ts_max=T0 + timedelta(seconds=100),
        by_topic=[("txn.raw", 1000)],
        disposition=[("clean", 100), ("quarantined", 40), ("redacted", 860)],
        by_tier=[(0, 100), (1, 900)],
        entities=[
            EntityRow("IBAN_PK", 2000, 1000, 1, 1, 0.5),
            EntityRow("CNIC", 900, 900, 1, 1, 1.0),
        ],
        entity_fields=[("CNIC", "cnic", 900), ("IBAN_PK", "iban_from", 1000)],
        failures=[("ValueError", 3)],
        failclosed=[ReviewItem("id-1", T0, "ValueError", "bad shape")],
        failclosed_total=3,
        uncertain=[ReviewItem("id-2", T0, "sub-threshold confidence", "IBAN_PK (lowest confidence 0.50)")],
        uncertain_total=37,
    )
    defaults.update(overrides)
    return ReportData(**defaults)


def test_all_nine_sections_render() -> None:
    out = report.render(make_data())
    for heading in [
        "## 1. Scope of scan",
        "## 2. Disposition",
        "## 3. Detection tiers",
        "## 4. Personal data observed",
        "## 5. Where it was found",
        "## 6. Items requiring review",
        "## 7. Processing failures",
        "## 8. System properties",
        "## 9. Limitations",
    ]:
        assert heading in out


def test_disclaimer_is_present() -> None:
    """The report must never read as a compliance determination."""
    out = report.render(make_data())
    assert "not a compliance determination" in out
    assert "not legal advice" in out


def test_classified_entities_carry_their_category_and_basis() -> None:
    out = report.render(make_data())
    assert "National identity number" in out
    assert "Financial account identifier" in out
    assert "PECA 2016" in out
    assert "State Bank of Pakistan" in out


def test_unclassified_entity_types_are_surfaced_not_dropped() -> None:
    """The failure mode this guards against: a reader cannot distinguish
    'no personal data of that kind' from 'found it, did not understand it'."""
    data = make_data(entities=[EntityRow("MYSTERY_ID", 5, 5, 1, 1, 1.0)])
    out = report.render(data)
    assert "### Unclassified entity types" in out
    assert "`MYSTERY_ID`" in out
    assert "**unclassified**" in out


def test_no_unclassified_section_when_everything_is_known() -> None:
    assert "### Unclassified entity types" not in report.render(make_data())


def test_quarantine_splits_into_two_worklists_with_totals() -> None:
    out = report.render(make_data())
    assert "### 6.1 Failed closed -- engineering" in out
    assert "### 6.2 Uncertain -- human decision" in out
    assert "3 failed closed and 37 were withheld as uncertain" in out


def test_truncated_queues_say_so() -> None:
    """Listing 1 of 37 without saying so would understate the review backlog."""
    out = report.render(make_data())
    assert "*Showing 1 of 3.*" in out
    assert "*Showing 1 of 37.*" in out


def test_untruncated_queue_has_no_showing_note() -> None:
    data = make_data(failclosed_total=1, uncertain_total=1)
    assert "Showing" not in report.render(data)


def test_empty_period_renders_without_crashing() -> None:
    """A report over a window with no activity is a normal request, and must
    not divide by zero computing shares or throughput."""
    data = make_data(
        records=0,
        event_ts_min=None,
        event_ts_max=None,
        processed_ts_min=None,
        processed_ts_max=None,
        by_topic=[],
        disposition=[],
        by_tier=[],
        entities=[],
        entity_fields=[],
        failures=[],
        failclosed=[],
        failclosed_total=0,
        uncertain=[],
        uncertain_total=0,
    )
    out = report.render(data)
    assert "No records were processed in this period." in out
    assert "No personal data was detected in this period." in out
    assert "None in this period." in out


def test_zero_length_window_does_not_divide_by_zero() -> None:
    """Every record landing within the same second is the realistic case for a
    small benchmark run, and it makes the elapsed span exactly zero."""
    data = make_data(processed_ts_min=T0, processed_ts_max=T0)
    assert "1,000 records within one second" in report.render(data)


def test_throughput_is_reported_as_observed_not_as_capacity() -> None:
    out = report.render(make_data())
    assert "10 records/second observed" in out


def test_unbounded_window_is_labelled_honestly() -> None:
    out = report.render(make_data(since=None, until=None))
    assert "all recorded activity (no window specified)" in out


def test_half_open_window_label_names_both_ends() -> None:
    out = report.render(make_data(since=T0, until=None))
    assert "2026-07-01 09:00:00Z to latest record" in out


@pytest.mark.parametrize(
    "types, confidence, expected",
    [
        (None, None, "no findings recorded"),
        ("", None, "no findings recorded"),
        ("CNIC", None, "CNIC"),
        ("CNIC, EMAIL", 0.5, "CNIC, EMAIL (lowest confidence 0.50)"),
    ],
)
def test_uncertain_detail_handles_the_left_join_nulls(types, confidence, expected) -> None:
    """A message quarantined as uncertain may have no findings at all; the
    LEFT JOIN yields NULL for both columns there. Rendering that as 'None'
    would put a Python repr in a compliance document."""
    assert report._uncertain_detail(types, confidence) == expected


def test_render_is_pure() -> None:
    data = make_data()
    assert report.render(data) == report.render(data)


def test_detection_counts_are_not_claimed_to_be_people() -> None:
    """The distinction a compliance reader will otherwise get wrong."""
    out = report.render(make_data())
    assert "not of distinct individuals" in out


def test_limitations_state_what_the_report_cannot_show() -> None:
    out = report.render(make_data())
    assert "describes what entered the pipeline, not what existed" in out
    assert "Detection is not exhaustive" in out


# --------------------------------------------------------------------------- #
# CLI wiring
#
# main() builds its own connection, so these patch psycopg.connect and
# report.fetch — the SQL underneath is covered by the integration tests, and
# re-asserting it here through a double would only restate the fixtures.
# --------------------------------------------------------------------------- #
class FakeConnection:
    """Records that it was closed. main() must close the connection even when
    fetch raises, or a failing report leaks a Postgres session per run."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def cli(monkeypatch):
    import psycopg

    conn = FakeConnection()
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    return conn


def test_main_writes_markdown_to_stdout(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(report, "fetch", lambda *a, **k: make_data())
    assert report.main([]) == 0
    assert "# Data Governance Report" in capsys.readouterr().out


def test_main_writes_to_file_when_asked(cli, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(report, "fetch", lambda *a, **k: make_data())
    target = tmp_path / "report.md"
    assert report.main(["--output", str(target)]) == 0
    assert "# Data Governance Report" in target.read_text(encoding="utf-8")


def test_main_closes_the_connection_even_when_fetch_raises(cli, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("query failed")

    monkeypatch.setattr(report, "fetch", boom)
    with pytest.raises(RuntimeError):
        report.main([])
    assert cli.closed, "connection leaked on the failure path"


def test_main_passes_window_and_cap_through(cli, monkeypatch) -> None:
    seen = {}

    def spy(conn, since, until, max_queue):
        seen.update(since=since, until=until, max_queue=max_queue)
        return make_data()

    monkeypatch.setattr(report, "fetch", spy)
    report.main(["--since", "2026-07-01", "--until", "2026-07-02T12:00:00Z", "--max-queue", "7"])

    assert seen["since"] == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert seen["until"] == datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    assert seen["max_queue"] == 7


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-07-01", datetime(2026, 7, 1, tzinfo=timezone.utc)),
        ("2026-07-01T06:30:00Z", datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)),
        ("2026-07-01T06:30:00+05:00", datetime(2026, 7, 1, 6, 30, tzinfo=timezone(timedelta(hours=5)))),
    ],
)
def test_parse_ts_defaults_naive_input_to_utc(text, expected) -> None:
    """A naive --since would otherwise be compared against timestamptz using
    the server's timezone, silently shifting the window."""
    assert report._parse_ts(text) == expected