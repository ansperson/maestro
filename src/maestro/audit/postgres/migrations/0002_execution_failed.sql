BEGIN;

DO $$
BEGIN
    IF (SELECT version FROM audit.schema_version WHERE singleton) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'Audit schema migration 0002 requires version 1';
    END IF;
END
$$;

ALTER TABLE audit.events
    DROP CONSTRAINT audit_events_v1_shape;

ALTER TABLE audit.events
    ADD CONSTRAINT audit_events_v1_shape CHECK (
        (sequence = 1 AND event_type = 'execution.started')
        OR (
            sequence = 2
            AND event_type IN ('investigation.completed', 'execution.failed')
        )
    );

UPDATE audit.schema_version SET version = 2 WHERE singleton;

COMMIT;
