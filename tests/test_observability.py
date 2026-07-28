"""Stats reporting.

Worth testing because these numbers end up in the README benchmark table. A
percentile helper that is quietly off by one turns into a published claim.
"""
from __future__ import annotations

import logging

import pytest

from pipelineguard.observability import StatsReporter, percentile


@pytest.mark.parametrize(
    "p, expected",
    # Index-based, no interpolation: idx = round(p/100 * (n-1)).
    # p50 lands on 51 rather than 50 because round(49.5) uses banker's
    # rounding to 50, and ordered[50] is the 51st value.
    [(0, 1), (50, 51), (95, 95), (99, 99), (100, 100)],
)
def test_percentile_of_a_known_distribution(p, expected):
    assert percentile(list(range(1, 101)), p) == expected


def test_percentile_of_an_empty_window_is_zero_not_an_error():
    """The processor reports stats before any message arrives."""
    assert percentile([], 50) == 0.0


def test_percentile_of_a_single_value():
    assert percentile([7.0], 99) == 7.0


def test_percentile_does_not_mutate_its_input():
    values = [3.0, 1.0, 2.0]
    percentile(values, 50)
    assert values == [3.0, 1.0, 2.0]


@pytest.fixture
def reporter():
    return StatsReporter(logging.getLogger("test"), interval_s=1e9)


def test_counts_are_grouped_by_action(reporter, caplog):
    for action in ["clean", "redacted", "redacted", "quarantined"]:
        reporter.record(action, 1.0)
    with caplog.at_level(logging.INFO, logger="test"):
        reporter.report(final=True)
    line = caplog.text
    assert "processed=4" in line
    assert "clean=1" in line and "redacted=2" in line and "quarantined=1" in line


def test_failures_are_counted_separately_from_quarantines(reporter, caplog):
    """Every failure quarantines, but not every quarantine is a failure —
    an uncertain detection quarantines without anything going wrong."""
    reporter.record("quarantined", 1.0, failed=True)
    reporter.record("quarantined", 1.0, failed=False)
    with caplog.at_level(logging.INFO, logger="test"):
        reporter.report(final=True)
    assert "quarantined=2" in caplog.text
    assert "failed=1" in caplog.text


def test_reporting_with_no_messages_does_not_divide_by_zero(reporter, caplog):
    with caplog.at_level(logging.INFO, logger="test"):
        reporter.report(final=True)
    assert "no messages processed" in caplog.text


def test_latency_window_is_bounded(caplog):
    """Memory must stay flat on a stream that never ends."""
    reporter = StatsReporter(logging.getLogger("test"), interval_s=1e9, window=100)
    for i in range(1000):
        reporter.record("clean", float(i))
    assert len(reporter._latencies) == 100
    with caplog.at_level(logging.INFO, logger="test"):
        reporter.report(final=True)
    assert "processed=1,000" in caplog.text   # total is lifetime, not windowed


def test_maybe_report_respects_the_interval(caplog):
    reporter = StatsReporter(logging.getLogger("test"), interval_s=1e9)
    reporter.record("clean", 1.0)
    with caplog.at_level(logging.INFO, logger="test"):
        reporter.maybe_report()
    assert caplog.text == ""


def test_maybe_report_emits_once_the_interval_has_passed(caplog):
    reporter = StatsReporter(logging.getLogger("test"), interval_s=0)
    reporter.record("clean", 1.0)
    with caplog.at_level(logging.INFO, logger="test"):
        reporter.maybe_report()
    assert "processed=1" in caplog.text
