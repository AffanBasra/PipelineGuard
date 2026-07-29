"""Governance report: what personal data flowed through, and what was done.

Its reader is someone answering "what personal data did this pipeline see over
this period, what happened to it, and what needs a human?" -- a compliance
reviewer first, an engineer second. Both are served by the same facts rather
than by a compliance section with a dashboard bolted on: every figure is
stated with the scope it was measured over, which a reviewer needs to judge
coverage and an engineer reads as volume and throughput.

Source of truth is the audit trail and nothing else. The report re-reads no
Kafka topic and no message payload, so it cannot disclose a value the audit
trail declined to store -- `findings` holds entity type, field, span, tier and
confidence, never the matched text. The report inherits that property rather
than re-establishing it.

Layering, which exists for testability as much as for tidiness:

  * `_Q_*` and `fetch()` are the only code that touches a database. `fetch()`
    runs each query and packs the rows into `ReportData`, doing no aggregation
    of its own -- the GROUP BYs live in SQL, where they belong for a report
    over an audit table.
  * `render()` and everything it calls are pure functions over `ReportData`.

That split is deliberate. Handing canned rows to a fake connection would test
the renderer and leave the queries -- a wrong GROUP BY, a window inclusive at
the wrong end, a join that drops messages with no findings -- entirely
unexercised while the suite went green. So the pure layer is tested offline
and the queries are tested against a real Postgres (tests/integration/), which
is the only thing that can actually execute them.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from pipelineguard import compliance
from pipelineguard.config import settings

# Every query is bounded by the same half-open window on processed_ts:
# [since, until). Half-open so that consecutive reports over adjacent windows
# partition the data exactly -- a closed upper bound would count a record
# landing precisely on the boundary in both, which is the sort of thing that
# is never noticed until two reports disagree by one.
#
# COALESCE against the infinities lets a caller leave either end unbounded
# while keeping one parameterised query per section rather than assembling
# WHERE clauses by string concatenation.
_WINDOW = """
    processed_ts >= COALESCE(%s::timestamptz, '-infinity')
    AND processed_ts <  COALESCE(%s::timestamptz,  'infinity')
"""

_Q_SCOPE = f"""
SELECT count(*),
       min(event_ts), max(event_ts),
       min(processed_ts), max(processed_ts)
FROM messages_processed
WHERE {_WINDOW};
"""

_Q_BY_TOPIC = f"""
SELECT source_topic, count(*)
FROM messages_processed
WHERE {_WINDOW}
GROUP BY source_topic
ORDER BY count(*) DESC, source_topic;
"""

_Q_DISPOSITION = f"""
SELECT action, count(*)
FROM messages_processed
WHERE {_WINDOW}
GROUP BY action
ORDER BY action;
"""

_Q_BY_TIER = f"""
SELECT max_tier, count(*)
FROM messages_processed
WHERE {_WINDOW}
GROUP BY max_tier
ORDER BY max_tier;
"""

# Inner join: a finding cannot exist without its message (FK, ON DELETE
# CASCADE), and the window predicate lives on the message. Messages with no
# findings are absent by construction and are accounted for in section 2's
# disposition counts instead -- they are the 'clean' rows.
_Q_ENTITIES = f"""
SELECT f.entity_type,
       count(*),
       count(DISTINCT f.message_id),
       min(f.tier), max(f.tier),
       min(f.confidence)
FROM findings f
JOIN messages_processed m USING (message_id)
WHERE {_WINDOW}
GROUP BY f.entity_type
ORDER BY count(*) DESC, f.entity_type;
"""

_Q_ENTITY_FIELDS = f"""
SELECT f.entity_type, f.field, count(*)
FROM findings f
JOIN messages_processed m USING (message_id)
WHERE {_WINDOW}
GROUP BY f.entity_type, f.field
ORDER BY f.entity_type, count(*) DESC, f.field;
"""

_Q_FAILURES = f"""
SELECT failure_class, count(*)
FROM messages_processed
WHERE {_WINDOW} AND failure_class IS NOT NULL
GROUP BY failure_class
ORDER BY count(*) DESC, failure_class;
"""

# Two quarantine queues, deliberately separate. A fail-closed quarantine is
# deterministic -- it will fail identically on replay -- and is an engineering
# defect. An uncertain quarantine is a judgement the pipeline declined to
# make, and is a human's decision. Collapsing them into one list would hand a
# reviewer a worklist they cannot triage.
_Q_FAILCLOSED = f"""
SELECT message_id, processed_ts, failure_class, failure_detail
FROM messages_processed
WHERE {_WINDOW} AND action = 'quarantined' AND failure_class IS NOT NULL
ORDER BY processed_ts DESC
LIMIT %s;
"""

_Q_FAILCLOSED_TOTAL = f"""
SELECT count(*)
FROM messages_processed
WHERE {_WINDOW} AND action = 'quarantined' AND failure_class IS NOT NULL;
"""

# LEFT JOIN, unlike _Q_ENTITIES: a message can be quarantined as uncertain
# with no findings at all, and dropping those would hide exactly the records
# most in need of review.
_Q_UNCERTAIN = f"""
SELECT m.message_id,
       m.processed_ts,
       string_agg(DISTINCT f.entity_type, ', '),
       min(f.confidence)
FROM messages_processed m
LEFT JOIN findings f USING (message_id)
WHERE {_WINDOW} AND m.action = 'quarantined' AND m.failure_class IS NULL
GROUP BY m.message_id, m.processed_ts
ORDER BY m.processed_ts DESC
LIMIT %s;
"""

_Q_UNCERTAIN_TOTAL = f"""
SELECT count(*)
FROM messages_processed
WHERE {_WINDOW} AND action = 'quarantined' AND failure_class IS NULL;
"""


@dataclass(frozen=True)
class EntityRow:
    entity_type: str
    mentions: int
    messages: int
    min_tier: int
    max_tier: int
    min_confidence: float


@dataclass(frozen=True)
class ReviewItem:
    message_id: str
    processed_ts: datetime
    reason: str
    detail: str


@dataclass(frozen=True)
class ReportData:
    """Everything the renderer needs. Plain data -- no connection, no cursor,
    nothing lazy -- so every rendering test is a pure function call."""

    since: datetime | None
    until: datetime | None
    generated_at: datetime

    records: int
    event_ts_min: datetime | None
    event_ts_max: datetime | None
    processed_ts_min: datetime | None
    processed_ts_max: datetime | None
    by_topic: list[tuple[str, int]]

    disposition: list[tuple[str, int]]
    by_tier: list[tuple[int, int]]

    entities: list[EntityRow]
    entity_fields: list[tuple[str, str, int]]

    failures: list[tuple[str, int]]
    failclosed: list[ReviewItem]
    failclosed_total: int
    uncertain: list[ReviewItem]
    uncertain_total: int


# --------------------------------------------------------------------------- #
# I/O layer -- the only code here that touches a database
# --------------------------------------------------------------------------- #
def fetch(conn: Any, since: datetime | None, until: datetime | None, max_queue: int = 50) -> ReportData:
    """Run every section's query and pack the rows into `ReportData`.

    Does no aggregation: each query returns exactly the shape the renderer
    consumes, so there is nowhere for a counting bug to hide between the two.
    """
    window = (since, until)

    with conn.cursor() as cur:
        cur.execute(_Q_SCOPE, window)
        records, ev_min, ev_max, pr_min, pr_max = cur.fetchone()

        cur.execute(_Q_BY_TOPIC, window)
        by_topic = [(t, n) for t, n in cur.fetchall()]

        cur.execute(_Q_DISPOSITION, window)
        disposition = [(a, n) for a, n in cur.fetchall()]

        cur.execute(_Q_BY_TIER, window)
        by_tier = [(int(t), n) for t, n in cur.fetchall()]

        cur.execute(_Q_ENTITIES, window)
        entities = [
            EntityRow(et, mentions, messages, int(mn_tier), int(mx_tier), float(mn_conf))
            for et, mentions, messages, mn_tier, mx_tier, mn_conf in cur.fetchall()
        ]

        cur.execute(_Q_ENTITY_FIELDS, window)
        entity_fields = [(et, f, n) for et, f, n in cur.fetchall()]

        cur.execute(_Q_FAILURES, window)
        failures = [(fc, n) for fc, n in cur.fetchall()]

        cur.execute(_Q_FAILCLOSED, (*window, max_queue))
        failclosed = [
            ReviewItem(str(mid), ts, fc, detail or "")
            for mid, ts, fc, detail in cur.fetchall()
        ]

        cur.execute(_Q_FAILCLOSED_TOTAL, window)
        (failclosed_total,) = cur.fetchone()

        cur.execute(_Q_UNCERTAIN, (*window, max_queue))
        uncertain = [
            ReviewItem(
                str(mid),
                ts,
                "sub-threshold confidence",
                _uncertain_detail(types, conf),
            )
            for mid, ts, types, conf in cur.fetchall()
        ]

        cur.execute(_Q_UNCERTAIN_TOTAL, window)
        (uncertain_total,) = cur.fetchone()

    return ReportData(
        since=since,
        until=until,
        generated_at=datetime.now(timezone.utc),
        records=records,
        event_ts_min=ev_min,
        event_ts_max=ev_max,
        processed_ts_min=pr_min,
        processed_ts_max=pr_max,
        by_topic=by_topic,
        disposition=disposition,
        by_tier=by_tier,
        entities=entities,
        entity_fields=entity_fields,
        failures=failures,
        failclosed=failclosed,
        failclosed_total=failclosed_total,
        uncertain=uncertain,
        uncertain_total=uncertain_total,
    )


def _uncertain_detail(entity_types: str | None, min_confidence: float | None) -> str:
    """A message quarantined as uncertain may carry no findings at all -- the
    LEFT JOIN yields NULLs there. Say so rather than rendering 'None'."""
    if not entity_types:
        return "no findings recorded"
    if min_confidence is None:
        return entity_types
    return f"{entity_types} (lowest confidence {min_confidence:.2f})"


# --------------------------------------------------------------------------- #
# Pure rendering
# --------------------------------------------------------------------------- #
def _ts(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%SZ") if value else "--"


def _window_label(since: datetime | None, until: datetime | None) -> str:
    if since is None and until is None:
        return "all recorded activity (no window specified)"
    return f"{_ts(since) if since else 'earliest record'} to {_ts(until) if until else 'latest record'}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def _throughput(data: ReportData) -> str:
    """Observed wall-clock rate across the window actually covered by records.

    Not a capacity claim: the pipeline may have been idle for much of the
    span, which would understate it, or the window may clip a longer run.
    """
    if not (data.processed_ts_min and data.processed_ts_max and data.records):
        return "--"
    seconds = (data.processed_ts_max - data.processed_ts_min).total_seconds()
    if seconds <= 0:
        return f"{data.records:,} records within one second"
    return f"{data.records / seconds:,.0f} records/second observed"


def render(data: ReportData) -> str:
    """Render `data` as Markdown. Pure: same input, same output, always."""
    tier_names = {0: "no findings", 1: "Tier 1 (rules)", 2: "Tier 2 (encoder)", 3: "Tier 3 (LLM)"}
    dispositions = dict(data.disposition)
    lines: list[str] = [
        "# Data Governance Report",
        "",
        f"**Period covered:** {_window_label(data.since, data.until)}  ",
        f"**Generated:** {_ts(data.generated_at)}  ",
        f"**Source:** PipelineGuard audit trail (`messages_processed`, `findings`)",
        "",
        f"> {compliance.DISCLAIMER}",
        "",
        "---",
        "",
        "## 1. Scope of scan",
        "",
        "A finding count means nothing without knowing what was scanned to produce it.",
        "",
    ]

    lines += _table(
        ["measure", "value"],
        [
            ["Records scanned", f"{data.records:,}"],
            ["Earliest event", _ts(data.event_ts_min)],
            ["Latest event", _ts(data.event_ts_max)],
            ["First processed", _ts(data.processed_ts_min)],
            ["Last processed", _ts(data.processed_ts_max)],
            ["Observed rate", _throughput(data)],
        ],
    )

    if data.records:
        lines += [
            "",
            "The observed rate is wall-clock across the span these records "
            "actually cover, including any time the pipeline sat idle waiting "
            "for input. It is a description of this period, **not a capacity "
            "measurement**, and will read lower than a saturated benchmark of "
            "the same system.",
        ]

    if data.by_topic:
        lines += ["", "**Sources**", ""]
        lines += _table(["topic", "records"], [[t, f"{n:,}"] for t, n in data.by_topic])

    lines += ["", "## 2. Disposition", ""]
    if data.records:
        lines += _table(
            ["outcome", "records", "share"],
            [
                [action, f"{n:,}", f"{100 * n / data.records:.2f}%"]
                for action, n in data.disposition
            ],
        )
        lines += [
            "",
            "`clean` -- no personal data detected. `redacted` -- personal data "
            "detected and masked in place before the record was forwarded. "
            "`quarantined` -- withheld from the clean stream for review; see "
            "section 6.",
        ]
    else:
        lines += ["No records were processed in this period."]

    lines += ["", "## 3. Detection tiers", ""]
    if data.by_tier:
        lines += _table(
            ["highest tier reached", "records"],
            [[tier_names.get(t, f"tier {t}"), f"{n:,}"] for t, n in data.by_tier],
        )
    else:
        lines += ["No records were processed in this period."]

    lines += ["", "## 4. Personal data observed", ""]
    if data.entities:
        rows = []
        for e in data.entities:
            cls = compliance.classify(e.entity_type)
            category = cls.data_category if cls else "**unclassified**"
            tier = (
                tier_names.get(e.min_tier, str(e.min_tier))
                if e.min_tier == e.max_tier
                else f"tiers {e.min_tier}-{e.max_tier}"
            )
            rows.append(
                [
                    e.entity_type,
                    category,
                    f"{e.mentions:,}",
                    f"{e.messages:,}",
                    tier,
                    f"{e.min_confidence:.2f}",
                ]
            )
        lines += _table(
            ["entity type", "category", "mentions", "records", "detected by", "min confidence"],
            rows,
        )
        lines += [
            "",
            "Counts are of *detections*, not of distinct individuals -- the "
            "same person appearing in two records is counted twice, because "
            "the audit trail stores no value with which to link them. That is "
            "a deliberate consequence of not retaining personal data.",
        ]

        lines += ["", "### Regulatory basis", ""]
        for e in data.entities:
            cls = compliance.classify(e.entity_type)
            if cls is None:
                continue
            lines += [
                f"**{e.entity_type}** -- {cls.data_category}"
                + ("  *(special category)*" if cls.special_category else ""),
                "",
                f"- *Pakistan:* {cls.pk_basis}",
                f"- *GDPR:* {cls.gdpr_basis}",
                "",
            ]
        lines += [
            f"> {compliance.CROSS_BORDER_NOTE}",
            "",
            "The Pakistani and GDPR bases above differ in character, not "
            "merely in detail -- see section 8.",
            "",
        ]

        missing = compliance.unclassified([e.entity_type for e in data.entities])
        if missing:
            lines += [
                "### Unclassified entity types",
                "",
                "The following were detected but have no regulatory "
                "classification in this build. They are listed rather than "
                "omitted: a reader must be able to tell 'none found' from "
                "'found and not understood'.",
                "",
            ]
            lines += [f"- `{m}`" for m in missing]
            lines += [""]
    else:
        lines += ["No personal data was detected in this period."]

    lines += ["", "## 5. Where it was found", ""]
    if data.entity_fields:
        lines += _table(
            ["entity type", "field", "mentions"],
            [[et, f"`{f}`", f"{n:,}"] for et, f, n in data.entity_fields],
        )
    else:
        lines += ["No personal data was detected in this period."]

    lines += ["", "## 6. Items requiring review", ""]
    quarantined = dispositions.get("quarantined", 0)
    lines += [
        f"{quarantined:,} record(s) were quarantined: {data.failclosed_total:,} "
        f"failed closed and {data.uncertain_total:,} were withheld as uncertain. "
        "The two are separate worklists.",
        "",
        "### 6.1 Failed closed -- engineering",
        "",
        "Deterministic failures: unparseable bytes, wrong payload shape, or a "
        "detector that raised. These will fail identically on replay, so they "
        "need a fix rather than a decision.",
        "",
    ]
    if data.failclosed:
        lines += _table(
            ["message id", "processed", "failure", "detail"],
            [[f"`{i.message_id}`", _ts(i.processed_ts), i.reason, i.detail] for i in data.failclosed],
        )
        if data.failclosed_total > len(data.failclosed):
            lines += ["", f"*Showing {len(data.failclosed):,} of {data.failclosed_total:,}.*"]
    else:
        lines += ["None in this period."]

    lines += [
        "",
        "### 6.2 Uncertain -- human decision",
        "",
        "The pipeline detected something it was not confident enough to act "
        "on, and declined to guess. Each needs a person to confirm or clear it.",
        "",
    ]
    if data.uncertain:
        lines += _table(
            ["message id", "processed", "reason", "detail"],
            [[f"`{i.message_id}`", _ts(i.processed_ts), i.reason, i.detail] for i in data.uncertain],
        )
        if data.uncertain_total > len(data.uncertain):
            lines += ["", f"*Showing {len(data.uncertain):,} of {data.uncertain_total:,}.*"]
    else:
        lines += ["None in this period."]

    lines += ["", "## 7. Processing failures", ""]
    if data.failures:
        lines += _table(
            ["failure class", "records"], [[f"`{fc}`", f"{n:,}"] for fc, n in data.failures]
        )
    else:
        lines += ["No processing failures were recorded in this period."]

    lines += [
        "",
        "## 8. System properties",
        "",
        "What the pipeline does, and why each regime cares. The behaviour is "
        "a statement of fact about this system; the two bases explain "
        "significance and are not assertions that an obligation has been "
        "discharged.",
        "",
        f"> {compliance.LEGAL_LANDSCAPE_NOTE}",
        "",
    ]
    for prop in compliance.SYSTEM_PROPERTIES:
        lines += [
            f"### {prop.title}",
            "",
            prop.behaviour,
            "",
            f"- *Pakistan:* {prop.pk_basis}",
            f"- *GDPR:* {prop.gdpr_basis}",
            "",
        ]

    lines += [
        "## 9. Limitations",
        "",
        "Stated because a governance document that overstates its own coverage "
        "is worse than none.",
        "",
        "- **This report describes what entered the pipeline, not what "
        "existed.** Whether every record reached it is a question about Kafka "
        "offsets and consumer lag, which this audit trail cannot answer. It is "
        "not evidence that a source system was scanned completely.",
        "- **Detection is not exhaustive.** Only entity types the configured "
        "detectors recognise can appear here. Personal data of a type no "
        "detector covers passes through unrecorded and is indistinguishable, "
        "in this report, from its absence.",
        "- **Counts are of detections, not of people.** No value is stored, so "
        "records referring to the same individual cannot be linked.",
        "- **Latency figures elsewhere in this project measure detection "
        "only**, not end-to-end processing time.",
        "- **Delivery is at-least-once.** A record processed twice is upserted "
        "on its message id, so it is counted once here -- but the guarantee is "
        "idempotent recording, not exactly-once processing.",
        "",
    ]

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the PipelineGuard governance report.")
    parser.add_argument("--since", type=_parse_ts, default=None, help="ISO-8601 window start (inclusive)")
    parser.add_argument("--until", type=_parse_ts, default=None, help="ISO-8601 window end (exclusive)")
    parser.add_argument("--max-queue", type=int, default=50, help="max review items listed per queue")
    parser.add_argument("--output", default=None, help="write to this path instead of stdout")
    args = parser.parse_args(argv)

    import psycopg

    conn = psycopg.connect(settings.postgres_dsn)
    try:
        data = fetch(conn, args.since, args.until, args.max_queue)
    finally:
        conn.close()

    markdown = render(data)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())