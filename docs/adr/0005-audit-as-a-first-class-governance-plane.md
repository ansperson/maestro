# ADR-0005: Audit as a First-Class Governance Plane

* **Status:** Proposed
* **Date:** 2026-08-25
* **Decision owners:** Project maintainers
* **Extends:** ADR-0004 — Separate Work Management, Audit, and Observability Planes
* **Related:** ADR-0001 — Maestro as an Engineering Execution Platform

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

As Maestro becomes capable of increasingly autonomous engineering work, important decisions and outcomes will occur without direct human participation at every step.

Examples include:

* resolving repository facts;
* selecting or rejecting an implementation approach;
* pausing for a human decision;
* applying an approved decision;
* creating or modifying artifacts;
* validating changes;
* rejecting work after independent validation;
* interacting with external systems;
* completing or failing a Job.

Today, a human can often understand an agent's work because the human is manually coordinating agent sessions.

That model does not scale to Maestro Jobs.

For example:

```text
Issue
  ↓
Grill Agent
  ↓
Implementation Agent
  ↓
PR Review Agent
  ↓
Address Agent
  ↓
Independent Validator
  ↓
Complete
```

may involve several independent agent executions over minutes, hours, or days.

The user should not need to inspect raw logs, model transcripts, Jira comments, or low-level traces to answer:

> What happened?

> What decisions did Maestro make?

> Why were those decisions made?

> What evidence was used?

> Which human decisions affected execution?

> What external actions occurred?

> What validation established the final outcome?

Maestro therefore requires Audit as a first-class governance capability.

## Proposed Decision

Maestro will maintain a structured, durable, semantic Audit Trail for meaningful engineering execution.

Audit recording will be an **application responsibility**.

It must not depend on an AI worker remembering to call an `audit` tool.

Conceptually:

```text
Maestro Application
        |
        +---- business operation
        |
        +---- AuditPort.record(...)
```

rather than:

```text
AI Worker
   |
   +---- maybe remembers to call audit()
```

The canonical Audit Trail will contain semantically meaningful events representing important decisions, actions, checkpoints, validations, and outcomes.

Audit is not intended to record every technical execution detail.

## AuditPort

Audit persistence will be accessed through a domain/application port.

Conceptually:

```python
class AuditPort(Protocol):
    async def append(
        self,
        event: AuditEvent,
    ) -> None: ...
```

The exact interface is not decided by this ADR.

The architectural invariant is:

```text
Maestro Application
        ↓
    AuditPort
        ↓
storage adapter
```

Application and Job logic must not depend directly on:

* PostgreSQL;
* SQLite;
* pgweb;
* a specific HTTP API;
* Langfuse;
* OpenTelemetry;
* filesystem logs.

Storage technology remains an adapter concern.

## Audit Is Automatic

Meaningful lifecycle operations must emit Audit events automatically.

An agent should not need instructions such as:

```text
Remember to record an audit event.
```

Audit coverage must result from application orchestration and domain lifecycle transitions.

This is necessary because an optional agent behavior is not a reliable governance control.

## Semantic Events

Audit should record events with semantic or governance significance.

Expected categories include events equivalent to:

```text
execution.started
execution.completed
execution.failed

decision.recorded

fact.resolved
fact.uncertain

human_input.requested
human_input.received

state.transitioned

action.started
action.completed
action.failed

artifact.created
artifact.updated

external_effect.performed

validation.completed

security_event.recorded
```

The final event taxonomy remains an Audit v1 design decision.

The important rule is:

> An event belongs in Audit because it helps a human understand or govern the engineering outcome, not merely because something happened internally.

## Audit Is Not a Trace

Audit must not record every low-level operation.

Examples that normally belong to Observability rather than Audit include:

```text
file opened
grep executed
tool invoked
individual model message
token count
model latency
subprocess timing
internal retry attempt
```

These may exist in traces.

They should not pollute the canonical semantic Audit Trail.

A single semantic Audit event may summarize many technical trace operations.

For example:

```text
Audit:
Repository fact resolved:
Order supports multiple Payments.

Observability:
- 17 files inspected
- 4 searches
- 1 model execution
- 2.8s runtime
- N tokens
```

This separation is intentional.

## No Private Chain of Thought

Maestro Audit will not persist private model chain-of-thought or raw hidden reasoning.

Audit may contain an externally meaningful rationale.

Example:

```text
Decision:
Classified the billing relationship as uncertain.

Rationale:
The current implementation structurally permits multiple billing
accounts, but ADR-017 defines a 1:1 invariant and no superseding
migration or ADR was found.

Evidence:
- ...
```

This is appropriate Audit content.

Raw internal model reasoning is not.

The objective is explainability and governance, not reconstruction of private model thought processes.

## Decision Rationale

Important decisions should record enough rationale for a human to understand the basis of the outcome.

Rationale should be:

* concise;
* factual;
* externally understandable;
* based on available evidence;
* free of unnecessary internal deliberation.

Where applicable, decisions should reference evidence rather than copying large source excerpts.

## Evidence

Audit events may reference evidence such as:

* repository paths;
* line ranges;
* ADRs;
* tests;
* commits;
* PRs;
* issue references;
* validation artifacts.

Evidence should normally be stored as structured references.

For example:

```text
repository: payments
path: src/domain/payment.py
lines: 41-58
finding: Payment references Order without a uniqueness constraint.
```

Audit should not become a duplicate repository or document archive.

## External References

Audit should correlate with external systems through structured references.

Examples:

```text
provider: jira
type: issue
id: PAY-123
```

```text
provider: github
type: pull_request
repository: example/payments
id: 891
```

External systems remain authoritative for their own data.

Maestro Audit should not copy entire Jira issues, GitHub PRs, or other external work records solely for convenience.

## Correlation

Audit must support correlation across Maestro execution.

Expected identifiers may include:

```text
audit_id
job_id
execution_id
work_item_ref
repository_ref
trace_id
```

Not every identifier exists in Maestro v1.

The model must nevertheless allow future correlation without requiring Audit events to duplicate data from other planes.

Conceptually:

```text
Jira PAY-123
     |
     +---- Job job_456
              |
              +---- Audit aud_789
              |
              +---- Execution exec_101
                       |
                       +---- Trace trace_202
```

## Audit Lifetime

A single Audit Trail should represent a meaningful unit of engineering execution.

For future Jobs, the natural relationship is expected to be approximately:

```text
Job
  |
  +---- one primary Audit Trail
            |
            +---- many semantic Audit Events
```

For bounded capabilities that execute outside a Job, Maestro may create a standalone Audit Trail associated with the invocation.

The precise lifecycle rules are deferred to the Audit v1 design.

## Append-Oriented History

Canonical Audit history should be append-oriented.

Historical events should not normally be rewritten by the Maestro execution that produced them.

Conceptually:

```text
event 1
event 2
event 3
event 4
...
```

rather than:

```text
audit document
→ continually overwritten with the newest interpretation
```

Corrections should prefer explicit subsequent events where practical.

For example:

```text
decision.recorded
decision.superseded
```

is preferable to silently mutating historical history.

The exact immutability guarantees, database permissions, and tamper-evidence mechanisms remain implementation decisions.

## Audit Is Human-Readable

Audit data must have a normal human inspection path.

A user should not need to:

* inspect application log files;
* enter the Maestro container;
* manually inspect internal runtime files;
* reconstruct decisions from traces.

The initial inspection experience may be simple.

Potential interfaces include:

```text
generic database explorer
CLI
MCP read capability
dedicated future Audit UI
```

The specific UI is not a domain requirement.

Human readability is.

## Storage Requirements

The canonical Audit Store should support:

* durable persistence;
* structured data;
* relational/correlated queries;
* chronological event access;
* filtering;
* human inspection;
* future Job correlation;
* external reference correlation;
* independent access from the Maestro process;
* migration/versioning.

A service-backed relational store is the expected production model.

### PostgreSQL

PostgreSQL is the leading candidate for the first Audit Store adapter.

Reasons include:

* mature structured relational storage;
* strong query capabilities;
* transactional semantics;
* mature tooling;
* easy container deployment;
* future compatibility with multiple Maestro processes;
* straightforward read-only access for humans and tools;
* compatibility with future dedicated APIs/UI.

However:

> PostgreSQL is not part of the Maestro domain contract.

The final backend choice and schema should be validated during Audit v1 design before this ADR becomes Accepted.

### SQLite

SQLite may be useful for tests, fixtures, development experiments, or lightweight adapters.

It is not currently the preferred operational target because a local database file provides a weaker human inspection and multi-process operational experience for the governance use case being designed.

This is not a statement that SQLite is technically unreliable.

The concern is operational visibility and future usage characteristics.

## Initial Human Inspection Candidate

If PostgreSQL is selected for Audit v1, a generic read-only database explorer such as `pgweb` may provide the initial human inspection surface.

Conceptually:

```text
                Postgres
               /        \
              /          \
             v            v

PostgresAuditAdapter    pgweb
       |                  |
       v                  v

    Maestro             Human
```

If used, pgweb is:

* an operational convenience;
* read-only from the user-facing perspective;
* not a Maestro domain dependency;
* not the long-term Audit UI contract.

Database authorization should enforce read-only access independently from any pgweb UI option.

A dedicated Audit UI may replace or complement pgweb later without changing the Maestro domain architecture.

## Storage Access Roles

If PostgreSQL is selected, the expected security direction is to use distinct roles.

Conceptually:

```text
Maestro Audit Writer
→ append required Audit data
→ no broad administrative privileges

Human / pgweb Reader
→ SELECT
→ no mutation privileges
```

The feasibility and exact permissions must be validated against the final schema and migration strategy.

Maestro should not receive broad database administrative privileges during normal runtime.

## Summaries

Audit summaries should be **derived from canonical Audit events**.

The architecture should not maintain two competing canonical modes such as:

```text
detailed audit
summary audit
```

Instead:

```text
Canonical semantic events
          |
          v
Future summarization
          |
          v
Human-readable execution summary
```

A summary may include:

* objective;
* important decisions;
* human decisions;
* important actions;
* validations;
* unresolved issues;
* external references;
* final outcome.

The underlying event trail remains available.

Summary generation is not part of this ADR's implementation decision.

## Public Read Capabilities

Future Maestro capabilities may provide structured Audit access.

Potential examples:

```text
get_audit_trail
search_audit
get_execution_summary
```

Their exact contracts are not defined here.

Read access may be exposed through MCP without making the Audit backend itself part of the public contract.

## Audit Writes Are Not a Public Agent Tool

The architecture should not expose a generic public capability equivalent to:

```text
record_audit(...)
```

for ordinary agents to decide when governance events are recorded.

Canonical Audit writes are generated by Maestro application/lifecycle behavior.

Special future administrative APIs may exist, but they should not replace automatic application-level recording.

## Audit and Human Checkpoints

Future Jobs will contain human checkpoints.

When a human decision affects execution, Audit should record the semantic decision.

For example:

```text
human_input.requested

Question:
Should archived invoices remain searchable?

External reference:
Jira PAY-123
```

followed later by:

```text
human_input.received

Decision:
Archived invoices remain searchable for administrators only.

Source:
Jira PAY-123
```

The complete Jira conversation does not need to be duplicated in Audit.

Audit records the decision that influenced execution.

## Audit and External Side Effects

Meaningful external side effects should be auditable.

Examples include:

* Git commit created;
* branch pushed;
* PR created;
* Jira state updated;
* deployment requested;
* external artifact created.

Audit should record semantic references and outcomes.

Low-level HTTP requests belong to Observability.

## Audit and Validation

Validation is governance-significant.

Future Jobs should record events such as:

```text
validation.completed
status: passed
validator: independent
revision: abc123
```

or:

```text
validation.completed
status: failed
remaining_findings: 2
```

A Job is not complete merely because the implementation worker reports success.

Audit should reflect the validation that established completion.

## Security-Relevant Events

Significant security events may belong in Audit.

Examples:

* operation blocked by policy;
* repository changed during verification;
* human approval required;
* destructive action rejected;
* runtime security limitation affected execution;
* integrity validation failed.

This does not mean every security log belongs in Audit.

Only governance-significant outcomes should be included.

## Data Sensitivity

Audit may contain sensitive engineering metadata.

Audit design must consider:

* repository names;
* issue references;
* decision rationale;
* evidence paths;
* human decisions;
* security events.

Audit should avoid unnecessarily storing:

* source-file bodies;
* entire prompts;
* entire model responses;
* credentials;
* secrets;
* private chain-of-thought.

The detailed classification, redaction, retention, access-control, and deletion policies remain Audit v1 design decisions.

## Observability Correlation

Audit events may reference a trace identifier when Observability exists.

For example:

```text
audit event
  |
  +---- trace_id
```

This allows detailed troubleshooting without placing low-level execution telemetry directly in Audit.

Audit must remain useful even if observability data has expired or is unavailable.

## Failure Semantics

Audit failure semantics are intentionally unresolved in this Proposed ADR.

Important questions include:

* Does a failed Audit write block execution?
* Can Audit events be buffered?
* Are writes retried?
* How is idempotency guaranteed?
* What happens when storage is unavailable?
* Which events are mandatory before a Job may proceed?
* Which events must commit atomically with state transitions or side effects?

These are significant design questions and must be resolved before Audit becomes a governance dependency for autonomous Jobs.

The agent reviewing this ADR should challenge this area specifically.

## Transaction Boundaries

The relationship between:

```text
Job state transition
Audit event
external side effect
```

requires explicit design.

For example:

```text
create GitHub PR
       |
       +---- external effect succeeds
       |
       +---- Audit write fails
```

must not produce an ambiguous governance state.

Likewise:

```text
Job transitions to COMPLETED
       |
       +---- final Audit event fails
```

requires defined behavior.

This ADR deliberately does not prescribe distributed transactions.

Audit v1 design should evaluate:

* transactional outbox;
* idempotent event IDs;
* retry semantics;
* side-effect ordering;
* reconciliation.

Do not introduce these mechanisms speculatively before validating the actual first use cases.

## Idempotency

Future durable Jobs may retry execution after process failure.

Audit event recording must therefore eventually support idempotent semantics.

Expected mechanisms may include:

```text
event_id
execution_id
sequence
idempotency_key
```

The exact strategy is deferred.

Duplicate semantic events caused purely by technical retries should be avoidable or identifiable.

## Event Ordering

Audit should provide a stable ordering within a Trail.

Possible mechanisms include:

```text
timestamp
+
sequence number
```

The exact ordering semantics remain open.

Wall-clock timestamps alone may not be sufficient for deterministic execution reconstruction.

## Retention

Audit retention is not defined by this ADR.

Because Audit is a governance plane, retention will likely differ from Observability retention.

For example:

```text
Observability
→ relatively short technical retention

Audit
→ potentially longer governance retention
```

The exact policy will depend on operational, privacy, and compliance requirements.

## Deletion and Correction

This ADR proposes append-oriented history but does not establish permanent undeletability.

Future requirements may require:

* retention expiration;
* privacy deletion;
* administrative correction;
* regulatory deletion.

Any deletion/correction mechanism must preserve governance clarity where possible.

This must be addressed in Audit v1 or a subsequent policy decision.

## Audit Schema Versioning

Audit events will evolve.

The implementation should therefore anticipate explicit event/schema versioning rather than assuming the first event representation will remain permanent.

Conceptually:

```text
event_type
event_version
```

The exact compatibility strategy remains open.

## Audit Service Boundary

This ADR does not yet require Audit to be deployed as an independent network service.

Possible implementations include:

```text
Maestro process
    ↓
PostgresAuditAdapter
    ↓
PostgreSQL
```

or:

```text
Maestro
    ↓
Audit Service
    ↓
PostgreSQL
```

The latter may become useful for:

* multiple Maestro instances;
* independent authorization;
* centralized ingestion;
* separate lifecycle;
* non-Maestro producers.

The first implementation should choose the smallest architecture that satisfies the requirements.

Do not introduce a network service solely for conceptual purity.

## Containers

The expected initial deployment may use containers such as:

```text
Maestro
PostgreSQL
pgweb
```

but container topology is an operational decision.

Audit domain logic must remain independent from Docker or Compose.

## Non-Goals

This ADR does not finalize:

* exact PostgreSQL schema;
* exact backend technology;
* exact `AuditPort` interface;
* final Audit event taxonomy;
* retention policy;
* deletion policy;
* encryption strategy;
* database migration tool;
* multi-tenancy;
* pgweb as permanent UI;
* dedicated Audit UI;
* summary-generation model;
* MCP read-tool contracts;
* pagination/search API;
* Audit service deployment;
* distributed transactions;
* transactional outbox;
* idempotency implementation;
* observability backend;
* WorkItemPort implementation.

## Proposed Architectural Invariants

Before this ADR becomes Accepted, validate whether the following should become permanent invariants:

1. Audit is a first-class governance plane.
2. Audit is separate from Work Management and Observability.
3. Audit recording is automatic application behavior.
4. AI workers do not optionally decide whether an event is audited.
5. Audit stores semantic events, not exhaustive technical traces.
6. Audit does not persist private chain-of-thought.
7. Important decisions include concise rationale and evidence where applicable.
8. Canonical Audit history is append-oriented.
9. External work items and artifacts are referenced rather than indiscriminately duplicated.
10. Audit storage is behind `AuditPort`.
11. Audit data is structured, durable, queryable, and independently human-readable.
12. Summaries are derived from canonical Audit events.
13. Public Audit write tools are not the primary recording mechanism.
14. PostgreSQL is the leading initial store candidate but not a domain dependency.
15. pgweb may be an initial operational read surface but is not a long-term UI contract.
16. Audit and Job/external-side-effect consistency must be designed explicitly before autonomous durable Jobs depend on Audit.

## Validation Required Before Acceptance

The architecture review should specifically challenge:

### Domain boundary

Is `AuditPort` the correct ownership boundary?

Should Audit belong to Job infrastructure, application infrastructure, or a separate governance subsystem?

### Event model

Are semantic events the right canonical model?

What minimum information makes an event useful without becoming observability noise?

### Storage

Does PostgreSQL materially improve the expected operational model compared with SQLite or another store?

Is a relational model appropriate?

Should events use relational columns, JSON payloads, or a hybrid model?

### Human inspection

Is PostgreSQL + read-only pgweb sufficient for the initial human-readability requirement?

What views would make Audit usable without building a custom UI?

### Append semantics

How strong should append-only guarantees be?

Should the Maestro runtime role have `UPDATE` or `DELETE` permissions?

How are legitimate correction/deletion requirements handled?

### Consistency

What happens when an Audit write fails?

How does Audit coordinate with:

* Job state transitions;
* Git commits;
* PR creation;
* Jira updates;
* future deployments?

Is an outbox needed initially or only with durable Jobs?

### Idempotency

How are retries prevented from producing misleading duplicate Audit events?

### Privacy and security

What sensitive data can Audit contain?

What should be redacted?

How should database authorization be structured?

### Availability

Should Audit unavailability block execution?

Does the answer differ for:

* bounded capabilities;
* destructive operations;
* durable Jobs?

### Query model

Which initial queries are genuinely required?

Examples may include:

```text
show audit by audit_id
show audits for work item
show audits for repository
show decisions
show failed validations
show human checkpoints
```

### Evolution

Can the model support future PR and Issue Jobs without prematurely implementing their data model?

## Expected Next Step

After Maestro v1 and hardened container execution are stable:

```text
1. Review this ADR critically against the actual codebase.
2. Record accepted/rejected changes.
3. Resolve the open consistency/storage/security questions.
4. Update this ADR.
5. Move Status from Proposed to Accepted only when the architectural invariants are agreed.
6. Create an Audit v1 PRD/design describing the concrete first implementation.
```

The PRD/design should then decide:

* Audit v1 event types;
* schema;
* storage adapter;
* PostgreSQL deployment if selected;
* pgweb/read experience if selected;
* failure semantics;
* idempotency;
* initial queries;
* migrations;
* tests;
* operational requirements.

## Consequences if Accepted

### Positive

A first-class Audit plane would:

* make autonomous Maestro execution understandable;
* preserve important decisions independently from task trackers;
* prevent Jira/GitHub issues from becoming execution logs;
* preserve governance history independently from telemetry retention;
* support future human oversight;
* provide evidence for Job outcomes;
* improve troubleshooting of autonomous decisions;
* support correlation across issues, PRs, agents, Jobs, and traces.

### Negative

Audit introduces durable state and therefore new complexity:

* persistence;
* migrations;
* availability;
* failure handling;
* idempotency;
* storage security;
* retention;
* query design;
* consistency with external effects.

An overly verbose Audit model could become as unusable as raw logs.

An overly sparse Audit model could fail its governance purpose.

The implementation must therefore optimize for semantic usefulness rather than maximum event volume.

## Decision Summary

This ADR proposes that Maestro treat Audit as a first-class governance subsystem with the following intended model:

```text
Maestro Application
        |
        +---- engineering execution
        |
        +---- automatic semantic Audit events
                         |
                         v
                     AuditPort
                         |
                         v
                    Audit Store
                         |
              +----------+----------+
              |                     |
              v                     v
        programmatic access     human inspection
```

The Audit Trail records:

```text
what Maestro meaningfully decided and did
+
why important decisions were made
+
what evidence supported them
+
what human input affected execution
+
what validation established the outcome
```

without becoming:

```text
a Jira comment stream
a low-level execution trace
a raw model transcript
a chain-of-thought archive
```

The proposal remains **Proposed** until reviewed against the actual Maestro implementation and refined into an accepted architecture.
