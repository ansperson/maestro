BEGIN;

DO $bootstrap$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname IN (
            'maestro_audit_migrator',
            'maestro_audit_writer',
            'maestro_audit_reader'
        )
    ) THEN
        RAISE EXCEPTION 'Maestro Audit bootstrap requires unused role names';
    END IF;

    CREATE ROLE maestro_audit_migrator
        LOGIN PASSWORD NULL
        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    CREATE ROLE maestro_audit_writer
        LOGIN PASSWORD NULL
        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    CREATE ROLE maestro_audit_reader
        LOGIN PASSWORD NULL
        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
END
$bootstrap$;

ALTER ROLE maestro_audit_migrator SET search_path = pg_catalog;
ALTER ROLE maestro_audit_writer SET search_path = pg_catalog;
ALTER ROLE maestro_audit_reader SET search_path = pg_catalog, audit_read;

DO $database_privileges$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC',
        pg_catalog.current_database()
    );
    EXECUTE pg_catalog.format(
        'GRANT CONNECT, CREATE ON DATABASE %I TO maestro_audit_migrator',
        pg_catalog.current_database()
    );
    EXECUTE pg_catalog.format(
        'GRANT CONNECT ON DATABASE %I TO maestro_audit_writer, maestro_audit_reader',
        pg_catalog.current_database()
    );
END
$database_privileges$;

REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;

DO $forward_ownership$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'audit'
    ) THEN
        ALTER SCHEMA audit OWNER TO maestro_audit_migrator;
        ALTER TABLE IF EXISTS audit.schema_version OWNER TO maestro_audit_migrator;
        ALTER TABLE IF EXISTS audit.executions OWNER TO maestro_audit_migrator;
        ALTER TABLE IF EXISTS audit.events OWNER TO maestro_audit_migrator;
    END IF;
END
$forward_ownership$;

COMMIT;
