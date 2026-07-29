"""The report's SQL, executed against a real Postgres.

Why these are not unit tests with a fake cursor: the report's logic *is* its
SQL. A fake that returns canned rows asserts that the renderer formats the
fixtures, and leaves every GROUP BY, join and window boundary unexercised
while the suite reports green. Each test below targets a specific way one of
those queries could be wrong and still look right.

Skipped automatically when no database is reachable, so `pytest` stays offline
by default. Run them with a stack up:

    docker compose up -d
    pytest -m integration

Every test runs inside a transaction that is rolled back, so the audit tables
are left exactly as they were found.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pipelineguard import report
from pipelineguard.config import settings

pytestmark = pytest.mark.integration

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    try:
        connection = psycopg.connect(settings.postgres_dsn, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable at {settings.postgres_dsn}: {exc}")

    connection.autocommit = False
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('messages_processed'), to_regclass('findings');")
        if any(name is None for name in cur.fetchone()):
            connection.close()
            pytest.skip("audit schema not present -- run db/init.sql")

    try:
        yield connection
    finally:
        # Nothing this test wrote is committed, so the tables are untouched.
        connection.rollback()
        connection.close()


def add(
    conn,
    *,
    action: str,
    processed_ts: datetime,
    findings: tuple = (),
    failure_class: str | None = None,
    failure_detail: str | None = None,
    source_topic: str = "txn.raw",
    event_ts: datetime | None = None,
) -> str:
    """Insert one audited message and its findings. Returns the message id."""
    message_id = str(uuid.uuid4())
    max_tier = max((f[3] for f in findings), default=0)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages_processed
                (message_id, source_topic, event_ts, processed_ts, max_tier,
                 action, latency_ms, schema_version, failure_class, failure_detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s);
            """,
            (
                message_id,
                source_topic,
                event_ts or processed_ts,
                processed_ts,
                max_tier,
                action,
                0.1,
                failure_class,
                failure_detail,
            ),
        )
        # findings entries are (entity_type, field, confidence, tier)
        for entity_type, field, confidence, tier in findings:
            cur.execute(
                """
                INSERT INTO findings
                    (message_id, entity_type, field, span_start, span_end, tier, confidence, action)
                VALUES (%s, %s, %s, 0, 5, %s, %s, %s);
                """,
                (
                    message_id,
                    entity_type,
                    field,
                    tier,
                    confidence,
                    "quarantined" if action == "quarantined" else "redacted",
                ),
            )
    return message_id


@pytest.fixture
def isolated(conn):
    """Empty the audit tables inside the (rolled back) transaction so counts
    are exact rather than relative to whatever the database already held."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM findings;")
        cur.execute("DELETE FROM messages_processed;")
    return conn


def test_window_is_half_open(isolated) -> None:
    """[since, until). A record exactly at `since` is in; one exactly at
    `until` is out. If the upper bound were inclusive, two reports over
    adjacent windows would each count a boundary record -- a discrepancy that
    only ever surfaces as two reports disagreeing by one.
    """
    add(isolated, action="clean", processed_ts=T0 - timedelta(seconds=1))
    add(isolated, action="clean", processed_ts=T0)
    add(isolated, action="clean", processed_ts=T0 + timedelta(minutes=30))
    add(isolated, action="clean", processed_ts=T0 + timedelta(hours=1))

    data = report.fetch(isolated, T0, T0 + timedelta(hours=1))
    assert data.records == 2


def test_unbounded_window_covers_everything(isolated) -> None:
    add(isolated, action="clean", processed_ts=T0 - timedelta(days=400))
    add(isolated, action="clean", processed_ts=T0 + timedelta(days=400))

    assert report.fetch(isolated, None, None).records == 2


def test_entity_counts_separate_mentions_from_records(isolated) -> None:
    """Two IBANs in one message is two mentions across one record. Collapsing
    them would understate exposure; counting a record per mention would
    overstate reach."""
    add(
        isolated,
        action="redacted",
        processed_ts=T0,
        findings=(
            ("IBAN_PK", "iban_from", 1.0, 1),
            ("IBAN_PK", "iban_to", 1.0, 1),
            ("CNIC", "cnic", 1.0, 1),
        ),
    )
    add(isolated, action="redacted", processed_ts=T0, findings=(("IBAN_PK", "iban_from", 1.0, 1),))

    data = report.fetch(isolated, None, None)
    by_type = {e.entity_type: e for e in data.entities}

    assert by_type["IBAN_PK"].mentions == 3
    assert by_type["IBAN_PK"].messages == 2
    assert by_type["CNIC"].mentions == 1
    assert by_type["CNIC"].messages == 1


def test_clean_messages_are_counted_but_contribute_no_entities(isolated) -> None:
    """The inner join in _Q_ENTITIES drops messages with no findings, which is
    correct -- they are accounted for as 'clean' in the disposition."""
    add(isolated, action="clean", processed_ts=T0)
    add(isolated, action="redacted", processed_ts=T0, findings=(("EMAIL", "email", 1.0, 1),))

    data = report.fetch(isolated, None, None)
    assert data.records == 2
    assert dict(data.disposition) == {"clean": 1, "redacted": 1}
    assert [e.entity_type for e in data.entities] == ["EMAIL"]


def test_uncertain_queue_includes_messages_with_no_findings(isolated) -> None:
    """The reason _Q_UNCERTAIN uses a LEFT JOIN. A message quarantined with no
    findings at all is precisely the case most needing review, and an inner
    join would silently drop it from the reviewer's worklist."""
    add(isolated, action="quarantined", processed_ts=T0)

    data = report.fetch(isolated, None, None)
    assert data.uncertain_total == 1
    assert len(data.uncertain) == 1
    assert data.uncertain[0].detail == "no findings recorded"


def test_quarantine_splits_into_disjoint_queues(isolated) -> None:
    """failure_class is what separates 'will fail again on replay' from 'needs
    a human'. A message must appear in exactly one queue."""
    add(
        isolated,
        action="quarantined",
        processed_ts=T0,
        failure_class="ValueError",
        failure_detail="unparseable payload",
    )
    add(
        isolated,
        action="quarantined",
        processed_ts=T0,
        findings=(("IBAN_PK", "iban_from", 0.5, 1),),
    )

    data = report.fetch(isolated, None, None)
    assert data.failclosed_total == 1
    assert data.uncertain_total == 1
    assert data.failclosed[0].reason == "ValueError"
    assert data.failclosed[0].detail == "unparseable payload"
    assert "IBAN_PK" in data.uncertain[0].detail

    failclosed_ids = {i.message_id for i in data.failclosed}
    uncertain_ids = {i.message_id for i in data.uncertain}
    assert failclosed_ids.isdisjoint(uncertain_ids)


def test_queue_listing_is_capped_but_totals_are_not(isolated) -> None:
    """The truncation note in the report is only honest if the total is
    counted independently of the capped listing."""
    for _ in range(5):
        add(isolated, action="quarantined", processed_ts=T0)

    data = report.fetch(isolated, None, None, max_queue=2)
    assert len(data.uncertain) == 2
    assert data.uncertain_total == 5


def test_min_confidence_reflects_the_weakest_detection(isolated) -> None:
    """A checksum-failed hit is recorded at reduced confidence; the report
    must surface the weakest, not the average, or a single uncertain
    detection disappears behind confident ones."""
    add(
        isolated,
        action="redacted",
        processed_ts=T0,
        findings=(("IBAN_PK", "iban_from", 1.0, 1), ("IBAN_PK", "iban_to", 0.5, 1)),
    )

    data = report.fetch(isolated, None, None)
    assert data.entities[0].min_confidence == pytest.approx(0.5)


def test_scope_reports_both_event_and_processed_ranges(isolated) -> None:
    """They differ: event_ts is when the transaction happened, processed_ts
    when the pipeline saw it. A reviewer asking 'what period does this cover?'
    means the former."""
    add(isolated, action="clean", processed_ts=T0, event_ts=T0 - timedelta(days=2))
    add(isolated, action="clean", processed_ts=T0 + timedelta(minutes=5), event_ts=T0)

    data = report.fetch(isolated, None, None)
    assert data.event_ts_min == T0 - timedelta(days=2)
    assert data.event_ts_max == T0
    assert data.processed_ts_min == T0
    assert data.processed_ts_max == T0 + timedelta(minutes=5)


def test_topic_and_field_breakdowns_group_correctly(isolated) -> None:
    add(isolated, action="clean", processed_ts=T0, source_topic="txn.raw")
    add(isolated, action="clean", processed_ts=T0, source_topic="txn.raw")
    add(
        isolated,
        action="redacted",
        processed_ts=T0,
        source_topic="doc.raw",
        findings=(("EMAIL", "body", 1.0, 1), ("EMAIL", "body", 1.0, 1), ("EMAIL", "subject", 1.0, 1)),
    )

    data = report.fetch(isolated, None, None)
    assert dict(data.by_topic) == {"txn.raw": 2, "doc.raw": 1}
    assert data.entity_fields == [("EMAIL", "body", 2), ("EMAIL", "subject", 1)]


def test_failure_classes_are_counted(isolated) -> None:
    add(isolated, action="quarantined", processed_ts=T0, failure_class="ValueError", failure_detail="x")
    add(isolated, action="quarantined", processed_ts=T0, failure_class="ValueError", failure_detail="y")
    add(isolated, action="quarantined", processed_ts=T0, failure_class="KeyError", failure_detail="z")

    data = report.fetch(isolated, None, None)
    assert data.failures == [("ValueError", 2), ("KeyError", 1)]


def test_fetch_output_renders(isolated) -> None:
    """End to end: real rows through the real queries into the real renderer."""
    add(
        isolated,
        action="redacted",
        processed_ts=T0,
        findings=(("CNIC", "cnic", 1.0, 1),),
    )
    markdown = report.render(report.fetch(isolated, None, None))
    assert "# Data Governance Report" in markdown
    assert "National identity number" in markdown