# ADR-0008: Adaptive Engineering Job Orchestration

* **Status:** Proposed
* **Date:** 2026-08-25
* **Decision owners:** Project maintainers
* **Depends on:** ADR-0006 — Decision Authority and Human Approval
* **Depends on:** ADR-0007 — Assurance, Challenge, and Independent Validation
* **Related:** ADR-0001 — Maestro as an Engineering Execution Platform
* **Related:** ADR-0005 — Audit as a First-Class Governance Plane

## Context

A central goal of Maestro v2 is to coordinate a feature or issue from initial intent to a verified engineering outcome.

A naive implementation could hardcode:

```text
refine requirements
→ write PRD
→ technical design
→ grill
→ implement
→ review
→ validate
```

for every task.

That would recreate the inefficiency of the current interactive `grill` workflow.

Many tasks do not need every stage.

Example:

```text
Add middle_name to an existing response object using the established pattern.
```

may require:

```text
context
→ implement
→ test
→ validate
```

while:

```text
Support multiple billing accounts per organization.
```

may require substantial domain clarification, technical design, approvals, challenge, migration analysis, implementation, and independent validation.

The workflow therefore needs to adapt to:

* existing artifacts;
* unresolved domain ambiguity;
* technical novelty;
* risk;
* authority;
* assurance requirements.

At the same time, Maestro must not give an unrestricted LLM planner permission to invent arbitrary workflows.

## Proposed Decision

Maestro will support adaptive, policy-driven engineering Jobs.

A Job should execute:

> the minimum workflow necessary to reach a sufficiently specified, appropriately authorized, and independently validated outcome for the task's risk profile.

Adaptive does not mean arbitrary.

Routing will be constrained by:

```text
Job state
+
stage prerequisites
+
Decision Authority policy
+
Assurance policy
+
deterministic routing rules
+
agent assessment/recommendation
```

An LLM recommendation may influence routing but must not bypass hard policy requirements.

## Initial Target Job

The first general end-to-end Job is expected to be conceptually:

```text
implement_feature
```

or an equivalent name determined during v2 design.

Its potential stages include:

```text
Context Assembly
Task Assessment
Requirements Refinement
PRD Authoring
Domain Approval
Technical Design
Technical Approval
Challenge / Grill
Implementation
Review
Independent Validation
Completion
```

Not every stage executes for every Job.

## Context Assembly

Before asking humans or designing solutions, Maestro should assemble relevant context.

Potential sources include:

* issue/work item;
* repository;
* accepted PRDs;
* accepted ADRs;
* authoritative domain documentation;
* `CONTEXT.md`;
* related Audit Trails;
* existing implementation;
* related tests;
* external references.

The objective is:

```text
evidence first
questions second
```

Maestro should not ask a human for information that authoritative context already resolves.

## Task Assessment

A Job should assess dimensions such as:

```text
technical complexity
domain ambiguity
architecture novelty
security impact
data impact
breaking-change impact
external side effects
reversibility
existing authority completeness
```

The result should be structured.

Conceptually:

```text
TaskAssessment

complexity
domain_ambiguity
architecture_impact
security_impact
data_impact
reversibility
breaking_change
human_decisions_required
recommended_assurance_profile
```

Exact fields remain open.

## Deterministic Minimums

Some signals impose mandatory routing or assurance.

Examples may include:

```text
unresolved domain ambiguity
→ requirements refinement

new material technology
→ technical approval

breaking public API
→ explicit authority + elevated assurance

destructive migration
→ risk approval + critical assurance

security boundary change
→ elevated/critical assurance

accepted PRD already exists
→ domain refinement may be skipped

accepted ADR fully resolves technical choice
→ do not ask the same technical decision again
```

These rules must not be bypassed merely because an agent classifies a task as simple.

## Existing Artifacts as State

Jobs should consume authoritative artifacts rather than recreate them.

Example:

```text
Issue
  ↓
accepted PRD exists?
  ↓ yes
skip domain refinement/PRD generation
```

Another:

```text
accepted ADR resolves architecture?
  ↓ yes
apply authority
```

The principle is:

> Jobs should consume existing authority instead of regenerating it.

## Requirements Refinement

Requirements refinement should focus exclusively on unresolved domain/product behavior.

Its objective is not to conduct a broad technical interview.

Conceptually:

```text
issue + authoritative context + repository facts
       ↓
refine-requirements agent
       ↓
domain decision set
```

The workflow should:

1. gather existing authority;
2. resolve repository facts automatically;
3. identify only real domain ambiguities;
4. group related decisions where useful;
5. ask humans only for missing authority;
6. produce requirements with no silent business assumptions.

A future skill such as:

```text
refine-requirements
```

may perform this role.

This ADR does not define the skill implementation.

## PRD Authoring

Authoring should be separable from requirements discovery.

Conceptually:

```text
resolved domain decisions
        ↓
to-prd
        ↓
PRD
```

The PRD should contain enough domain behavior to prevent implementation from inventing requirements.

Expected qualities include:

* goals;
* non-goals;
* actors;
* behavior;
* domain rules;
* edge cases;
* acceptance criteria;
* explicit human decisions;
* known constraints;
* no unresolved blocking domain questions.

The precise PRD format remains a skill/artifact decision.

## Domain Approval

For meaningful domain changes, the Job may require explicit approval of the PRD/domain decisions.

The approval view should emphasize decisions rather than requiring the maintainer to reconstruct the entire conversation.

Conceptually:

```text
PRD READY

Decisions:
- archived invoices searchable by admins
- applies retroactively
- deletion prohibited after settlement

Open blocking domain questions:
0

Approve?
```

Approved PRDs become authority for later stages.

## Technical Design

Technical Design begins only when domain prerequisites are sufficient.

It may use:

* approved PRD;
* repository facts;
* ADRs;
* existing architecture;
* established patterns;
* security constraints;
* operational constraints.

It should produce:

```text
proposed architecture
affected components
data model
API impact
migration strategy
compatibility impact
security impact
alternatives
risks
test strategy
rollout considerations
technical decisions required
```

The exact Technical Design artifact remains outside this ADR.

## Technical Approval

New material technical decisions require authority according to ADR-0006.

Example:

```text
Recommendation:
PostgreSQL

Existing ADR deciding this:
No

Authority:
HUMAN_TECHNICAL
```

The Job pauses until the decision is resolved.

Routine decisions already covered by established architecture should proceed automatically.

## Challenge / Grill Stage

The `grill` workflow is expected to become a challenge/pressure-test stage rather than primary requirements discovery.

A Job may invoke Grill depending on Assurance policy.

Examples:

```text
PRD
  ↓
Grill
  ↓
missing domain branch discovered
```

or:

```text
Technical Design
  ↓
Grill
  ↓
unsupported architecture assumption discovered
```

Routine work may skip full Grill.

High-risk work may require it.

## Targeted Challenge

Grill may identify assumptions that require targeted Challenger investigation.

Example:

```text
Technical Design assumes legacy_account_id is unused.
        ↓
Challenger investigates that assumption independently.
```

This composes broad artifact challenge with fine-grained evidence verification.

## Returning to Prior Stages

If challenge discovers a gap, the Job should return only to the necessary stage.

Example:

```text
Technical Design
      ↓
Grill
      ↓
missing domain rule
      ↓
Requirements Refinement
      ↓
PRD update/approval
      ↓
Technical Design delta
      ↓
Challenge
```

The Job should not restart from zero unnecessarily.

## Stage Preconditions and Outputs

Each stage should declare prerequisites and outputs.

Conceptually:

```text
TechnicalDesignStage

requires:
- approved requirements
- resolved blocking domain decisions
- repository context

produces:
- TechnicalDesign
- TechnicalDecisionRequests[]
- Risks[]
```

Implementation:

```text
ImplementationStage

requires:
- approved requirements
- required technical decisions approved
- no blocking authority gaps

produces:
- repository changes
- tests
- artifacts
```

This makes orchestration state-driven rather than relying only on a hardcoded sequence.

## Stage Skipping

A stage may be skipped only when its required outcome already exists or policy does not require it.

Examples:

```text
approved PRD exists
→ requirements refinement may be skipped

no architecture novelty
→ full technical design may be skipped

ROUTINE assurance
→ full Grill may be skipped
```

Skipping should be explainable and auditable.

## Policy-Driven Planning

The Job should not expose all tools/skills to an LLM and ask:

```text
"What workflow would you like to invent?"
```

Instead:

```text
State Machine
+
Policy Engine
+
Agent Assessment
```

determines valid next stages.

An AI may recommend:

```text
"Elevated assurance is appropriate."
```

but deterministic policy may enforce:

```text
security impact = high
→ challenge cannot be skipped
```

## Job State

A Feature Job should own structured state.

Conceptually:

```text
FeatureJob

context
assessment

requirements_status
technical_design_status

open_domain_decisions
open_technical_decisions
open_risk_decisions

assurance_profile

current_stage
completed_stages
skipped_stages

implementation_artifacts
validation_status
```

Exact persistence is outside this ADR.

## Durable State

The fundamental ADR-0001 principle applies:

> Agents are disposable workers. Jobs own durable state.

No agent thread should need to remain open while:

* waiting for human approval;
* waiting for Jira response;
* waiting for external state;
* transitioning between stages.

## Human Checkpoints

When authority is missing:

```text
Job
 ↓
WAITING_FOR_HUMAN
```

The Job persists enough information to resume later.

The checkpoint should include:

* decision required;
* authority class;
* recommendation when appropriate;
* alternatives;
* concise rationale;
* relevant evidence;
* affected artifacts.

## Assurance Integration

ADR-0007 determines minimum assurance.

Conceptually:

```text
TaskAssessment
       ↓
AssurancePolicy
       ↓
ROUTINE / STANDARD / ELEVATED / CRITICAL
       ↓
required stages
```

Assurance can add mandatory:

* Grill;
* Challenger;
* review;
* independent validation;
* human approval.

## Decision Authority Integration

ADR-0006 determines which choices can proceed automatically.

Conceptually:

```text
choice needed
    ↓
authority already exists?
   / \
 yes  no
 │     │
apply  checkpoint
```

Job orchestration must not turn recommendations into decisions.

## Audit Integration

Routing and governance-significant stage events should be auditable.

Examples:

```text
job.started
assessment.completed
stage.required
stage.skipped
decision.required
decision.approved
challenge.required
stage.completed
validation.completed
job.completed
```

Important routing decisions should include concise rationale.

Example:

```text
stage.skipped

stage:
requirements_refinement

reason:
Accepted PRD v3 already resolves the domain requirements.
```

The Job must not audit every low-level agent/tool operation.

## Work Management Integration

Jobs may originate from:

```text
Jira
GitHub Issue
direct user request
future WorkItem adapter
```

The external tracker represents Work Management.

Job state and Audit remain separate.

A Job should reference the external work item rather than duplicate its full history unnecessarily.

## Simple Task Example

```text
Issue:
Add middle_name to Customer response.

Assessment:
domain ambiguity = none
architecture novelty = none
security impact = none
reversible = yes
assurance = ROUTINE

Existing patterns:
sufficient

Workflow:

context
→ implementation
→ tests
→ validation
→ complete
```

No unnecessary PRD/Grill interview.

## Medium Feature Example

```text
Feature:
Archive invoices.

Assessment:
domain ambiguity = medium
architecture impact = local
risk = standard/elevated

Workflow:

context
→ refine requirements
→ PRD
→ domain approval
→ technical design
→ light challenge
→ implementation
→ review
→ validation
```

## Large Feature Example

```text
Feature:
Multiple billing accounts per organization.

Assessment:
domain ambiguity = high
architecture novelty = high
data impact = high
assurance = ELEVATED/CRITICAL

Workflow:

context
→ deep requirements refinement
→ PRD
→ domain Grill
→ human domain approval
→ technical design
→ targeted challenge
→ technical Grill
→ human technical decisions
→ implementation
→ PR review cycle
→ independent validation
→ verified outcome
```

## Adaptive Workflow Principle

The permanent architectural principle proposed is:

> Maestro should execute the minimum workflow necessary to reach a sufficiently specified, appropriately authorized, and independently validated outcome for the task's risk profile.

This is distinct from:

```text
always run every stage
```

and from:

```text
let an LLM freely invent the workflow
```

## Skill Independence

Skills remain independently maintained.

Potential skills include:

```text
refine-requirements
to-prd
technical-design
grill
implementation
pr-review
pr-address
```

Maestro Jobs coordinate workers configured with the required skill.

Maestro should not absorb all skill logic into Job implementation.

## Artifact Ownership

Artifacts such as:

```text
PRD
Technical Design
Review Findings
Validation Result
```

should have explicit ownership/status.

Future orchestration must determine:

* draft vs approved;
* version;
* authority;
* supersession;
* references.

Exact artifact registry design remains open.

## Idempotency and Resumption

Durable Jobs must eventually handle:

* process restart;
* stage retry;
* duplicate external events;
* side effects already completed.

The future technical design must define stage idempotency and reconciliation.

This ADR does not prescribe the persistence implementation.

## Bounded Execution

Adaptive does not mean unbounded.

Jobs require limits such as:

```text
maximum agent executions
maximum challenge rounds
maximum address rounds
maximum duration
optional cost/token budget
```

Limits may vary by Assurance profile.

When exceeded, the Job escalates rather than looping indefinitely.

## Proposed Invariants

Before acceptance, validate:

1. Jobs are adaptive rather than universally fixed pipelines.
2. Adaptive routing is policy-constrained, not free-form LLM planning.
3. Context/evidence gathering precedes human questioning.
4. Existing approved artifacts can satisfy stage prerequisites.
5. Domain ambiguity routes to requirements refinement.
6. Material technical decisions route to authority.
7. Grill becomes conditional challenge, not mandatory discovery.
8. Assurance determines mandatory challenge/validation stages.
9. Stages declare prerequisites and outputs.
10. Jobs return only to stages needed to resolve discovered gaps.
11. Stage skips are explainable and auditable.
12. Agents remain disposable; Job state is durable.
13. Skills remain independent from orchestration implementation.
14. Jobs execute the minimum sufficient workflow for risk and ambiguity.

## Open Questions Before Acceptance

### First Job scope

Should v2 first implement:

```text
implement_feature
```

or a narrower Job?

### Assessment

Which dimensions are required in `TaskAssessment`?

### Policy representation

Should routing rules be code, configuration, typed policy objects, or another mechanism?

### Artifact model

How are approved PRDs/designs discovered and versioned?

### Skills

How does Maestro explicitly provide a skill to an isolated worker?

### Stage model

Does Maestro need a generic `Stage` abstraction initially or should the first Job use explicit application code?

### Persistence

What minimum durable state is necessary for v2?

### Human checkpoints

How are answers authenticated and correlated?

### Work Management

Should v2 initially support direct requests only, or one external issue tracker?

### Routing evals

How do we measure:

```text
unnecessary stage rate
missed required stage rate
unnecessary human questions
unsafe automatic decisions
time/cost to completion
```

### Recovery

How does a Job resume after crashes or deployment restart?

## Implementation Strategy

If accepted, avoid creating a generic workflow framework before implementing a real Job.

Preferred sequence:

```text
1. Implement one real Feature Job.
2. Encode concrete stages/policies.
3. Observe duplication and real extension points.
4. Extract abstractions only when demonstrated.
```

Do not begin with a universal DAG/workflow engine.

## Non-Goals

This ADR does not select:

* database;
* queue;
* workflow engine;
* Temporal/Celery/etc.;
* generic DAG framework;
* Job storage schema;
* issue tracker;
* skill format;
* artifact storage;
* UI;
* scheduler;
* distributed execution.

## Consequences if Accepted

### Positive

Adaptive orchestration would:

* reduce unnecessary human questioning;
* make simple tasks fast;
* give complex tasks sufficient rigor;
* reuse approved project knowledge;
* connect domain refinement, design, implementation, and validation;
* preserve explicit authority;
* integrate assurance proportionally to risk;
* create a path to end-to-end issue implementation.

### Negative

Routing policy becomes a critical system component.

Bad classification may skip necessary stages or add unnecessary work.

Artifact status/authority must become explicit.

Durable state and recovery will be required.

The first Feature Job may reveal that some abstractions proposed here need simplification.

## Decision Summary

This ADR proposes a Maestro v2 execution model:

```text
Feature / Issue
      ↓
Context Assembly
      ↓
Task Assessment
      ↓
Decision Authority + Assurance Policy
      ↓
Adaptive Stage Selection
      ↓
Requirements / PRD when needed
      ↓
Technical Design / Approval when needed
      ↓
Challenge / Grill when needed
      ↓
Implementation
      ↓
Review / Independent Validation as required
      ↓
Verified Outcome
```

with the guiding rule:

> **Execute the minimum workflow necessary to produce a sufficiently specified, appropriately authorized, and independently validated outcome for the task's risk profile.**

The proposal remains **Proposed** until validated against the current Maestro implementation and refined into the concrete Maestro v2 design.
