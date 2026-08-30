BEGIN;

CREATE SCHEMA audit;

CREATE TABLE audit.schema_version (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    version smallint NOT NULL CHECK (version > 0),
    applied_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

INSERT INTO audit.schema_version (singleton, version) VALUES (TRUE, 1);

CREATE TABLE audit.executions (
    audit_id uuid PRIMARY KEY,
    execution_id uuid NOT NULL UNIQUE,
    capability varchar(64) NOT NULL CHECK (capability = 'resolve_codebase_fact'),
    repository_id varchar(16) NOT NULL CHECK (repository_id ~ '^[0-9a-f]{16}$'),
    repository_fingerprint char(64) NOT NULL
        CHECK (repository_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE TABLE audit.events (
    event_id uuid PRIMARY KEY,
    audit_id uuid NOT NULL REFERENCES audit.executions(audit_id),
    sequence smallint NOT NULL CHECK (sequence > 0),
    event_type varchar(64) NOT NULL,
    event_version smallint NOT NULL CHECK (event_version = 1),
    occurred_at timestamptz NOT NULL,
    persisted_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    content_hash char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT audit_events_sequence_unique UNIQUE (audit_id, sequence),
    CONSTRAINT audit_events_v1_shape CHECK (
        (sequence = 1 AND event_type = 'execution.started')
        OR (sequence = 2 AND event_type = 'investigation.completed')
    )
);

COMMIT;
