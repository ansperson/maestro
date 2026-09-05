# ADR-0008: Durable Pull Request Review Job

Date: 2026-08-25

Status: Proposed

Depends on: ADR-0006, ADR-0007

Related: ADR-0001, ADR-0004, ADR-0013

## Context

Maestro currently executes bounded Capabilities within one request. It cannot durably coordinate
work that pauses for another worker, an external change, or human authority. Audit cannot fill
that role: ADR-0013 assigns resumable execution state to Jobs and keeps Audit outside ordinary Job
transitions.

The first demonstrated orchestration need is pull request review:

```text
review an exact PR revision
-> wait for or perform corrections
-> inspect the new revision
-> independently validate the outcome
```

This workflow can span processes and external actors. It therefore needs durable identity,
checkpoints, revision binding, idempotency, and bounded recovery. It does not yet justify a generic
adaptive workflow engine.

## Proposed Decision

Introduce a durable `review_pull_request` Job as the first Job implementation.

The initial workflow is fixed and explicit:

```text
load PR and work-item authority
-> pin the current PR revision
-> run a read-only review
-> publish actionable findings
-> address findings or wait for an external correction
-> pin the resulting revision
-> run deterministic gates and independent validation
-> complete, repeat within a bound, or wait for human authority
```

The implementation must remain specific to this outcome. Shared primitives should be extracted
only after a second Job demonstrates the same boundary.

## Job Ownership

The Job owns the minimum durable state needed to resume safely:

- stable Job identity and type;
- current state and checkpoint reason;
- repository and pull request identity;
- expected and observed PR revisions;
- current bounded attempt;
- references to findings, validations, and external side effects;
- idempotency keys for replayable operations;
- terminal outcome and typed failure when applicable.

MCP transport state, Audit events, logs, and issue comments are not the source of truth for Job
progress.

## Initial States

Use only states required by the first workflow:

```text
CREATED
RUNNING
WAITING_FOR_EXTERNAL
WAITING_FOR_HUMAN
FAILED
COMPLETED
CANCELLED
```

Detailed progress is represented by a typed checkpoint within `RUNNING` or a waiting state. Do not
create a state for every implementation step unless recovery semantics differ.

## Revision Safety

Every review and validation result is bound to the exact commit it inspected. Before applying a
result or completing the Job, Maestro confirms that the pull request still has the expected
revision.

If the revision changes unexpectedly, stale conclusions are not applied. The Job records the new
revision and restarts from the nearest safe checkpoint within its attempt bounds.

## Corrections

The Job supports two correction paths:

- a bounded write-capable worker addresses findings under explicit repository authorization; or
- the Job enters `WAITING_FOR_EXTERNAL` until a person or external agent updates the PR.

The design must not assume that Maestro always has write authority. External comments, labels,
reviews, and branch updates require idempotency and reconciliation against the provider's observed
state.

## Assurance and Authority

ADR-0007 governs review challenge and independent validation. A worker that changes the code
cannot be the final validator when independent validation is required.

ADR-0006 governs human and technical decision authority. A Job persists a checkpoint when required
authority is absent; it does not infer approval from agent confidence or surrounding prose.

## Separation of Planes

- **Work Management** contains findings, decisions, blockers, and outcomes that participants need
  to continue.
- **Job state** contains resumable execution state, expected revisions, attempts, and idempotency.
- **Audit** receives a bounded projection of material actions and terminal outcomes according to
  ADR-0013.
- **Observability** contains model/tool calls, timings, diagnostics, and low-level failures.

Ordinary Job transitions do not synchronously depend on a separate Audit write. The persistence
design must define how material Audit projections are eventually made reliable.

## Initial Non-Goals

The first implementation does not include:

- a generic DAG or workflow-definition language;
- an adaptive LLM planner or numeric task-assessment engine;
- distributed scheduling or multiple worker hosts;
- `implement_feature` or general issue implementation orchestration;
- Jira or multiple Work Management providers;
- arbitrary user-defined Jobs;
- Audit as a Job store or transaction coordinator;
- MCP Tasks as durable Job state.

## Decisions Required Before Acceptance

Implementation is not authorized until follow-up design closes:

1. persistence backend, schema, concurrency control, and migration ownership;
2. public MCP contract for starting, reading, resuming, and cancelling a Job;
3. secure execution boundary for write-capable correction and repository-controlled validation;
4. GitHub authentication, idempotency, stale-revision handling, and side-effect reconciliation;
5. checkpoint authorization and the event that resumes external or human waits;
6. bounded retry, cancellation, timeout, and orphan-worker behavior;
7. acceptance of the proportional assurance policy in ADR-0007;
8. reliable bounded Audit projection without placing Audit on the transition critical path.

These may be resolved in one implementation ADR if the design is cohesive. Do not create one ADR
per routine implementation choice.

## Alternatives Rejected

### Implement a generic adaptive orchestrator first

Rejected because there is no second implemented Job demonstrating reusable routing semantics. It
would front-load abstractions before persistence and recovery behavior are proven.

### Use Audit as Job state

Rejected because an append-only governance history is not a resumable workflow model and would
make Audit a critical-path bottleneck.

### Keep the workflow entirely inside one MCP request

Rejected because external corrections and approvals can outlive the request or process.

## Consequences

### Positive

- The first Job solves a concrete end-to-end orchestration problem.
- Durable state has one explicit owner.
- Exact-revision and idempotency rules prevent stale or duplicate actions.
- Generic orchestration abstractions are deferred until evidence supports them.

### Negative

- A later Job may reveal a better shared model and require refactoring.
- Durable persistence and write-capable execution introduce new security boundaries.
- The proposal cannot be accepted until its listed design decisions are resolved.
