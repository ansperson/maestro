# ADR-0013: Consolidated Bounded Audit Boundary

Date: 2026-09-05

Status: Accepted

Supersedes: ADR-0005 and ADR-0012 as the current Audit decision

Related: ADR-0004, ADR-0006, ADR-0008

## Context

Maestro needs to separate information that helps engineering work continue from information that
proves what the system materially did. Without that separation, issue trackers become transcripts
or Audit becomes an accidental workflow database.

ADR-0004 established three planes:

```text
Work Management = intent and coordination
Audit           = semantic governance history
Observability   = technical execution detail
```

ADR-0005 then introduced Audit before the first durable Job. The implementation deliberately went
deep: PostgreSQL persistence, strict event contracts, fail-closed writes, retry and ambiguous
commit handling, role separation, sanitization, curated views, and hardened deployment controls.
That work validated useful security and governance boundaries, but it is disproportionate to the
current product, which still has one public read-only Capability.

ADR-0012 froze speculative expansion and removed Audit from the ordinary future Job critical path.
This ADR consolidates the current boundary and its rationale in one place. ADR-0005 and ADR-0012
remain unchanged in the decision log as the history that led here.

The lesson is not that Audit has no value. The lesson is that Audit must remain smaller than the
work it records and must not become a synchronous dependency of every future Job transition.

## Decision

Information written by Maestro has one primary owner:

```text
needed to understand, authorize, or continue work  -> Work Management
needed to resume execution safely                  -> Job state
needed to prove a material action or outcome       -> Audit
needed to operate or debug the system              -> Observability
needed by none of these                             -> discard
```

The planes may correlate through stable identifiers. They do not duplicate complete histories or
substitute for one another.

### Work Management

The work item must contain what a human or later execution needs to proceed:

- requirements and acceptance criteria;
- unresolved questions and blockers;
- authoritative decisions and approvals;
- externally useful progress and outcomes.

A decision required to authorize later work cannot live only in Audit. Doing so would make the
work item incomplete and force orchestration to reconstruct state from governance history.

### Job state

Future Jobs own the minimum durable state required to resume safely:

- current state and checkpoint;
- expected repository and external revisions;
- attempt and idempotency identities;
- references to produced artifacts and external side effects;
- bounded retry and completion state.

Audit is never queried to decide the next Job transition.

### Audit

Audit is a bounded semantic record of material behavior. Examples include:

- an authority decision applied by an execution;
- a material external side effect;
- an independent validation outcome;
- a terminal Capability or Job outcome.

Audit is not a transcript, event bus, Job store, artifact registry, workflow engine, or complete
history of model and tool interactions. It stores no prompts, private reasoning, source bodies,
raw responses, or low-level tool traces.

### Observability

Model calls, tool calls, timings, retry diagnostics, logs, and operational errors belong to
Observability. Retaining information with no continuation, governance, or operational value is not
required.

## Current Implementation

`resolve_codebase_fact` retains the Audit behavior already shipped:

```text
validated and authorized request
        -> durable execution.started
        -> bounded investigation
        -> durable investigation.completed or execution.failed
        -> semantic result only after terminal persistence
```

The current event taxonomy also includes `authority.applied` for the implemented decision-authority
entry point. PostgreSQL remains behind `AuditPort`; the runtime writer remains append-oriented and
separate from migration and reader roles.

This ADR does not silently remove the existing fail-closed public and security contract. Any
simplification of that behavior requires an explicit compatibility and threat-model review.

## Scope Freeze

Until a real Capability or Job demonstrates a requirement, do not add:

- new Audit event types;
- automated backup, restore, retention, replication, or reconciliation subsystems;
- public Audit query tools or a dedicated Audit UI;
- generic append or workflow APIs;
- transcript or low-level interaction persistence;
- infrastructure justified only by anticipated future Jobs.

Manual operational limitations should remain documented honestly. A production, regulatory, or
measured recovery requirement may justify a focused follow-up decision.

## Relationship to Jobs

Future Jobs must not use the current self-committing `AuditRecorder` as their state store or
transaction coordinator. Job transitions become durable at the Job-owned persistence boundary.
Ordinary Job progress must not synchronously wait for a separate Audit write.

The first Job design must choose how material Audit records are projected without losing them. A
transactional outbox is a candidate, but this ADR does not select a mechanism before Job
persistence is designed. Policy may require a high-risk action to establish an Audit record before
proceeding; that is an explicit exception rather than the default path.

## Complexity and Tests

Do not delete tests while retaining the production behavior they protect. Current negative and
boundary tests substantiate fail-closed persistence, idempotency, PostgreSQL privileges,
credential isolation, sanitization, and cancellation behavior.

Complexity reduction proceeds in this order:

1. freeze speculative behavior;
2. measure what real Capabilities and Jobs exercise;
3. simplify or remove an unneeded production contract;
4. remove tests made obsolete by that contract;
5. consolidate duplicated test setup without weakening observable coverage.

Job-domain tests use a fake Audit projection or no Audit dependency. PostgreSQL and container gates
remain required when the existing Audit boundary changes; they are not part of the inner
development loop for unrelated Job-domain work.

## Reconsideration Triggers

Revisit this decision only with evidence of at least one of:

- a production or regulatory retention requirement;
- a Job whose material outcome cannot be reconstructed from the bounded record;
- measured Audit availability blocking useful work;
- an operational recovery objective that manual procedures cannot meet;
- a second consumer that demonstrates a stable reusable Audit contract.

## Consequences

### Positive

- Work items remain sufficient to continue work.
- Jobs can resume from their own state.
- Audit remains reviewable and semantically meaningful.
- New Audit complexity requires demonstrated value.
- Ordinary Job transitions do not inherit the current synchronous Audit bottleneck.

### Negative

- Existing `resolve_codebase_fact` keeps its PostgreSQL operational cost for now.
- Some information intentionally exists in only one plane and must be followed by reference.
- The first Job still needs a persistence and consistency decision.
- A future asynchronous Audit projection may temporarily lag Job state.

## Decision Summary

```text
Work Management = continue and authorize the work
Job state        = resume the execution
Audit            = prove material actions and outcomes
Observability    = operate and debug the system
Everything else = discard
```

Audit is preserved at its demonstrated responsibilities, frozen against speculative expansion,
and kept outside the ordinary Job critical path.
