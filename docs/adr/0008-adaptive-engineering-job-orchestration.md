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
-> wait for an external correction
-> inspect the new revision
-> independently validate the outcome
```

This workflow can span processes and external actors. It therefore needs durable identity,
checkpoints, revision binding, idempotency, and bounded recovery.

Future engineering processes are also expected to become Jobs. The first implementation should
therefore establish a small reusable Job kernel, but one concrete state machine. It does not yet
justify a generic workflow-definition language or adaptive orchestration engine.

## Proposed Decision

Introduce a durable `review_pull_request` Job as the first Job implementation.

The Job exists before the first review and follows this fixed workflow:

```text
create Job and load PR/work-item authority
-> pin PR HEAD A
-> run a read-only review
-> no material findings: independently validate the review against HEAD A, then complete or wait
-> material findings: publish them and enter WAITING_FOR_EXTERNAL
-> an external person or agent performs pr-address and produces HEAD B
-> resume the Job and verify that HEAD changed as expected
-> run independent validation against HEAD B
-> complete, request one more correction, or wait for human authority
```

`pr-review` and `pr-validate` are separate Skill roles performed by disposable workers within one
Job. `pr-address` participates in the workflow but remains external to Maestro in the first
version.

## Reusable Job Kernel

The first implementation establishes only the reusable behavior already required by the review
workflow:

- stable Job identity and type;
- generic lifecycle status plus a Job-specific typed checkpoint;
- durable attempts and exact input/output revision identities;
- optimistic concurrency and a bounded execution lease;
- idempotency for transitions and external side effects;
- start, read, resume, cancel, and typed failure semantics;
- transactional outbox records for owned external effects and material Audit projection;
- runtime, provider, model, and policy-version identity for every worker execution.

The `review_pull_request` state machine, finding contracts, GitHub revision rules, and assurance
criteria remain specific to that Job. A second Job may reuse the kernel and provide evidence for
extracting more shared workflow concepts.

## Persistence

Job state uses PostgreSQL in the existing deployment, behind a dedicated `JobRepository` port.
Jobs use a separate schema, migration ownership, and runtime role from Audit. Sharing a PostgreSQL
deployment does not allow either domain to query or mutate the other's tables.

The initial relational model contains only what the workflow exercises: Jobs, attempts, and an
outbox. Job-specific checkpoint data may use a strict versioned payload inside a relational
envelope. Do not introduce an event store, generic artifact registry, or table per workflow stage.

A state transition and its owned outbox messages commit atomically. Outbox delivery is idempotent
and reconciles ambiguous provider responses before retrying. Audit remains a bounded projection;
it is not queried to resume the Job.

Without a background dispatcher, `start` and `resume` deliver pending outbox messages after the
owning transition commits and before advancing past the related checkpoint. A later `resume`
reconciles and retries messages left pending by process loss. `get_job` remains read-only and never
causes an external side effect.

## Lifecycle and Execution

Use only generic states required by the first workflow:

```text
CREATED
RUNNING
WAITING_FOR_EXTERNAL
WAITING_FOR_HUMAN
FAILED
COMPLETED
CANCELLED
```

Review, validation, and their expected revisions are typed checkpoints, not additional generic
states.

The first version is command-driven and has no background scheduler. An MCP `start` or `resume`
call acquires the Job lease and advances the state machine until the next durable wait or terminal
state. A later call may resume it from that checkpoint, including after Maestro restarts.

Cancellation stops and reaps the owned worker, releases or lets the bounded lease expire, and
leaves the last committed checkpoint resumable unless the Job was explicitly cancelled. A stale
lease may be recovered; two callers cannot advance the same Job version concurrently.

## Public MCP Boundary

The initial public contract provides four explicit operations:

```text
start_pull_request_review
get_job
resume_job
cancel_job
```

These tools expose stable Job identifiers and typed public states, not persistence records or
internal stage machinery. MCP session/task state is not durable Job state.

`resume_job` is mechanical rather than authoritative. For an external wait it verifies provider
state, including the new PR HEAD. For a human wait it re-reads explicit authority under ADR-0006;
the caller cannot assert that approval occurred.

## GitHub Boundary

A dedicated pull request port owns GitHub mechanics. The Job domain does not know GitHub identifier,
API, review, check-run, comment, or authentication shapes.

The first adapter must support only what this Job exercises:

- resolve one configured repository and pull request;
- read immutable revision identity and relevant metadata;
- publish or reconcile one bounded findings artifact;
- read the required CI/check outcome for an exact revision;
- observe whether an external correction produced a new revision.

The adapter receives a least-privilege token through a dedicated configuration projection. Every
write carries an idempotency identity and is reconciled against observed GitHub state after an
ambiguous response. There are no webhooks or polling daemon initially; the operator or calling
agent explicitly invokes `resume_job`.

## Revision Safety

Every review and validation result is bound to the exact commit inspected. Before publishing a
result, applying it to Job state, or completing the Job, Maestro confirms that the pull request
still has the expected revision.

If the revision changes unexpectedly, stale conclusions are not applied. The Job records the
observed revision and returns to the nearest safe review checkpoint within its round bound.

The first version does not execute repository-controlled tests or builds. It reads the required CI
result for the exact revision through the pull request port. Introducing local repository-controlled
execution belongs to the later write-capable execution design.

## Assurance and Correction Rounds

ADR-0007 governs review challenge and independent validation. Review and validation run in fresh
worker contexts with different objectives. The validator checks each material finding against the
new exact revision and the observed deterministic CI result.

When the initial review reports no material findings, the validator independently checks that
conclusion against the same revision before the Job can complete. A disagreement becomes findings
or `WAITING_FOR_HUMAN`; it is not resolved by trusting the first worker.

One correction round is:

```text
published findings
-> external pr-address creates a new revision
-> independent validation of that revision
```

The Job permits at most two correction rounds. If material findings remain after the second
validation, it enters `WAITING_FOR_HUMAN` with the unresolved findings and evidence. Operational
retries do not consume a correction round unless a new revision is evaluated.

Initially both roles use the deployment's explicitly selected provider runtime, but each execution
records provider and model identity. Different-provider routing is deferred until evaluations show
that it materially improves assurance; multiple providers are not treated as proof of independence.

## External `pr-address`

The first Job never gives a worker repository write access, executes repository-controlled code,
creates commits, or pushes a branch. A human or external agent performs `pr-address`; Maestro
observes the resulting revision and resumes.

The next intended architectural slice may introduce a bounded write-capable execution boundary as
the shared substrate for `pr-address` and a later `implement_issue` Job. That requires its own
security decision covering isolated worktrees, command execution, diff validation, credentials,
commit/push ownership, cancellation, and recovery. It is not hidden inside this read-only Job.

## Separation of Information

- **Work Management** contains findings, decisions, blockers, and outcomes needed to continue.
- **Job state** contains checkpoints, expected revisions, attempts, leases, and idempotency.
- **Audit** receives bounded material actions and terminal outcomes according to ADR-0013.
- **Observability** contains model/tool calls, timings, diagnostics, and low-level failures.

Ordinary Job transitions commit independently of Audit delivery. The transactional outbox makes
the bounded projection recoverable without making the current `AuditRecorder` a transaction
coordinator.

## Initial Non-Goals

The first implementation does not include:

- a generic DAG, workflow-definition language, stage registry, or plugin system;
- adaptive LLM planning or numeric task assessment;
- a background scheduler, webhooks, or distributed workers;
- an internal write-capable `pr-address` execution;
- `implement_feature`, `implement_issue`, or automatic PR creation;
- Jira or multiple Work Management providers;
- runtime routing across multiple model providers;
- arbitrary user-defined Jobs;
- Audit or MCP Tasks as durable Job state.

## Dogfooding Acceptance Scenario

The implementation is not complete until this scenario succeeds:

1. start a Job through MCP for an existing Maestro pull request at HEAD A;
2. persist and publish review findings bound to A;
3. enter `WAITING_FOR_EXTERNAL` and stop the Maestro process;
4. let an external `pr-address` actor produce HEAD B;
5. start a new Maestro process and resume the same Job through MCP;
6. independently validate the findings and required CI against B;
7. complete, request the second bounded correction round, or wait for a human;
8. repeat start/resume safely without duplicating findings or external side effects.

The candidate implementation may run this scenario against its own PR as controlled bootstrap
evidence, but it cannot be the only merge gate. Independent CI and human review remain required.
The strongest dogfooding evidence is the integrated Job reviewing the next Maestro PR.

## Decisions Required Before Acceptance

The architectural direction is now bounded. These questions still block acceptance:

1. define the exact strict MCP request/result schemas and externally observable error semantics;
2. define the minimal Job, attempt, lease, and outbox schema plus migration and role privileges;
3. define the pull request/finding contracts and the least-privilege GitHub permission set;
4. define lease expiry, cancellation, operational retry, and ambiguous-delivery recovery bounds;
5. define which GitHub checks are required and how absent, pending, failed, or stale checks affect
   validation;
6. accept the proportional assurance policy in ADR-0007 and version the two Skill policies;
7. define an evaluation and control arm for review/validation quality under ADR-0011.

These can be resolved in one implementation design and issue set. They do not require one ADR per
table, error code, or routine implementation choice.

## Alternatives Rejected

### Build only a PR-specific state machine with no reusable Job kernel

Rejected because identity, durable lifecycle, concurrency, idempotency, and resume semantics are
already known requirements for later Jobs. Reimplementing them per workflow would create drift.

### Implement a generic adaptive orchestrator first

Rejected because no second Job demonstrates reusable routing semantics. It would front-load
abstractions before persistence and recovery are proven.

### Execute `pr-address` inside the first Job

Rejected because it combines unproven durable orchestration with repository writes, untrusted code
execution, Git credentials, commits, and push recovery. The external correction still exercises
the Job's durable wait and resume boundary.

### Use Audit as Job state

Rejected because governance history is not a resumable workflow model and would make Audit a
critical-path bottleneck.

### Keep the workflow entirely inside one MCP request

Rejected because external corrections and approvals can outlive the request or process.

## Consequences

### Positive

- The first Job solves a concrete end-to-end orchestration problem.
- A minimal reusable kernel establishes durable ownership without guessing future workflow shape.
- PostgreSQL transactions support concurrency, idempotency, and reliable outbox delivery.
- Exact-revision and two-round rules prevent stale or unbounded autonomous actions.
- External correction proves restart/resume before write-capable execution is introduced.

### Negative

- A later Job may reveal a better shared model and require kernel refactoring.
- PostgreSQL remains an operational prerequisite for Jobs.
- Explicit resume means the first version does not progress autonomously while unattended.
- The Job cannot correct findings itself until the write-capable boundary is separately approved.
- The proposal remains unaccepted until its contract, persistence, GitHub, recovery, and evaluation
  details are closed.
