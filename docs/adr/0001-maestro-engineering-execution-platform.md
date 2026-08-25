# ADR-0001: Maestro as an Engineering Execution Platform

* **Status:** Accepted
* **Date:** 2026-08-25
* **Decision owners:** Project maintainers

## Context

Our AI-assisted engineering workflows currently rely primarily on agent skills.

Skills such as:

```text
grill
pr-review
pr-address
```

define how an agent should perform specialized engineering work.

This model works well when one agent can execute the workflow from beginning to end.

However, recurring workflows increasingly require multiple independent agent executions.

Two concrete examples motivated this decision.

### Repository Fact Verification

During a `grill` session, an agent may encounter a factual question about the repository.

For example:

```text
Does the current system allow multiple billing accounts per organization?
```

The user may currently need to:

```text
Grill Agent
    |
    v
asks question

User
    |
    v
opens another agent

Verifier Agent
    |
    v
investigates repository

User
    |
    v
copies answer

Grill continues
```

The user is acting as a message bus between agents.

### Pull Request Review Cycle

A PR may require several independent roles:

```text
review
  ↓
address findings
  ↓
validate resulting changes
```

Today this may require the user to manually open and coordinate several agent sessions.

For example:

```text
Agent A
skill: pr-review
    |
    v
findings

User
    |
    v

Agent B
skill: pr-address
    |
    v
changes

User
    |
    v

Agent C
skill: pr-validate
    |
    v
verdict
```

The repeated coordination is mechanical rather than a meaningful human decision.

We expect the same problem to occur in larger workflows such as implementing an issue from clarification through final validation.

We therefore need infrastructure capable of coordinating multiple independent agents toward durable engineering outcomes.

## Decision

We will build Maestro as an **Engineering Execution Platform**.

Maestro will coordinate:

```text
agents
skills
capabilities
jobs
integrations
policies
human checkpoints
```

toward verified engineering outcomes.

MCP will initially be one interface through which coding agents access Maestro.

MCP is not Maestro's domain model.

The architectural model is:

```text
Clients
  |
  | MCP / future interfaces
  v
Maestro
  |
  +-- Capabilities
  |
  +-- Jobs
  |
  +-- Integrations
  |
  +-- Policies
  |
  v
Agent Runtime
  |
  v
Disposable Agents
  |
  v
Skills
```

## Core Concepts

### Skill

A Skill defines how an agent performs a specialized role.

Examples:

```text
grill
pr-review
pr-address
pr-validate
```

Skills may define:

* reasoning methodology;
* review criteria;
* conversational behavior;
* domain language;
* workflow-specific instructions;
* when human input is required.

Skills remain independently executable outside Maestro.

### Capability

A Capability is a reusable bounded engineering operation with explicit input and output.

Capabilities may be deterministic or agentic.

Example:

```text
resolve_codebase_fact
```

A capability is appropriate when the reusable primitive benefits from infrastructure such as:

* isolated execution;
* stable contracts;
* programmatic validation;
* explicit permissions;
* independent runtime;
* observability;
* timeout;
* concurrency control.

### Job

A Job coordinates multiple operations toward a durable engineering outcome.

A Job may contain:

```text
agents
skills
capabilities
integrations
state transitions
retries
policies
human checkpoints
```

Examples include:

```text
review_pull_request
implement_issue
```

A Job is not merely a long prompt.

It is an explicit orchestration state machine.

### Checkpoint

A Checkpoint represents a durable pause.

Examples:

```text
WAITING_FOR_HUMAN
WAITING_FOR_EXTERNAL
```

The Job persists its state and may resume later.

The worker that produced the checkpoint does not need to remain alive.

## Fundamental Principle

We adopt the following ownership model:

> **Agents are disposable workers. Jobs own durable state.**

An engineering task must survive:

* individual agent termination;
* process restarts;
* external delays;
* human response delays;
* retry boundaries.

An active model session must not become the persistence mechanism for a long-running engineering task.

## Skills Remain Independent

Maestro will not absorb skills into its implementation by default.

For example:

```text
pr-review
pr-address
pr-validate
```

remain skills.

Maestro may execute different agents with those skills:

```text
Maestro Job
    |
    +-- Agent A + pr-review
    |
    +-- Agent B + pr-address
    |
    +-- Agent C + pr-validate
```

This preserves separation between:

```text
skill
= how the worker performs the role

Maestro
= how workers are coordinated
```

## Capability vs Job

We distinguish bounded reusable operations from orchestration.

Example Capability:

```text
resolve_codebase_fact
```

Conceptually:

```text
question
  ↓
investigation
  ↓
structured result
```

Example Job:

```text
review_pull_request
```

Conceptually:

```text
PR
  ↓
review
  ↓
address
  ↓
validate
  ↓
verified result
```

The Job may consume Capabilities.

## Repository Fact Verification

The initial Maestro Capability will be:

```text
resolve_codebase_fact
```

Its responsibility is:

> Determine whether an objective question about the current repository can be answered reliably, and return validated repository evidence.

Possible outcomes include:

```text
resolved
uncertain
human_decision_required
```

This capability exists in Maestro rather than solely in a skill because it benefits from:

* isolated AI context;
* independent investigation;
* enforceable read-only access;
* stable structured output;
* evidence validation;
* timeout;
* cancellation;
* recursion protection;
* repository access control;
* reuse across future workflows.

## Pull Request Review Job

Maestro may provide a Job that coordinates the existing PR-related skills.

Conceptually:

```text
                    PR HEAD abc123
                         |
                         v
                  Review Agent
                skill: pr-review
                   READ ONLY
                         |
                         v
                     findings
                         |
                         v
                 Address Agent
               skill: pr-address
                     WRITE
                         |
                         v
                  tests / lint
                         |
                         v
                  commit + push
                         |
                         v
                   HEAD def456
                         |
                         v
                Validation Agent
              skill: pr-validate
                   READ ONLY
                         |
                   +-----+-----+
                   |           |
                   v           v
                 PASS         FAIL
                   |           |
                   v      bounded retry
               COMPLETE
```

The Job owns:

* stage progression;
* original revision;
* resulting revision;
* findings;
* commits;
* test results;
* retry count;
* final result.

Individual agents own none of this durable state.

## Independent Validation

The validation stage must be independent from the implementation/address stage.

The validator should receive sufficient evidence to evaluate the resulting state.

It should not simply receive:

```text
The address agent says all findings were fixed.
```

Instead, it should independently inspect:

```text
original intent
original findings
final repository state
final diff
tests
relevant requirements
```

This is intended to reduce confirmation bias and correlated agent errors.

## Repository Revision Safety

Jobs operating on version-controlled repositories must track revision identity.

For example:

```text
initial HEAD = abc123
```

If the remote PR changes during execution:

```text
abc123 → xyz999
```

Maestro must not silently continue based on stale assumptions.

The Job must follow an explicit policy such as:

```text
restart
pause
abort safely
request human attention
```

After modification:

```text
final HEAD = def456
```

the final validator evaluates that specific revision.

## Bounded Retries

Agent loops must be bounded.

Example:

```text
Review
  ↓
Address
  ↓
Validate
  |
  +-- PASS
  |
  +-- FAIL
         |
         v
      Address
         |
         v
      Validate
```

must have an explicit limit such as:

```text
max_address_rounds = 2
```

When the limit is reached, the Job transitions to an explicit state requiring human attention or failure handling.

Unbounded agent-to-agent loops are prohibited.

## Future Issue Implementation Job

Maestro is expected to eventually coordinate an entire engineering issue.

Conceptually:

```text
Issue
  |
  v
Load Context
  |
  v
Grill Agent
skill: grill
  |
  +-- factual uncertainty
  |       |
  |       v
  | resolve_codebase_fact
  |
  +-- human decision
          |
          v
   request checkpoint
          |
          v
    update Jira
          |
          v
 WAITING_FOR_HUMAN
          |
          v
        resume
          |
          v
Implementation Agent
          |
          v
Tests / Validation
          |
          v
Create / Update PR
          |
          v
PR Review Job
          |
          v
Final Validation
          |
          v
Update Issue
          |
          v
COMPLETE
```

This is a durable orchestration problem, not a single-agent prompt.

## Human Checkpoints

Human involvement is a first-class Job state.

We explicitly reject the model:

```text
human input required
=
workflow failure
```

Instead:

```text
human input required
=
WAITING_FOR_HUMAN
```

The Job persists:

* question;
* reason;
* relevant context;
* current state;
* artifacts produced so far;
* next resumable step.

The question may be published through an integration such as Jira.

After a human response is received, the Job resumes.

## Durable State

Jobs should eventually persist sufficient state to survive process restarts.

Possible state includes:

```text
Job ID
Job type
Job state

repository
branch
revision

issue reference
PR reference

stage
retry counters

artifacts
decisions
checkpoints

agent execution history
```

Persistence technology is deliberately not decided by this ADR.

We will introduce durable storage when the first Job requiring it is implemented.

## MCP's Role

MCP is an interface.

Conceptually:

```text
Codex
  |
  | MCP
  v
Maestro
```

The MCP layer may expose:

```text
Capabilities
Jobs
Job status
Job cancellation
```

but Maestro domain logic must not depend on MCP concepts.

For example:

```text
Maestro Job
```

is a domain entity.

An MCP long-running task mechanism may later represent that Job to MCP clients, but it does not own the Job's persistence or state model.

## Integrations

Maestro will integrate with existing engineering systems where required.

Potential examples:

```text
GitHub
Jira
Terraform
CI
cloud systems
```

Maestro should prefer using existing mature integrations rather than recreating them unnecessarily.

The division is:

```text
Maestro
= orchestration

integration
= communication with external system
```

For example:

```text
Job
  |
  v
request human clarification
  |
  v
Jira adapter
```

The Job should not encode Jira-specific semantics deeply unless the workflow genuinely requires them.

## Permissions by Stage

Different workers should receive different permissions.

For example:

```text
PR Reviewer
filesystem: read-only
GitHub: read

PR Addressor
filesystem: write worktree
GitHub: push approved feature branch

PR Validator
filesystem: read-only
GitHub: read
```

Permissions should follow least privilege.

A prompt instruction such as:

```text
Do not modify files.
```

is not considered sufficient enforcement where technical isolation is available.

## Policy-Bounded Autonomy

End-to-end execution must remain bounded by explicit policy.

Example:

```text
automatically allowed:
  read repository
  modify feature branch
  run tests
  commit approved changes

requires approval:
  destructive database changes
  breaking API change
  production infrastructure modification
  secret modification
  unresolved business decision

forbidden by default:
  bypass protected branch rules
  bypass required checks
  silently merge production changes
```

The exact policy mechanism will evolve separately.

## State Model

Expected Job states include:

```text
QUEUED
RUNNING
WAITING_FOR_HUMAN
WAITING_FOR_EXTERNAL
BLOCKED
FAILED
COMPLETED
CANCELLED
```

Transitions must be validated explicitly.

State should not be represented by arbitrary strings scattered across orchestration code.

## Failure Model

We distinguish:

```text
engineering uncertainty
operational failure
human checkpoint
external wait
```

For example:

```text
resolve_codebase_fact → uncertain
```

means investigation completed but evidence was insufficient.

It is not equivalent to:

```text
agent process crashed
```

Likewise:

```text
WAITING_FOR_HUMAN
```

is not an error.

## Why Maestro Is Not Just a Collection of Skills

Skills alone are insufficient for some orchestration requirements.

They cannot reliably provide:

* durable state ownership;
* cross-agent coordination;
* bounded retry enforcement;
* revision locking;
* checkpoint persistence;
* independent observability;
* cross-system integration state;
* process restart recovery;
* permission boundaries across stages.

Maestro provides those execution-level concerns while leaving expertise in skills.

## Why Maestro Is Not a Generic Workflow Framework

Maestro should not become a universal workflow engine.

Its scope is engineering execution involving agentic and deterministic workers.

We explicitly avoid using Maestro merely because a sequence exists.

A Maestro Job is justified when there is material value in coordinating:

```text
multiple independent workers
+
engineering state
+
external systems
+
retries
+
human checkpoints
```

toward a verified outcome.

## Decision Rules

Use a Skill when the primary problem is:

> How should one agent perform this specialized work?

Use a Capability when the primary problem is:

> What reusable bounded engineering operation should be available to multiple callers?

Use a Job when the primary problem is:

> How do we coordinate multiple independent executions, systems, and checkpoints toward one durable engineering outcome?

## Consequences

### Positive

We remove the user from mechanical agent-to-agent coordination.

Independent agents can review, modify, and validate work.

Long-running engineering tasks can pause and resume.

Human decisions become explicit checkpoints.

Skills remain independently maintainable.

Capabilities can evolve independently from their consumers.

Jobs provide auditability across engineering execution.

Repository revisions and artifacts can be tracked explicitly.

Permissions can be scoped by execution stage.

### Negative

Maestro becomes a more substantial system than a simple MCP tool server.

Durable Jobs will eventually require persistence.

Orchestration introduces:

* state management;
* retries;
* idempotency concerns;
* cancellation;
* concurrency;
* recovery;
* integration failures;
* policy enforcement.

Agent execution cost may increase due to independent validation.

Multi-agent workflows create additional failure modes.

These costs are accepted because they directly address repeated real workflows rather than speculative generalization.

## Anti-Goals

Maestro will not become:

* a repository containing every prompt;
* a replacement for agent skills;
* an unrestricted autonomous engineering agent;
* a generic `ask_ai` endpoint;
* a universal workflow engine;
* an implementation of every external engineering system;
* a source of unbounded autonomous agent loops.

The fact that something can be automated does not automatically justify implementing it as a Maestro Job.

## Guiding Principles

### Agents are disposable workers

Do not store the task's source of truth only inside an agent session.

### Jobs own state

Durable engineering work must be recoverable and resumable.

### Skills define expertise

Do not duplicate specialized role behavior unnecessarily inside Maestro.

### Human checkpoints are first-class

Human decisions belong in explicit states, not ad hoc interruptions.

### Validation should be independent

Workers validating changes should not blindly trust workers that produced those changes.

### Autonomy must be policy bounded

Permission, retry, and execution boundaries must be explicit.

### Prefer orchestration over manual coordination

The user should not have to relay messages between agents when Maestro can safely coordinate them.

### Verified outcome is the goal

A Job is not complete because an agent claims success.

Completion should be supported by appropriate validation and artifacts.

## Decision Summary

Maestro will evolve according to the following model:

```text
Skills
= expertise and agent behavior

Agents
= disposable workers

Capabilities
= bounded reusable engineering primitives

Jobs
= durable multi-step engineering orchestration

Checkpoints
= explicit pauses for human or external input

Integrations
= communication with external engineering systems

Maestro
= engineering execution and control plane
```

The initial implementation begins with:

```text
resolve_codebase_fact
```

The first orchestration-oriented use case is expected to be:

```text
review_pull_request
  → review
  → address
  → validate
```

The longer-term target is:

```text
implement_issue
  → clarify
  → implement
  → test
  → review
  → address
  → validate
  → complete
```

This architecture is intended to remove mechanical human orchestration while preserving explicit human authority over decisions that genuinely require it.
