-- PipelineGuard audit schema.
-- Design notes:
--  * messages_processed is keyed on message_id (the producer-assigned UUID in the
--    envelope), NOT a serial — this is what makes audit writes idempotent under
--    at-least-once delivery: a redelivered message upserts instead of duplicating.
--  * findings carries one row per detected entity; entity VALUES are never stored,
--    only type/span/tier/confidence. The audit trail must not itself become a PII store.

CREATE TABLE IF NOT EXISTS messages_processed (
    message_id      UUID PRIMARY KEY,
    source_topic    TEXT        NOT NULL,
    event_ts        TIMESTAMPTZ NOT NULL,           -- from the message envelope
    processed_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
    max_tier        SMALLINT    NOT NULL DEFAULT 0, -- highest tier that ran (0 = no findings)
    action          TEXT        NOT NULL CHECK (action IN ('clean', 'redacted', 'quarantined')),
    latency_ms      DOUBLE PRECISION NOT NULL,      -- end-to-end detection latency for this message
    schema_version  SMALLINT    NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS findings (
    id           BIGSERIAL PRIMARY KEY,
    message_id   UUID     NOT NULL REFERENCES messages_processed (message_id) ON DELETE CASCADE,
    entity_type  TEXT     NOT NULL,    -- e.g. CNIC, IBAN_PK, PHONE_PK, EMAIL, PERSON_NAME
    field        TEXT     NOT NULL,    -- which payload field it was found in
    span_start   INTEGER  NOT NULL,
    span_end     INTEGER  NOT NULL,
    tier         SMALLINT NOT NULL CHECK (tier IN (1, 2, 3)),
    confidence   REAL     NOT NULL,
    action       TEXT     NOT NULL CHECK (action IN ('redacted', 'quarantined'))
);

-- Redeliveries delete-and-reinsert findings for the message, so no unique
-- constraint is needed here; idempotency is enforced at the writer level
-- (see src/pipelineguard/audit.py).

CREATE INDEX IF NOT EXISTS idx_findings_message   ON findings (message_id);
CREATE INDEX IF NOT EXISTS idx_findings_type      ON findings (entity_type);
CREATE INDEX IF NOT EXISTS idx_processed_ts       ON messages_processed (processed_ts);
CREATE INDEX IF NOT EXISTS idx_processed_action   ON messages_processed (action);
