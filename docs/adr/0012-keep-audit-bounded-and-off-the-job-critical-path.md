# ADR-0012: Keep Audit Bounded and off the Job Critical Path

Date: 2026-09-05

Status: Accepted

Amends: ADR-0004, ADR-0005

Related: ADR-0006, ADR-0008

## Context

Audit was introduced before the first durable Job so Maestro could validate governance against
the bounded `resolve_codebase_fact` Capability. That implementation established strong contracts,
append-oriented PostgreSQL storage, idempotent writes, role separation, sanitization, and
fail-closed behavior.

The result is coherent but disproportionately large for the current product. Audit now carries
substantial implementation, test, deployment, and operational cost while Maestro still has one
public read-only Capability and no durable Job. Extending the same synchronous lifecycle into Jobs
would make Audit availability and schema evolution part of every orchestration transition. It
would also risk using Audit as Job state, which ADR-0004 forbids.

The underlying information problem is simpler. Information written by an agent has four possible
destinations:

```text
needed to continue, coordinate, or authorize work  -> Work Management
needed to resume an execution                       -> Job state
needed to reconstruct a material action or outcome -> Audit
needed only to operate or debug the system          -> Observability
needed by none of these                              -> discard
```

Duplicating all information into every plane increases noise without improving governance. A
complete agent transcript is especially unsuitable for Audit: it is costly, difficult to review,
and expands the sensitive-data surface.

## Decision

Audit remains a bounded semantic governance record. It is not a transcript, event bus, workflow
engine, Job store, artifact registry, or general history of agent interactions.

Work Management owns the information a human or later execution needs to understand and continue
the work. This includes requirements, acceptance criteria, unresolved questions, approvals,
authoritative decisions, blockers, and externally useful progress. A decision needed to authorize
later work must remain available in Work Management; storing it only in Audit would make the work
item non-resumable.

Job state owns the minimum durable execution state needed to resume safely, including the current
state, expected repository revision, checkpoint, attempt identity, and references to external
side effects. Audit must never be queried to determine the next Job transition.

Audit stores only material semantic facts needed to reconstruct what Maestro meaningfully did and
why. Examples include an applied authority decision, a material external side effect, a validation
outcome, or a terminal Job outcome. Records should reference Work Management and Job identities
rather than duplicate their full content.

Observability owns technical traces such as model calls, tool calls, timings, retries, logs, and
diagnostics. Information with no continuation, governance, or operational value is not retained.

## Job Critical-Path Rule

The current `resolve_codebase_fact` fail-closed Audit behavior remains unchanged until a separate
implementation decision simplifies it. This ADR does not silently weaken an existing public or
security contract.

Future Jobs must not depend on the current self-committing `AuditRecorder` as their state store or
transaction coordinator. A Job transition becomes durable in the Job-owned persistence boundary.
Audit recording must not make every ordinary transition synchronously wait for a separate Audit
write.

The Job persistence design must choose an explicit consistency mechanism for material Audit
records. A transactional outbox or another recoverable projection is a candidate, but this ADR
does not select one before the first Job persistence design exists. Policy may still require a
specific high-risk action to establish an Audit record before proceeding; that is an explicit
exception, not the default execution path.

## Scope Freeze

Until a durable Job demonstrates a concrete need, do not add:

- new Audit event types;
- automated backup, restore, retention, replication, or reconciliation subsystems;
- Audit query tools or a dedicated Audit UI;
- generic append APIs or workflow abstractions;
- transcript, prompt, source-body, or low-level tool-call persistence;
- infrastructure whose only purpose is anticipated Job Audit behavior.

The existing operator documentation may describe manual recovery limitations honestly. A future
production or compliance requirement may justify lifting part of this freeze through a focused
decision and measured use case.

## Complexity and Test Policy

Do not reduce test coverage merely to make the repository appear smaller while the corresponding
production behavior remains. The current negative and boundary tests substantiate fail-closed
persistence, idempotency, role, credential, sanitization, and cancellation claims.

Complexity reduction should proceed in this order:

1. stop adding speculative Audit behavior;
2. measure which behavior is exercised by real Capabilities and Jobs;
3. simplify or remove an unneeded production contract through an explicit compatibility and
   security review;
4. remove the tests that became obsolete with that contract;
5. consolidate duplicated test setup without weakening observable coverage.

Audit tests remain outside orchestration ordering except where a change actually touches the
Audit boundary. Job tests should depend on a typed fake Audit projection or no Audit dependency,
not on PostgreSQL Audit integration. PostgreSQL and container gates remain required for changes to
the existing Audit implementation, but they must not become mandatory development loops for
unrelated Job-domain changes.

## Consequences

### Positive

- Work items remain sufficient for humans and later executions to continue work.
- Jobs can resume from their own durable state without reconstructing state from Audit events.
- Audit remains human-reviewable and semantically meaningful.
- Audit outages and migrations do not automatically become bottlenecks for every future Job step.
- New Audit complexity requires demonstrated value rather than anticipated reuse.

### Negative

- Some information is intentionally stored in only one plane and must be followed through stable
  references.
- The first Job still needs a concrete persistence and consistency decision.
- Existing `resolve_codebase_fact` retains its current PostgreSQL operational cost until a
  separate simplification is approved and implemented.
- Eventual Audit projection may be temporarily behind Job state when the future consistency design
  permits it.

## Decision Summary

```text
Work Management = what is needed to understand, authorize, and continue the work
Job state        = what is needed to resume execution safely
Audit            = bounded semantic proof of material actions and outcomes
Observability    = technical execution detail
Everything else = discard
```

Audit is preserved but frozen at its demonstrated responsibilities. It does not own Job state and
does not become the default synchronous gate for ordinary Job transitions.
