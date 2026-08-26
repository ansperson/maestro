BEGIN;

DO $schema_guard$
BEGIN
    IF (SELECT version FROM audit.schema_version WHERE singleton) IS DISTINCT FROM 2 THEN
        RAISE EXCEPTION 'Audit schema migration 0003 requires version 2';
    END IF;
    IF CURRENT_USER <> 'maestro_audit_migrator' THEN
        RAISE EXCEPTION 'Audit schema migration 0003 requires the migration-owner role';
    END IF;
END
$schema_guard$;

CREATE SCHEMA audit_read AUTHORIZATION maestro_audit_migrator;

CREATE FUNCTION audit.verify_execution_v1(
    expected_audit_id uuid,
    expected_execution_id uuid,
    expected_capability text,
    expected_repository_id text,
    expected_repository_fingerprint text
) RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $verify_execution$
    SELECT
        count(*) = 1
        AND count(*) FILTER (
            WHERE execution.audit_id = expected_audit_id
              AND execution.execution_id = expected_execution_id
              AND execution.capability = expected_capability
              AND execution.repository_id = expected_repository_id
              AND execution.repository_fingerprint = expected_repository_fingerprint
        ) = 1
    FROM audit.executions AS execution
    WHERE execution.audit_id = expected_audit_id
       OR execution.execution_id = expected_execution_id
$verify_execution$;

CREATE FUNCTION audit.verify_event_v1(
    expected_event_id uuid,
    expected_audit_id uuid,
    expected_execution_id uuid,
    expected_sequence smallint,
    expected_event_type text,
    expected_event_version smallint,
    expected_occurred_at timestamptz,
    expected_content_hash text,
    expected_payload jsonb
) RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $verify_event$
    SELECT
        count(*) = 1
        AND count(*) FILTER (
            WHERE event.event_id = expected_event_id
              AND event.audit_id = expected_audit_id
              AND execution.execution_id = expected_execution_id
              AND event.sequence = expected_sequence
              AND event.event_type = expected_event_type
              AND event.event_version = expected_event_version
              AND event.occurred_at = expected_occurred_at
              AND event.content_hash = expected_content_hash
              AND event.payload = expected_payload
        ) = 1
    FROM audit.events AS event
    JOIN audit.executions AS execution ON execution.audit_id = event.audit_id
    WHERE event.event_id = expected_event_id
       OR (event.audit_id = expected_audit_id AND event.sequence = expected_sequence)
$verify_event$;

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
    event.payload ->> 'failure_stage' AS failure_stage
FROM audit.events AS event
JOIN audit.executions AS execution ON execution.audit_id = event.audit_id
ORDER BY event.audit_id, event.sequence;

CREATE VIEW audit_read.evidence
WITH (security_barrier = true)
AS
SELECT
    event.audit_id,
    execution.execution_id,
    execution.repository_id,
    'primary'::text AS evidence_scope,
    NULL::bigint AS conflict_ordinal,
    NULL::text AS conflict_description,
    primary_evidence.ordinality AS evidence_ordinal,
    primary_evidence.item ->> 'path' AS path,
    CASE
        WHEN pg_catalog.jsonb_typeof(primary_evidence.item -> 'line_start') = 'number'
            THEN primary_evidence.item ->> 'line_start'
    END AS line_start,
    CASE
        WHEN pg_catalog.jsonb_typeof(primary_evidence.item -> 'line_end') = 'number'
            THEN primary_evidence.item ->> 'line_end'
    END AS line_end,
    primary_evidence.item ->> 'symbol' AS symbol,
    primary_evidence.item ->> 'finding' AS finding
FROM audit.events AS event
JOIN audit.executions AS execution ON execution.audit_id = event.audit_id
CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
    CASE
        WHEN pg_catalog.jsonb_typeof(event.payload -> 'evidence') = 'array'
            THEN event.payload -> 'evidence'
        ELSE '[]'::jsonb
    END
) WITH ORDINALITY AS primary_evidence(item, ordinality)
WHERE event.event_type = 'investigation.completed'

UNION ALL

SELECT
    event.audit_id,
    execution.execution_id,
    execution.repository_id,
    'conflict'::text AS evidence_scope,
    conflict.ordinality AS conflict_ordinal,
    conflict.item ->> 'description' AS conflict_description,
    conflict_evidence.ordinality AS evidence_ordinal,
    conflict_evidence.item ->> 'path' AS path,
    CASE
        WHEN pg_catalog.jsonb_typeof(conflict_evidence.item -> 'line_start') = 'number'
            THEN conflict_evidence.item ->> 'line_start'
    END AS line_start,
    CASE
        WHEN pg_catalog.jsonb_typeof(conflict_evidence.item -> 'line_end') = 'number'
            THEN conflict_evidence.item ->> 'line_end'
    END AS line_end,
    conflict_evidence.item ->> 'symbol' AS symbol,
    conflict_evidence.item ->> 'finding' AS finding
FROM audit.events AS event
JOIN audit.executions AS execution ON execution.audit_id = event.audit_id
CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
    CASE
        WHEN pg_catalog.jsonb_typeof(event.payload -> 'conflicts') = 'array'
            THEN event.payload -> 'conflicts'
        ELSE '[]'::jsonb
    END
) WITH ORDINALITY AS conflict(item, ordinality)
CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
    CASE
        WHEN pg_catalog.jsonb_typeof(conflict.item -> 'evidence') = 'array'
            THEN conflict.item -> 'evidence'
        ELSE '[]'::jsonb
    END
) WITH ORDINALITY AS conflict_evidence(item, ordinality)
WHERE event.event_type = 'investigation.completed';

REVOKE ALL PRIVILEGES ON SCHEMA audit FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA audit_read FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA audit
    FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA audit_read
    FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA audit
    FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA audit_read
    FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA audit
    FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA audit_read
    FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;

GRANT USAGE ON SCHEMA audit TO maestro_audit_writer;
GRANT INSERT (
    audit_id,
    execution_id,
    capability,
    repository_id,
    repository_fingerprint
) ON audit.executions TO maestro_audit_writer;
GRANT INSERT (
    event_id,
    audit_id,
    sequence,
    event_type,
    event_version,
    occurred_at,
    content_hash,
    payload
) ON audit.events TO maestro_audit_writer;
GRANT SELECT (singleton, version)
    ON audit.schema_version TO maestro_audit_writer;
GRANT EXECUTE ON FUNCTION audit.verify_execution_v1(uuid, uuid, text, text, text)
    TO maestro_audit_writer;
GRANT EXECUTE ON FUNCTION audit.verify_event_v1(
    uuid,
    uuid,
    uuid,
    smallint,
    text,
    smallint,
    timestamptz,
    text,
    jsonb
) TO maestro_audit_writer;

GRANT USAGE ON SCHEMA audit_read TO maestro_audit_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA audit_read TO maestro_audit_reader;

ALTER DEFAULT PRIVILEGES FOR ROLE maestro_audit_migrator IN SCHEMA audit
    REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE maestro_audit_migrator IN SCHEMA audit
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE maestro_audit_migrator
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE maestro_audit_migrator IN SCHEMA audit
    REVOKE EXECUTE ON FUNCTIONS FROM maestro_audit_writer, maestro_audit_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE maestro_audit_migrator IN SCHEMA audit_read
    REVOKE ALL PRIVILEGES ON TABLES
    FROM PUBLIC, maestro_audit_writer, maestro_audit_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE maestro_audit_migrator IN SCHEMA audit_read
    GRANT SELECT ON TABLES TO maestro_audit_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE maestro_audit_migrator IN SCHEMA audit_read
    REVOKE EXECUTE ON FUNCTIONS FROM maestro_audit_writer, maestro_audit_reader;

UPDATE audit.schema_version SET version = 3 WHERE singleton;

COMMIT;
