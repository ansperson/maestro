# Audit PostgreSQL roles, migrations, and read views

Maestro Audit uses one dedicated PostgreSQL database and three fixed login-role identities:

| Role | Purpose | Effective access |
|---|---|---|
| `maestro_audit_migrator` | deployment-only migration owner | owns Audit objects; creates and changes schemas; no superuser, database creation, or role-management authority |
| `maestro_audit_writer` | normal Maestro runtime | inserts only application-supplied Audit columns, reads the schema version, and executes two exact duplicate-verification functions |
| `maestro_audit_reader` | human/query client | selects curated `audit_read` views only |

The one-time administrator bootstrap creates these roles with `LOGIN` but with a null password.
It deliberately contains no default credential. Provision each credential afterward using the
operator's secret-management procedure and configure PostgreSQL client authentication to require
it. Credential generation, rotation, and distribution belong to the deployment work, not these
schema resources.

The fixed identities make a role-name collision a hard bootstrap failure. Audit v1 therefore
expects its approved dedicated PostgreSQL container/database rather than sharing one cluster among
independently bootstrapped Maestro installations.

In the supported two-container deployment, `scripts/maestro_compose.py bootstrap` and
`scripts/maestro_compose.py migrate` perform the steps below as short-lived containers, each
receiving only the credentials its role requires. Use the manual commands below when operating
PostgreSQL outside that deployment. See [`container.md`](container.md) for the deployment.

## Clean installation

Run these commands from a trusted Maestro checkout. `ADMIN_DSN` must identify the PostgreSQL
cluster administrator only for the one-time bootstrap; `MIGRATOR_DSN` must identify
`maestro_audit_migrator` after its external credential has been provisioned.

```bash
psql -X --set ON_ERROR_STOP=1 --dbname "$ADMIN_DSN" \
  --file src/maestro/audit/postgres/migrations/bootstrap_roles.sql
psql -X --set ON_ERROR_STOP=1 --dbname "$MIGRATOR_DSN" \
  --file src/maestro/audit/postgres/migrations/0001_audit_tracer.sql
psql -X --set ON_ERROR_STOP=1 --dbname "$MIGRATOR_DSN" \
  --file src/maestro/audit/postgres/migrations/0002_execution_failed.sql
psql -X --set ON_ERROR_STOP=1 --dbname "$MIGRATOR_DSN" \
  --file src/maestro/audit/postgres/migrations/0003_roles_and_read_views.sql

psql "$MIGRATOR_DSN" --set ON_ERROR_STOP=1 \
  --file src/maestro/audit/postgres/migrations/0004_authority_applied.sql
```

The bootstrap revokes `PUBLIC` database and `public`-schema privileges. It grants database
`CONNECT` and `CREATE` only to the migrator, and grants only `CONNECT` to the writer and reader.
Consequently, writer and reader sessions cannot create schemas or temporary tables. The v3
migration owns all Audit objects as the migrator, grants no role memberships, installs restrictive
default privileges, and grants each runtime privilege explicitly.

Normal Maestro startup accepts only the writer's typed host, port, database, user, and owner-only
password-file settings; it rejects DSNs and ambient libpq connection variables and never applies
bootstrap or migration SQL. Keep administrator, migrator, writer, and reader connection material
separate. The `ADMIN_DSN` and `MIGRATOR_DSN` examples above are operator-only `psql` inputs, not
Maestro application configuration.

## Forward upgrade from schema v3

For an existing v3 Audit database, run only `0004_authority_applied.sql` as the migrator. It
refuses any schema version other than v3 and refuses to run as another role.

The v4 migration admits the `decision_authority` capability and the `authority.applied` event
type, replaces `audit_read.execution_summary` so an applied decision reports as
`authority_applied`, and adds `audit_read.applied_authority`. It grants the reader `SELECT` on
the new view and changes no writer privilege: the writer stays append-only over the same two
tables and the same two verification functions.

## Forward upgrade from schema v2

For an existing v2 Audit database, run the administrator bootstrap first. It transfers the known
v2 `audit` schema and tables to the migrator without changing stored events. Provision the new role
credentials, then run only `0003_roles_and_read_views.sql` as the migrator. The migration refuses
any schema version other than v2 and refuses to run as another role.

Migrations are forward-only, transactional, and explicitly operator-run. Do not retry a completed
bootstrap or migration mechanically: inspect the schema version and the original failure first.

## Curated reader queries

The reader has no access to `audit.executions`, `audit.events`, the JSONB payload column, content
hashes, fingerprints, or duplicate-verification functions. It can query:

```sql
SELECT *
FROM audit_read.execution_summary
WHERE audit_id = '00000000-0000-0000-0000-000000000001';

SELECT *
FROM audit_read.event_timeline
WHERE execution_id = '00000000-0000-0000-0000-000000000002'
ORDER BY sequence;

SELECT *
FROM audit_read.evidence
WHERE repository_id = '0123456789abcdef'
ORDER BY audit_id, evidence_scope, conflict_ordinal, evidence_ordinal;

SELECT *
FROM audit_read.applied_authority
WHERE work_item = '26'
ORDER BY occurred_at DESC;
```

`execution_summary` reports `resolved`, `uncertain`, `human_decision_required`, `authority_applied`,
`failed`, or `incomplete` without requiring JSON operators. `event_timeline` exposes event identity, order, and
approved semantic fields. `evidence` expands primary and conflict evidence into repository-relative
references. `applied_authority` reads back the decision or written rule an execution applied,
with its subject, decided value, scope, validity, approver, origin, and a digest of the entry
as it stood. That content was captured at application time rather than referenced, so editing
the work item afterwards changes the item and never this row; a digest that no longer matches
the current block shows the entry has since changed, without saying which version was right. None of the views exposes the raw JSONB payload or database persistence timestamps.

The views are operational query surfaces, not a public Maestro Capability or tamper-evidence
mechanism. A database owner remains administratively capable of changing stored data.
