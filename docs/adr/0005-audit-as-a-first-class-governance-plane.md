# ADR-0005: Audit as a First-Class Governance Plane

* **Status:** Accepted
* **Date:** 2026-08-25
* **Decision owners:** Project maintainers
* **Extends:** ADR-0004 — Separate Work Management, Audit, and Observability Planes
* **Supersedes in part:** ADR-0001 — durable storage waits for the first Job, for Audit only
* **Supersedes in part:** ADR-0002 — no persistence in the current implementation scope
* **Related:** ADR-0003 — Hardened Local Container Execution
* **Related:** ADR-0006 — Decision Authority and Human Approval

## Revision Note

This ADR was refined and accepted against the implemented Maestro v1 codebase after maintainer
review.

The refinement records approved Audit v1 decisions, replaces speculative alternatives with a
concrete design for `resolve_codebase_fact`, and incorporates the approved deployment,
credential-isolation, residual-risk, fail-closed-scope, and release-compatibility decisions. No
maintainer-authority decision remains open in this ADR.

## Context

ADR-0004 separates Maestro information into three architectural planes:

```text
Work Management
= what needs to be done

Audit
= what Maestro meaningfully decided and did

Observability
= how execution technically happened
```

Maestro v1 currently exposes only the bounded `resolve_codebase_fact` Capability. It has no
durable Jobs, external work-management integrations, Audit Store, or remote transport.

The current service already produces semantic outcomes such as:

```text
resolved
uncertain
human_decision_required
operational failure
```

and validates repository identity, repository stability, evidence references, output shape, and
result invariants before returning a result.

Structured stderr logs provide technical operational metadata, but they are ephemeral and are not
an authoritative semantic history. They cannot answer governance questions reliably after the
process exits.

The maintainers have decided to introduce Audit before the first durable Job so the governance
model can be validated against one bounded, read-only Capability before it must coordinate Job
state, external side effects, and multiple agent executions.

This is an intentional scope evolution.

This ADR supersedes ADR-0001 only where ADR-0001 defers all durable storage until the first Job,
and supersedes ADR-0002 only where ADR-0002 excludes persistence from the current implementation.
The exception is limited to the Audit governance plane. It does not rewrite either ADR's
historical decision, transfer Job state to Audit, or authorize Jobs, Checkpoints, external
integrations, additional Capabilities, remote transport, or multi-agent orchestration. The
fundamental ownership rule that Jobs own durable work state remains unchanged.

## Recorded Maintainer Decisions

The following decisions are authoritative for this refinement:

1. Audit v1 will be introduced for `resolve_codebase_fact` before durable Jobs exist.
2. Audit is a first-class governance subsystem and plane, not a Maestro Capability.
3. PostgreSQL is the Audit v1 production backend, behind an Audit port.
4. No SQLite adapter will be implemented, including for tests.
5. Application tests will use a typed fake; adapter and migration tests will use PostgreSQL.
6. pgweb is excluded from Audit v1.
7. Audit v1 is fail-closed.
8. Audit failures are typed operational errors, never semantic Capability results.
9. Audit data is bounded, sanitized, semantic, and excludes raw context, prompts, responses,
   chain-of-thought, source bodies, credentials, secrets, and unnecessary absolute paths.
10. The initial event taxonomy is deliberately small.
11. PostgreSQL uses a relational envelope with typed, versioned JSONB payloads.
12. Migration, writer, and reader database roles are separate.
13. Runtime history is append-oriented and the writer cannot update, delete, or define schema.
14. Audit v1 has no automatic retention expiry or automated backup subsystem; the local
    deployment documents `pg_dump` and `pg_restore`.
15. Maestro and its disposable Codex worker remain co-resident in the hardened Maestro container;
    PostgreSQL runs in a separate container on a private deployment network with durable storage
    and no published database port.
16. The co-resident process/network isolation limitation is an explicitly accepted Audit v1
    residual risk. Audit v1 will not add a separate worker container, broker, or Audit service
    solely to remove it.
17. Audit is a breaking release. The package/server version must receive the SemVer-compatible
    breaking increment derived from the actual current version and project policy at release
    time.
18. Fail-closed behavior applies to audited execution. Audit unavailability prevents an audited
    tool execution from proceeding or succeeding, but does not require MCP discovery or unrelated
    control-plane behavior to become unavailable.

## Decision

Maestro will maintain a structured, durable, semantic Audit Trail for every
`resolve_codebase_fact` execution that reaches the audited execution boundary.

Audit recording is an application responsibility:

```text
MCP Adapter
    |
    v
resolve_codebase_fact application service
    |
    +---- repository authorization and validation
    |
    +---- Audit application service
    |         |
    |         v
    |      AuditPort
    |         |
    |         v
    |   PostgreSQL adapter
    |
    +---- AgentRuntime
```

An AI worker never decides whether an event is audited and is never given a public Audit write
tool.

The canonical Audit Trail records semantically meaningful execution state. It does not record
every file read, search, tool call, model message, token, latency measurement, retry attempt, or
other technical trace detail.

## Ownership Boundary

Audit is a package-level governance subsystem with three distinct responsibilities:

```text
Audit domain/contracts
    immutable event identities and strict semantic payloads

Audit application service
    lifecycle policy, bounded retries, idempotency policy, and error mapping

Audit persistence adapter
    PostgreSQL transactions, SQL, migrations, and role-specific storage behavior
```

The `resolve_codebase_fact` application service depends on the Audit application boundary. It
does not import PostgreSQL types or issue SQL.

The PostgreSQL adapter depends inward on Audit contracts and implements the Audit persistence
port. The MCP adapter remains thin and does not own Audit lifecycle logic.

Audit-specific contracts belong under an Audit-owned package. They must not be added to a generic
package-wide `contracts.py`, `service.py`, or `utils.py` bucket.

## AuditPort v1 Semantics

This ADR preserves an `AuditPort` boundary but does not establish a universal
`append(event) -> None` interface.

For the current use case, the port must support two idempotent persistence operations equivalent
to:

```text
start audited execution
    atomically create immutable execution identity
    and persist execution.started

record terminal outcome
    persist exactly one of
    investigation.completed
    execution.failed
```

The exact Python method names and data classes are implementation details. The semantic contract
is:

* `start` persists the execution row and first event in one PostgreSQL transaction;
* `record terminal` persists one immutable terminal event in one PostgreSQL transaction;
* each operation is safe to retry with the same identities and content;
* neither operation silently accepts identity reuse with different content;
* successful return means the transaction committed or an identical prior commit was verified;
* the port does not manage AI execution, MCP errors, sanitization, or repository validation.

This interface is intentionally scoped to standalone audited Capability execution. A future Job
consistency design may introduce a transaction-aware unit of work or transactional outbox. Future
Jobs must not be forced through the Audit v1 self-committing interface when atomic Job-state
coordination requires a different boundary.

## Audited Execution Boundary

The v1 lifecycle is:

```text
validate bounded request
        |
authorize repository
        |
acquire execution/admission slot
        |
capture repository fingerprint
        |
construct identities and sanitized objective
        |
persist execution.started
        |
        +---- persistence fails -> no AI execution
        |
run bounded investigation or normative short-circuit
        |
validate repository, evidence, result, and size
        |
sanitize accepted semantic result
        |
persist investigation.completed
        |
        +---- persistence fails -> do not return semantic result
        |
return semantic result
```

Input validation, repository authorization, admission waiting, and initial fingerprint capture are
preflight. An invalid, unauthorized, rejected, or cancelled request that never crosses the durable
`execution.started` boundary does not create an Audit Trail. Such conditions may still be
recorded through safe structured observability or future security telemetry.

Fingerprint capture occurs before `execution.started` because the start event must identify the
repository state the investigation is authorized to examine. No AI worker starts before the start
transaction is durable.

A normative question that produces `human_decision_required` without invoking the AI runtime is
still an audited Capability execution. It receives `execution.started` followed by
`investigation.completed` with the semantic result status.

## Event Taxonomy

Audit v1 defines exactly three semantic event types:

```text
execution.started
investigation.completed
execution.failed
```

`execution.started` records the durable beginning of an authorized investigation.

`investigation.completed` records one accepted semantic result:

```text
resolved
uncertain
human_decision_required
```

`execution.failed` records a safe typed operational failure after `execution.started` was
persisted.

No separate event is required for `fact.resolved`, `fact.uncertain`, evidence validation, or a
human-decision classification in v1 because those semantics are represented by the typed
`investigation.completed` payload.

Adding another event type requires a demonstrated current use case, a versioned payload contract,
a migration/compatibility review, curated-view review, and tests.

## No Private Chain of Thought

Audit will not persist private model chain-of-thought or raw hidden reasoning.

Audit may persist a concise, externally meaningful rationale from the accepted, sanitized result.
The rationale must be bounded, factual, and connected to validated evidence where applicable.

Audit must not persist:

* system, developer, verifier, or repository prompts;
* raw model messages or responses;
* internal deliberation;
* tool transcripts;
* source-file bodies.

## Identity, Correlation, and Ordering

Each audited invocation has application-generated opaque identifiers:

```text
audit_id
execution_id
event_id
```

`audit_id` identifies the Audit Trail.

`execution_id` identifies the standalone Capability execution and is also used as the request
correlation identifier in structured observability. Future Jobs may have one Audit Trail spanning
multiple execution identifiers; v1 must not collapse the concepts even though each standalone
Trail has one execution.

`event_id` identifies one immutable semantic event and is generated once outside any retry loop.

Ordering within an Audit Trail uses an explicit positive integer `sequence`. Wall-clock
timestamps are not the ordering authority.

For the two-event v1 lifecycle:

```text
sequence 1 = execution.started
sequence 2 = investigation.completed or execution.failed
```

Application timestamps record when the semantic event occurred. A separate database-generated
timestamp records when PostgreSQL accepted it.

## Audit-Safe Data

Audit v1 stores only data needed to understand and govern the bounded execution:

* Audit, execution, and event identity;
* event ordering, type, and version;
* semantic and persistence timestamps;
* Capability identity;
* private repository identity;
* repository fingerprint;
* bounded sanitized objective;
* semantic result status;
* sanitized answer where the result permits one;
* confidence as evidence strength;
* concise sanitized rationale;
* validated repository-relative evidence references;
* sanitized conflicts and their validated evidence references;
* safe typed operational error code and lifecycle stage for failures;
* server, runtime, model, and prompt-policy versions relevant to the result.

Caller `context` is not persisted in Audit v1.

The stored objective is the bounded neutral investigation objective after deterministic
normalization and Audit-specific sanitization. It is not the raw caller request. Absolute
repository roots, recognizable credentials/secrets, control characters, and other disallowed
content must be removed or replaced before persistence.

Only the final result that passed Pydantic validation, semantic invariants, repository-stability
validation, deterministic evidence validation, sanitization, and configured size limits may be
used to construct `investigation.completed`.

All Audit event payload models use Pydantic v2 strict validation, forbid unexpected fields, and
apply explicit item and byte bounds. PostgreSQL writes use bound parameters; untrusted payload
values never become SQL identifiers or SQL fragments.

## PostgreSQL Storage Model

PostgreSQL is the Audit v1 production backend.

PostgreSQL remains an infrastructure adapter. Audit application and domain code must not depend on
PostgreSQL, JSONB operators, SQL, a PostgreSQL driver, or deployment topology.

Audit v1 uses a hybrid relational model:

```text
audit.executions
    immutable execution/trail identity and correlation

audit.events
    immutable relational event envelope
    + typed/versioned JSONB semantic payload
```

### `audit.executions`

The execution table contains immutable fields equivalent to:

```text
audit_id                 UUID primary key
execution_id             UUID unique, not null
capability               bounded text, not null
repository_id            bounded private identifier, not null
repository_fingerprint   bounded digest, not null
created_at               timestamptz, database generated
```

It contains no mutable status column. Current status is derived from events.

### `audit.events`

The event table contains a relational envelope equivalent to:

```text
event_id        UUID primary key
audit_id        UUID foreign key, not null
sequence        positive integer, not null
event_type      bounded text, not null
event_version   positive integer, not null
occurred_at     timestamptz, not null
persisted_at    timestamptz, database generated
content_hash    fixed-format digest, not null
payload         JSONB object, not null
```

Required database constraints include:

* unique `(audit_id, sequence)`;
* v1 event-type allowlisting;
* positive sequence and version values;
* JSONB object shape at the database boundary;
* referential integrity from events to executions;
* bounded/fixed-format identity and digest constraints where practical.

The relational envelope supports stable correlation and common filtering. JSONB avoids a broad
nullable table or speculative subtype tables while event-specific payloads are still evolving.
Frequently queried stable fields must be promoted to relational columns only when demonstrated
queries justify it.

JSONB is not permission to persist arbitrary dictionaries. Each `(event_type, event_version)`
has one strict typed application schema.

## Event Payloads

The v1 payloads are semantically equivalent to the following.

### `execution.started` version 1

```text
objective
server_version
runtime_name
runtime_version
model
prompt_policy_version
```

Repository identity and fingerprint remain in the relational execution record rather than being
duplicated in every payload.

### `investigation.completed` version 1

```text
status
answer
confidence
rationale
evidence[]
conflicts[]
server_version
runtime_name
runtime_version
model
prompt_policy_version
```

Evidence references use normalized repository-relative paths, validated line ranges, optional
symbols, and bounded sanitized findings. Conflicts preserve the current public semantic model and
contain only validated evidence references.

### `execution.failed` version 1

```text
error_code
failure_stage
server_version
runtime_name
runtime_version
model
prompt_policy_version
```

The failure payload does not contain a raw exception, traceback, database message, prompt, model
response, or untrusted absolute path.

## Transaction Boundaries

Audit v1 uses short PostgreSQL transactions and never holds a database transaction open while an
AI worker runs.

### Start transaction

One transaction atomically inserts:

```text
audit.executions row
+
execution.started event
```

If this transaction cannot be durably committed or an identical prior commit cannot be verified,
the AI investigation does not start.

### Investigation

The application performs the bounded investigation outside a database transaction.

This avoids holding locks or database resources across model latency, admission delays, timeout,
or cancellation.

### Terminal transaction

One transaction inserts exactly one terminal event:

```text
investigation.completed
or
execution.failed
```

No execution row is updated. Curated views derive completeness and outcome from the immutable
events.

A semantic Capability result may be returned only after `investigation.completed` has committed
or an identical prior commit has been verified.

These boundaries do not solve future consistency between Job state, Git commits, pull requests,
issue updates, deployments, or other external side effects. Those cases require a later Job
consistency/outbox decision.

## Idempotency

Every event identity and canonical content hash is generated once before persistence retries.

The canonical hash covers the immutable event envelope fields supplied by the application and the
canonical typed payload representation. Database-generated persistence time is excluded.

Persistence follows these rules:

* a new identity and sequence are inserted normally;
* retrying an identical event is successful;
* reusing an event identity with different content is an integrity failure;
* reusing `(audit_id, sequence)` for a different event is an integrity failure;
* retry code must distinguish a verified duplicate from an unverified conflict;
* `ON CONFLICT DO NOTHING` alone is insufficient because it cannot establish content identity;
* application-generated identities are never regenerated inside a retry loop.

Writes use a bounded maximum attempt count and a bounded total persistence deadline. Only failures
classified as transient are retried. Validation, authorization, schema-version, constraint, and
content-mismatch failures fail immediately.

Exact retry timing is an operational implementation value, but the initial policy must be fixed,
small, deterministic under tests, centrally configured, and included in observability metadata.

## Fail-Closed Semantics

Audit errors are operational errors. They are never represented as `resolved`, `uncertain`, or
`human_decision_required`.

Audit v1 exposes safe typed errors equivalent to:

```text
AUDIT_UNAVAILABLE
AUDIT_PERSISTENCE_ERROR
```

`AUDIT_UNAVAILABLE` represents exhausted bounded attempts caused by connection failure, timeout,
or storage unavailability.

`AUDIT_PERSISTENCE_ERROR` represents non-transient persistence failures such as an incompatible
schema, violated invariant, authorization failure, ambiguous/mismatched idempotency state, or a
write that cannot satisfy the durable Audit contract.

Failure behavior is:

| Situation | Public behavior | Durable state |
|---|---|---|
| Invalid or unauthorized preflight | Existing typed operational error | No Audit Trail |
| Start write fails | Audit operational error; AI does not start | None, or an identical start verified before proceeding |
| Investigation succeeds and completion persists | Return semantic result | Complete Trail |
| Investigation succeeds and completion cannot be established | Audit operational error; do not return result | Start-only or complete-but-unacknowledged Trail |
| Investigation fails and failure event persists | Return original typed operational error | Complete failed Trail |
| Investigation fails and failure event cannot be established | Audit operational error takes precedence | Start-only or complete-but-unacknowledged Trail |

Audit persistence failures are logged safely without recursively attempting to audit the Audit
failure itself.

Returning an Audit error after an ambiguous commit may produce a conservative false negative: the
terminal event may exist even though the caller receives an error. This is preferable to returning
an unaudited semantic success. A repeated write with the same identity can later verify the commit.

## Cancellation, Process Failure, and Incomplete Trails

Cooperative cancellation after `execution.started` should attempt to persist
`execution.failed` with the existing `AGENT_CANCELLED` category after owned worker cleanup. The
attempt must be bounded and must not swallow cancellation or leave an orphan persistence task.

No in-process design can guarantee a terminal event after abrupt process termination, host loss,
database loss, or forced container removal.

Therefore:

* a durable start without a terminal event explicitly means incomplete execution;
* no semantic success was returned for such an execution;
* curated views surface incomplete Trails directly;
* v1 does not invent a terminal outcome during startup;
* automated reconciliation is deferred until durable Jobs or an operational requirement justify
  it.

This limitation must be documented honestly. Fail-closed means no successful result without a
durable terminal event; it does not mean a process can guarantee a terminal event after total
failure.

## PostgreSQL Roles and Append Enforcement

Audit v1 uses distinct database roles:

```text
migration role
    owns Audit schemas and objects
    applies versioned migrations and grants
    is unavailable to normal Maestro runtime

writer role
    used by normal Maestro runtime
    inserts executions and events
    has only the narrow read privileges needed to verify idempotent duplicates
    cannot UPDATE, DELETE, TRUNCATE, ALTER, CREATE, DROP, or grant privileges

reader role
    SELECT-only access to curated read views
    no mutation, DDL, or writer-role membership
```

PostgreSQL supports separate `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `CREATE`,
and schema privileges, so this separation is feasible without relying on application convention.

The migration must revoke unnecessary `PUBLIC` privileges and grant only the required schema,
table, column, and view access.

The writer may require narrow `SELECT` access to identity and content-hash fields so a duplicate
can be verified. It must not receive broad reader access merely for implementation convenience.
If a security-definer function is proposed instead, its search path, ownership, executable grants,
SQL-injection surface, and privilege escalation behavior require explicit review and negative
tests. It is not the default design.

Database ownership remains administratively capable of changing data. Role separation provides
least privilege for Maestro and human readers; it is not cryptographic tamper evidence and must
not be described as such.

## Migrations and Schema Compatibility

Audit schema changes use ordered, versioned migrations committed with the application.

Required migration behavior is:

* migrations run as an explicit administrative/deployment action using the migration role;
* normal Maestro startup never acquires DDL authority or silently migrates the database;
* the adapter verifies that the configured schema version is supported before an audited operation
  starts and after reconnect where needed;
* each migration is transactional where PostgreSQL permits;
* migrations preserve existing canonical events unless an explicitly reviewed data migration is
  required;
* automatic destructive down-migration is not a recovery strategy;
* grants and curated views are migration-controlled and tested;
* clean installation and forward upgrade from every supported prior Audit schema are tested.

The exact migration library or SQL runner is an implementation choice, not a domain invariant. It
must be justified against dependency policy and must not require the runtime writer to own schema.

Event payload version and PostgreSQL schema version are separate concepts:

```text
event_version
= interpretation of one event payload

database schema version
= physical storage and view compatibility
```

## Human-Readable Read Model

Audit v1 provides curated read-only SQL views instead of pgweb, an Audit UI, or public MCP Audit
query tools.

The initial read schema exposes views equivalent to:

```text
audit_read.execution_summary
audit_read.event_timeline
audit_read.evidence
```

### `execution_summary`

One row per audited execution containing:

* Audit and execution identifiers;
* Capability and private repository identifier;
* start and terminal timestamps;
* sanitized objective;
* semantic outcome or safe error code;
* confidence and concise rationale where applicable;
* evidence/conflict counts;
* an explicit `is_incomplete` value.

### `event_timeline`

One row per semantic event ordered by `audit_id` and `sequence`, exposing bounded human-readable
fields without requiring users to reconstruct basic history from JSON operators.

### `evidence`

One row per validated repository-relative evidence reference, including whether it supports the
primary result or a conflict.

The reader role receives `SELECT` on curated views and no mutation privileges. Direct base-table
access is not required for the normal human inspection role.

Views must remain useful with a normal PostgreSQL client and support at least:

```text
show one Audit Trail by audit_id
show one execution by execution_id
show Audits for a private repository identity
show resolved, uncertain, human-decision, failed, or incomplete executions
show validated evidence and conflicts for an execution
```

pgweb remains a likely follow-up operational surface. Adding it requires a separate review of
authentication, network exposure, reader credentials, container topology, and security claims.

## Retention, Correction, and Deletion

Audit v1 has no automatic expiration policy.

Normal runtime behavior is append-oriented:

* the writer cannot update or delete historical events;
* semantic corrections are represented by later versioned events when a supported correction use
  case exists;
* Audit v1 does not yet add a correction event because the current execution lifecycle does not
  require one;
* administrative deletion or legal/privacy handling remains outside normal Maestro runtime;
* retention and administrative deletion must not be implemented through writer privileges.

Append-oriented does not mean legally or physically undeletable. Database owners and storage
administrators retain administrative authority and responsibility.

## Backup and Restore

Audit v1 does not implement an automated backup subsystem.

The local deployment must use durable PostgreSQL storage and document operator-controlled
`pg_dump` and `pg_restore` procedures, including:

* which database/schema is included;
* how credentials are supplied safely;
* how a restore is verified;
* how schema/application compatibility is checked after restore;
* that an untested dump is not evidence of recoverability.

Automated schedules, off-host replication, point-in-time recovery, encryption/key management, and
retention automation are outside Audit v1.

## Availability and Startup

Audit is mandatory configuration once this ADR is implemented. Invalid or missing typed Audit
configuration fails at startup.

Database connectivity, writer-role authorization, and supported schema version must be established
before each audited execution crosses the durable `execution.started` boundary. A transient
database outage does not need to make MCP discovery unavailable; a tool call remains fail-closed
and returns the applicable typed Audit operational error without starting the AI worker.

Fail-closed applies to the audited execution lifecycle, not indiscriminately to every MCP or
control-plane operation. Audit unavailability must prevent `resolve_codebase_fact` from starting
AI work or returning semantic success. It does not require tool discovery, protocol negotiation,
health diagnostics that do not claim audited readiness, or unrelated future control-plane
behavior to depend on a successful Audit write.

Normal startup and request handling never acquire migration authority or apply schema changes.

An Audit outage must not be converted into observability-only operation or an in-memory buffer.
Audit v1 has no disk spool, background delivery queue, or best-effort mode because those mechanisms
would create additional durability, ordering, shutdown, and credential semantics.

## Local Deployment and Credential Isolation

Audit v1 extends the hardened local deployment with a separate PostgreSQL container:

```text
local deployment boundary
    |
    +-- Maestro container
    |      Maestro parent and disposable Codex worker remain co-resident
    |      ADR-0003 non-root/read-only/capability/resource controls remain
    |      Audit writer credential is parent application configuration
    |      worker environment explicitly excludes all Audit configuration
    |
    +-- PostgreSQL container
           private user-defined deployment network
           durable named volume
           no database port published by the hardened deployment
           distinct migration, append-writer, and query-reader credentials
```

PostgreSQL is the only component with writable durable database storage. The Maestro container
retains ADR-0003's read-only root and repository mounts, ephemeral bounded temporary state,
non-root execution, dropped capabilities, no-new-privileges, confinement, and absence of public
service ports. Audit therefore extends ADR-0003's deployment topology without moving deployment
or storage concerns into the Maestro domain.

ADR-0003 rejected Docker Compose because one local stdio process required no multi-container
orchestration. PostgreSQL creates that missing requirement. A supported multi-container launcher
or Compose definition is now permitted as a deployment-adapter implementation choice, provided
it preserves ADR-0003's security profile and this ADR's private-network, credential, and storage
constraints. This does not rewrite the historical ADR-0003 rationale.

The normal Maestro runtime receives only the append-writer credential. Migration, human query,
dump, and restore commands use explicit short-lived invocations with their separately scoped
credentials. The hardened deployment does not expose PostgreSQL publicly. Native integration
tests may use an isolated temporary PostgreSQL instance reachable only through an explicitly
local test boundary; that test mechanism is not a supported production exposure.

The disposable Codex worker must never receive Audit credentials in its environment, command
arguments, temporary `CODEX_HOME`, prompt, repository, logs, Audit payload, or provider request.
Worker process creation must retain the explicit allowlisted environment, inherit no Audit
connection or credential file descriptor, and use close-on-exec/closed-descriptor behavior
supported by the current subprocess architecture. Credential-bearing parent objects must remain
outside prompt construction and worker configuration.

These controls reduce accidental disclosure; they do not create a complete credential or network
security boundary between co-resident parent and worker processes. They share the Maestro
container's OS and network namespace, and stronger isolation would require a different execution
architecture. The maintainers explicitly accept that residual risk for Audit v1. Audit v1 will
not introduce a separate worker container, broker, or Audit service solely to remove it. Security
documentation must state this limitation without overstating environment or file-descriptor
filtering as isolation.

## Security and Privacy

Audit introduces a new persistence, credential, and network trust boundary.

Before implementation is complete, the threat model, `SECURITY.md`, README, configuration
documentation, container documentation, and residual-risk claims must cover:

* PostgreSQL writer, migration, and reader credentials;
* credential delivery and redaction;
* explicit exclusion of Audit credentials from the Codex worker environment;
* database network reachability;
* SQL injection and malformed JSON payloads;
* untrusted model text persisted after validation and sanitization;
* unauthorized reads and writes;
* event forgery, mutation, deletion, and identity collision;
* database outage, disk exhaustion, and partial/ambiguous commit;
* sensitive repository identity, objective, rationale, evidence, and conflict metadata;
* database dumps and restored copies;
* incomplete Trails after cancellation or process failure.

Database credentials must not be copied into the worker's minimal environment, temporary
`CODEX_HOME`, prompt, repository, logs, Audit payload, or model-provider request.

The worker and stored model-derived strings remain untrusted even after sanitization. Audit readers
must render them as data and must not execute embedded markup, links, commands, or SQL.

## Observability Correlation

Audit and Observability remain separate.

The same `execution_id` correlates the semantic Trail with structured logs. Observability may also
record duration, queue duration, retry count, database latency, model/tool timing, and technical
exceptions.

Audit records the semantic result and safe error category. It does not duplicate low-level retry,
SQL, model, or tool traces.

Audit must remain useful if observability data expires or is unavailable.

## Public Contract and Error Mapping

Audit does not add a public MCP Capability or Audit write tool.

The existing `resolve_codebase_fact` input and semantic result schemas remain unchanged. Its
operational error set expands with safe Audit error categories.

Audit adapter exceptions and database messages must never escape directly through MCP. They map to
typed application errors and safe protocol errors without stack traces, SQL, hostnames, usernames,
or credentials.

Schema snapshots, MCP contract tests, documentation, real stdio MCP Inspector validation, and
SemVer review are required because externally observable error and deployment behavior changes.

Audit is approved as a breaking release because an existing deployment lacks the mandatory Audit
Store configuration and because audited execution gains new externally observable operational
failure behavior. The release must apply the SemVer-compatible breaking increment derived from
the package/server version that is actually current when the change ships. At ADR acceptance the
repository version is `1.0.0`, so an immediate breaking release from that version is `2.0.0`.
This is a policy-derived result, not an unconditional hard-coded target if the current version
changes before release. All version declarations, package metadata, documentation, images, and
contract snapshots must remain consistent.

## Test Strategy

Testing is part of the Audit design.

### Application tests

Application tests use a typed fake Audit port/service and no SQLite adapter. They verify:

* start is durable before the fake AI runtime is invoked;
* start failure prevents AI invocation;
* all three semantic result statuses produce `investigation.completed`;
* accepted results are validated and sanitized before Audit persistence;
* successful results are withheld when terminal persistence fails;
* operational failures produce `execution.failed`;
* dual operation/Audit failure follows documented error precedence;
* retry identities remain stable;
* retry count and deadline are bounded;
* cancellation cleanup is bounded and leaves no orphan task;
* raw context, prompts, responses, source bodies, secrets, and absolute paths are absent;
* Audit errors never become semantic Capability statuses.

### PostgreSQL integration tests

Adapter, migration, and view tests use a real supported PostgreSQL instance. They verify:

* clean migration and schema-version checks;
* forward migration from every supported prior Audit schema;
* atomic execution-plus-start insertion;
* atomic terminal insertion;
* event and sequence uniqueness;
* identical retry success and mismatched retry rejection;
* transaction rollback on partial failure;
* concurrent duplicate writes;
* JSONB/object, event-type, version, identity, digest, and foreign-key constraints;
* writer INSERT and narrow idempotency-read behavior;
* writer rejection of UPDATE, DELETE, TRUNCATE, DDL, and broad reads;
* reader SELECT on curated views and rejection of mutation/base-table access;
* migration-role ownership and runtime-role separation;
* curated summary, timeline, evidence, conflict, failure, and incomplete views;
* database unavailability and incompatible-schema mapping without leaking internals.

SQLite must not substitute for these tests because doing so would leave PostgreSQL transactions,
constraints, JSONB, SQL, migrations, and grants unvalidated.

### Security and deployment tests

Tests must verify that:

* Audit credentials are absent from worker environment and isolated worker configuration;
* secrets and disallowed content are removed before persistence;
* SQL uses bound parameters;
* role grants fail closed;
* database logs and application logs do not expose credentials or payloads;
* container/network/storage changes preserve approved security controls;
* dump/restore documentation is exercised as a smoke procedure appropriate to the deployment.

Python implementation changes require the full deterministic gate. Dependency, packaging,
container, MCP boundary, and security-boundary changes require their additional project-standard
gates. Live AI evals are required only if verifier prompt/model/runtime/evidence behavior changes;
deterministic Audit tests do not imply live model validation.

## Rejected Alternatives

### Use structured logs as Audit

Rejected because logs have different semantics, retention, failure policy, and consumers.

### Let the AI worker write Audit events

Rejected because optional worker behavior is not a governance control and would expose the Audit
write surface to untrusted execution.

### SQLite production or test adapter

Rejected by maintainer decision. It would not validate PostgreSQL behavior and would add a second
storage contract without a supported operational use case.

### pgweb in Audit v1

Rejected for the initial implementation. Curated views and a SELECT-only role satisfy the first
read requirement without introducing a web/network/authentication surface.

### Hold one transaction across AI execution

Rejected because model execution is long-running, cancellable, and operationally independent from
a short database transaction.

### Best-effort or buffered Audit

Rejected for v1 because it violates fail-closed semantics and introduces another durable queue,
ordering, retry, and shutdown subsystem.

### Generic public `record_audit` tool

Rejected because canonical Audit coverage is application-owned.

### Transactional outbox in Audit v1

Rejected as premature. There is no durable Job state or external side effect to coordinate in the
current use case.

## Non-Goals

Audit v1 does not implement:

* durable Jobs or Job-state persistence;
* Checkpoints;
* work-management integrations;
* GitHub, Jira, deployment, or other external side effects;
* transactional outbox or distributed transactions;
* multiple Maestro writers across a distributed deployment guarantee;
* public Audit read or write MCP tools;
* pgweb or a dedicated Audit UI;
* summary generation by an AI model;
* SQLite;
* automated retention, deletion, backup, replication, or point-in-time recovery;
* cryptographic tamper evidence;
* multi-tenancy or remote authentication;
* observability backend selection;
* private chain-of-thought or raw transcript storage.

## Accepted Architectural Invariants

1. Audit is a first-class governance subsystem and plane, not a Capability.
2. Audit is separate from Work Management and Observability.
3. `resolve_codebase_fact` is audited automatically by application behavior.
4. AI workers do not decide whether an event is audited and receive no Audit write tool.
5. PostgreSQL is the Audit v1 production adapter, not a domain dependency.
6. No SQLite adapter exists; application tests use a typed fake and storage tests use PostgreSQL.
7. Audit v1 is fail-closed and exposes typed operational errors.
8. No semantic success is returned without a durable `investigation.completed` event.
9. The initial taxonomy is `execution.started`, `investigation.completed`, and
   `execution.failed`.
10. Audit data is bounded, sanitized, semantic, typed, and versioned.
11. Canonical history is append-oriented and normally immutable to the runtime writer.
12. Identity and per-Trail ordering are explicit and idempotent.
13. Storage uses immutable relational envelopes plus strict versioned JSONB payloads.
14. Start and terminal persistence use separate short transactions; AI work occurs outside a
    database transaction.
15. The AuditPort v1 lifecycle contract does not constrain future Job unit-of-work/outbox design.
16. Migration, writer, and reader roles are distinct; the writer has no UPDATE, DELETE, TRUNCATE,
    DDL, or broad read authority.
17. Human inspection uses curated SELECT-only PostgreSQL views in v1.
18. pgweb is a follow-up operational option, not an Audit v1 dependency or contract.
19. Runtime has no automatic retention expiry or administrative deletion authority.
20. Backup is initially operator-managed through documented `pg_dump`/`pg_restore` procedures.
21. Audit credentials, raw prompts/responses/context, source bodies, secrets, and chain-of-thought
    are excluded from worker context and Audit data.
22. Future external side-effecting Jobs require a separate consistency/outbox design.
23. Fail-closed behavior governs audited execution without requiring unrelated MCP control-plane
    operations to depend on Audit availability.
24. PostgreSQL is a separate private-network container with durable storage and no published
    database port in the hardened deployment.
25. The disposable Codex worker remains co-resident with Maestro but receives no Audit credentials
    or inherited Audit descriptors; the resulting shared-process/network-boundary risk is accepted
    and documented for v1.
26. Audit ships as the SemVer-compatible breaking increment from the actual current package/server
    version under the project's versioning policy.

## PostgreSQL References

The storage and role design was checked against current official PostgreSQL documentation:

* [Transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html)
* [Privileges](https://www.postgresql.org/docs/current/ddl-priv.html)
* [INSERT privilege and conflict behavior](https://www.postgresql.org/docs/current/sql-insert.html)
* [JSON and JSONB types](https://www.postgresql.org/docs/current/datatype-json.html)
* [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html)
* [pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html)

## Expected Next Step

```text
1. Create the concrete Audit v1 implementation plan/PRD.
2. Review and explicitly approve that plan.
3. Implement only after plan approval.
```

The implementation plan must map the accepted architecture to:

* Audit package ownership and typed contracts;
* lifecycle integration in `resolve_codebase_fact`;
* safe objective/result sanitization;
* PostgreSQL driver and migration mechanism;
* exact schema, constraints, grants, and views;
* typed settings and credential loading;
* retry/deadline values and error mapping;
* native and hardened local deployment;
* deterministic, PostgreSQL, security, packaging, and MCP validation;
* README, security, threat-model, container, operations, backup, and recovery documentation.

## Consequences

### Positive

* Governance behavior is validated against a bounded read-only Capability before durable Jobs.
* Semantic outcomes survive process exit independently from logs and traces.
* No successful Capability result can be returned without its durable terminal Audit event.
* PostgreSQL roles enforce meaningful separation between migration, append, and read access.
* A small taxonomy and typed payloads limit noise and speculative modeling.
* Explicit incomplete Trails expose crash/cancellation gaps without inventing outcomes.
* The AuditPort boundary preserves future Job consistency design freedom.
* Curated views provide immediate human inspection without a new web application.

### Negative

* Maestro gains a mandatory stateful service before durable Jobs exist.
* Capability execution availability now depends on PostgreSQL, and startup requires valid Audit
  configuration.
* Local deployment, credentials, migrations, backup, and recovery become more complex.
* Audit metadata creates a new sensitive-data and retention surface.
* Fail-closed terminal writes may convert an otherwise valid investigation into an operational
  error.
* A committed-but-unacknowledged terminal write can produce a conservative false-negative response.
* The current co-resident worker model retains the accepted credential/process/network-isolation
  residual risk.
* PostgreSQL integration and security gates increase test and CI cost.

## Decision Summary

This ADR accepts the following Audit v1 model:

```text
resolve_codebase_fact
        |
        +---- durable execution.started before AI
        |
        +---- bounded investigation and deterministic validation
        |
        +---- durable investigation.completed before semantic success
        |          or
        +---- durable execution.failed for operational failure
                         |
                         v
                     AuditPort
                         |
                         v
                  PostgreSQL adapter
                         |
              +----------+----------+
              |                     |
              v                     v
      append-only runtime       curated SELECT views
```

The design is concrete for the current bounded Capability, deliberately excludes future Job
consistency machinery, and records the approved deployment, credential-isolation residual risk,
fail-closed scope, and breaking-release strategy. Implementation remains gated on review and
explicit approval of the Audit v1 implementation plan/PRD.
