BEGIN;

DO $schema_guard$
BEGIN
    IF (SELECT version FROM audit.schema_version WHERE singleton) IS DISTINCT FROM 3 THEN
        RAISE EXCEPTION 'Audit schema migration 0004 requires version 3';
    END IF;
    IF CURRENT_USER <> 'maestro_audit_migrator' THEN
        RAISE EXCEPTION 'Audit schema migration 0004 requires the migration-owner role';
    END IF;
END
$schema_guard$;

-- An authority evaluation is its own audited execution. It is not a resolve_codebase_fact
-- run, and recording it as one would misattribute what happened.
ALTER TABLE audit.executions
    DROP CONSTRAINT executions_capability_check;

ALTER TABLE audit.executions
    ADD CONSTRAINT executions_capability_check CHECK (
        capability IN ('resolve_codebase_fact', 'decision_authority')
    );

-- Audit gains authority.applied and nothing else. Requesting, proposing, approving,
-- rejecting, and superseding a decision are coordination, and ADR-0004 gives coordination to
-- Work Management, so no decision-lifecycle event type is added here.
ALTER TABLE audit.events
    DROP CONSTRAINT audit_events_v1_shape;

ALTER TABLE audit.events
    ADD CONSTRAINT audit_events_v1_shape CHECK (
        (sequence = 1 AND event_type = 'execution.started')
        OR (
            sequence = 2
            AND event_type IN (
                'investigation.completed',
                'execution.failed',
                'authority.applied'
            )
        )
    );

DROP VIEW audit_read.event_timeline;

-- An authority event carried none of the fields the timeline projected, so it appeared as an
-- empty row. The timeline is the view that answers "what happened, in order", and an event
-- that shows up blank there is worse than one that is absent.
CREATE VIEW audit_read.event_timeline
WITH (security_barrier = true)
AS
SELECT
    event.audit_id,
    event.event_id,
    execution.execution_id,
    execution.repository_id,
    event.sequence,
    event.event_type,
    event.event_version,
    event.occurred_at,
    event.payload ->> 'objective' AS objective,
    event.payload ->> 'status' AS semantic_status,
    event.payload ->> 'answer' AS answer,
    event.payload ->> 'confidence' AS confidence,
    event.payload ->> 'rationale' AS rationale,
    event.payload ->> 'error_code' AS error_code,
    event.payload ->> 'failure_stage' AS failure_stage,
    event.payload ->> 'subject' AS authority_subject,
    event.payload ->> 'choice' AS authority_choice,
    event.payload ->> 'scope' AS authority_scope,
    event.payload ->> 'approved_by' AS authority_approved_by
FROM audit.events AS event
JOIN audit.executions AS execution ON execution.audit_id = event.audit_id
ORDER BY event.audit_id, event.sequence;

DROP VIEW audit_read.execution_summary;

CREATE VIEW audit_read.execution_summary
WITH (security_barrier = true)
AS
SELECT
    execution.audit_id,
    execution.execution_id,
    execution.capability,
    execution.repository_id,
    started.occurred_at AS started_at,
    terminal.occurred_at AS terminal_at,
    started.payload ->> 'objective' AS objective,
    CASE
        WHEN terminal.event_id IS NULL THEN 'incomplete'
        WHEN terminal.event_type = 'execution.failed' THEN 'failed'
        WHEN terminal.event_type = 'authority.applied' THEN 'authority_applied'
        ELSE terminal.payload ->> 'status'
    END AS outcome,
    terminal.payload ->> 'answer' AS answer,
    terminal.payload ->> 'confidence' AS confidence,
    terminal.payload ->> 'rationale' AS rationale,
    terminal.payload ->> 'error_code' AS error_code,
    terminal.payload ->> 'failure_stage' AS failure_stage,
    CASE
        WHEN pg_catalog.jsonb_typeof(terminal.payload -> 'evidence') = 'array'
            THEN pg_catalog.jsonb_array_length(terminal.payload -> 'evidence')
        ELSE 0
    END AS evidence_count,
    CASE
        WHEN pg_catalog.jsonb_typeof(terminal.payload -> 'conflicts') = 'array'
            THEN pg_catalog.jsonb_array_length(terminal.payload -> 'conflicts')
        ELSE 0
    END AS conflict_count,
    terminal.event_id IS NULL AS is_incomplete
FROM audit.executions AS execution
LEFT JOIN audit.events AS started
    ON started.audit_id = execution.audit_id AND started.sequence = 1
LEFT JOIN audit.events AS terminal
    ON terminal.audit_id = execution.audit_id AND terminal.sequence = 2;

-- The applied decision's content is read back as it was captured. A later edit to the work
-- item changes the item, never this row, which is the point of capturing rather than
-- referencing it.
CREATE VIEW audit_read.applied_authority
WITH (security_barrier = true)
AS
SELECT
    event.audit_id,
    execution.execution_id,
    execution.repository_id,
    event.occurred_at,
    event.payload ->> 'source_kind' AS source_kind,
    event.payload ->> 'subject' AS subject,
    event.payload ->> 'choice' AS choice,
    event.payload ->> 'scope' AS scope,
    event.payload ->> 'validity' AS validity,
    event.payload ->> 'approved_by' AS approved_by,
    event.payload ->> 'rationale' AS rationale,
    event.payload ->> 'origin' AS origin,
    event.payload ->> 'work_item' AS work_item,
    event.payload ->> 'source_digest' AS source_digest
FROM audit.events AS event
JOIN audit.executions AS execution ON execution.audit_id = event.audit_id
WHERE event.event_type = 'authority.applied';

-- The reader keeps SELECT on the read schema and gains nothing else. The writer's grants are
-- untouched: it stays append-only over the same two tables and the same two verify functions.
GRANT SELECT ON audit_read.applied_authority TO maestro_audit_reader;
GRANT SELECT ON audit_read.execution_summary TO maestro_audit_reader;
GRANT SELECT ON audit_read.event_timeline TO maestro_audit_reader;

UPDATE audit.schema_version SET version = 4 WHERE singleton;

COMMIT;
