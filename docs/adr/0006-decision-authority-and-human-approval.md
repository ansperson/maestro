# ADR-0006: Decision Authority and Human Approval

* **Status:** Proposed
* **Date:** 2026-08-25
* **Decision owners:** Project maintainers
* **Related:** ADR-0001 — Maestro as an Engineering Execution Platform
* **Related:** ADR-0004 — Separate Work Management, Audit, and Observability Planes
* **Related:** ADR-0005 — Audit as a First-Class Governance Plane

## Context

Maestro is expected to evolve from bounded engineering capabilities into durable Jobs capable of performing substantial engineering work with decreasing amounts of mechanical human coordination.

Examples include:

```text
Issue
  ↓
requirements refinement
  ↓
PRD
  ↓
technical design
  ↓
implementation
  ↓
review
  ↓
validation
  ↓
verified outcome
```

Increasing automation creates an authority problem.

An AI agent may be capable of:

* discovering repository facts;
* interpreting evidence;
* identifying alternatives;
* recommending an architecture;
* identifying a likely business interpretation;
* implementing a selected approach.

Capability to produce an answer does not imply authority to make the decision.

For example:

```text
Fact:
Existing services use PostgreSQL.

Inference:
A shared relational database fits existing infrastructure.

Recommendation:
Use PostgreSQL for Audit.

Decision:
Maestro Audit will use PostgreSQL.
```

The first three may be produced autonomously.

The final statement changes project architecture and requires appropriate authority unless that authority has already been explicitly delegated by accepted project policy.

Similarly:

```text
"The current implementation archives invoices."
```

is a factual repository question.

But:

```text
"Archived invoices should remain searchable by administrators."
```

is a domain decision unless an authoritative requirement already establishes it.

Without explicit authority semantics, Maestro risks silently converting:

* ambiguity into requirements;
* recommendations into decisions;
* current behavior into desired behavior;
* model preference into architecture;
* inferred business meaning into policy.

This is incompatible with trustworthy autonomous engineering.

## Proposed Decision

Maestro will explicitly distinguish:

```text
FACT
INFERENCE
RECOMMENDATION
DECISION
```

and will attach authority semantics to decisions.

The central invariant is:

> Maestro may aggressively automate information gathering, reasoning, recommendation, execution, and validation, but authority to make a decision must be explicit.

An AI conclusion does not become an authorized decision solely because:

* confidence is high;
* several agents agree;
* a judge model prefers it;
* the choice appears technically reasonable;
* current implementation happens to behave that way.

## Classification

### Fact

A claim about the current or historically documented state of the system that can be supported by authoritative evidence.

Examples:

```text
Order currently supports multiple Payments.

customer_id has a UNIQUE constraint.

ADR-017 requires PostgreSQL.

legacy_account_id is still read by BillingService.
```

Facts may normally be resolved autonomously.

### Inference

A conclusion derived from one or more facts but not directly stated by an authoritative source.

Example:

```text
Most existing infrastructure services use PostgreSQL,
therefore PostgreSQL would probably reduce operational novelty.
```

Inferences may be generated autonomously but must remain identifiable as inference.

### Recommendation

A proposed choice based on facts, constraints, trade-offs, or policies.

Example:

```text
Recommendation:
Use PostgreSQL rather than SQLite for Audit storage.
```

Recommendations may be generated and challenged autonomously.

A recommendation is not itself an authorized project decision.

### Decision

A choice that changes or establishes intended behavior, architecture, risk posture, product semantics, or project policy.

Examples:

```text
Use PostgreSQL for Audit.

Archived invoices are searchable by administrators.

Breaking compatibility is acceptable.

A destructive migration is approved.

This service may depend on Redis.
```

Decisions require appropriate authority.

## Authority Sources

Maestro should recognize authority from sources such as:

```text
explicit current human decision
accepted PRD / requirements artifact
accepted ADR
accepted project policy
authoritative domain documentation
explicitly delegated automated policy
```

The exact representation of these sources remains an implementation/design decision.

Authority must not be inferred merely from:

* source code;
* tests;
* common conventions;
* model confidence;
* historical implementation;
* popularity of a technology.

## Authority Precedence

The proposed precedence is conceptually:

```text
1. Explicit current human decision
2. Accepted requirements / PRD
3. Accepted ADR
4. Accepted project policy / authoritative domain documentation
5. Current repository facts
6. Agent inference
7. Agent recommendation
```

Lower levels must not silently override higher levels.

Examples:

```text
Accepted PRD says:
One billing account per organization.

Current code permits several.

Result:
Implementation discrepancy.

Not:
Permission to reinterpret the requirement.
```

Another example:

```text
Accepted ADR says:
Use PostgreSQL.

Agent recommends:
SQLite.

Result:
PostgreSQL remains authoritative unless changing the ADR is explicitly proposed.
```

The final precedence model should be validated before this ADR becomes Accepted.

## Decision Authority Categories

The initial conceptual authority categories are:

```text
AUTOMATIC_FACT
POLICY_DELEGATED
HUMAN_DOMAIN
HUMAN_TECHNICAL
HUMAN_RISK
```

The exact naming and representation remain open.

### AUTOMATIC_FACT

The question asks what is true and can be established from authoritative evidence.

Example:

```text
Does this table have a uniqueness constraint?
```

Maestro may resolve it automatically.

### POLICY_DELEGATED

An accepted policy, ADR, or other authoritative source already determines the choice.

Example:

```text
Project policy:
New HTTP services use the standard authentication middleware.
```

Maestro may apply that policy without requesting the same human decision repeatedly.

### HUMAN_DOMAIN

The choice establishes or interprets business/domain behavior not already authoritatively specified.

Examples:

```text
Should archived invoices remain searchable?

Can a Customer have multiple active subscriptions?

What happens when a payment arrives after settlement?
```

Maestro must not invent these answers.

### HUMAN_TECHNICAL

The choice establishes material technical architecture or technology not already delegated by project policy.

Examples:

```text
PostgreSQL vs SQLite.

Introduce Redis.

Adopt a new messaging system.

Change a service boundary.

Introduce a new externally visible API model.
```

Maestro may provide recommendations and alternatives but requires appropriate maintainer approval.

### HUMAN_RISK

The choice accepts meaningful risk or irreversible impact.

Examples:

```text
breaking compatibility
data loss
destructive migration
production infrastructure modification
security-risk acceptance
reduced durability
reduced availability
```

These require explicit authority regardless of model recommendation.

## Local Reversible Decisions

Not every implementation choice requires human approval.

Maestro may autonomously make local, reversible decisions when they:

* do not establish domain semantics;
* do not contradict accepted architecture;
* do not introduce material new dependencies;
* do not change public contracts;
* do not accept significant risk;
* are consistent with existing patterns;
* can be safely changed during implementation/review.

Examples may include:

```text
local function naming
private helper extraction
internal data structure choice
test fixture organization
minor implementation details
```

The goal is not to turn every coding decision into a checkpoint.

## Human Checkpoints

When required authority is missing, Maestro should create a durable human checkpoint rather than guessing.

Conceptually:

```text
Technical Design
       ↓
New architectural decision detected
       ↓
Recommendation prepared
       ↓
WAITING_FOR_HUMAN
       ↓
Maintainer approves/rejects
       ↓
Job resumes
```

A checkpoint should provide enough information for an efficient decision.

For example:

```text
Decision required:
Audit persistence backend

Authority:
HUMAN_TECHNICAL

Recommendation:
PostgreSQL

Alternatives considered:
SQLite

Rationale:
- shared durable store
- human query requirement
- multiple future Maestro workers
- existing operational familiarity

Relevant ADRs:
...

Approve PostgreSQL?
```

The user should not need to reconstruct the analysis from raw traces.

## Existing Authority Should Reduce Questions

Maestro should consume existing authoritative artifacts before asking humans.

Before requesting a decision, it should determine whether that decision is already resolved by:

* an accepted PRD;
* an ADR;
* domain documentation;
* explicit policy;
* a prior applicable human decision.

Conceptually:

```text
decision appears necessary
        ↓
search authority
        ↓
already decided?
   ┌────┴────┐
  yes        no
   │          │
   ▼          ▼
apply       human checkpoint
```

This is essential to reducing unnecessary questioning over time.

As project knowledge becomes more explicit, Maestro should require fewer repetitive human decisions.

## Requirements Authority

Domain behavior must not be inferred from technical implementation when the intended behavior is ambiguous.

If no accepted requirement exists:

```text
existing behavior
!=
automatically desired behavior
```

The current implementation is evidence about the system, not automatic product authority.

A domain refinement workflow may recommend interpretations, identify common patterns, or expose consequences.

It may not silently approve an interpretation on behalf of the domain owner.

## Technical Authority

A technical-design workflow may:

* gather facts;
* identify constraints;
* analyze alternatives;
* recommend a solution;
* challenge alternatives;
* estimate risks.

It may autonomously apply technical choices already delegated through accepted architecture or policy.

It may not establish materially new architecture without appropriate authority.

The implementation should distinguish:

```text
"Existing ADR already determines this."
```

from:

```text
"This choice seems obvious to the model."
```

## Risk Authority

No amount of agent agreement gives an AI authority to accept significant risk unless policy explicitly delegates that class of risk.

Examples include:

```text
"We can tolerate data loss."
"We can break this public API."
"This security weakness is acceptable."
"Ten minutes of production downtime is acceptable."
```

Agents may explain trade-offs.

Humans or explicit accepted policies decide risk acceptance.

## Relation to Challenge and Adjudication

Challenge mechanisms may dispute:

* facts;
* evidence;
* inference;
* recommendations;
* completeness;
* trade-off analysis.

Challenge or adjudication does not grant additional decision authority.

Example:

```text
Technical proposer:
Recommend PostgreSQL.

Challenger:
SQLite is operationally simpler.

Adjudicator:
Evidence favors PostgreSQL.

Authority:
HUMAN_TECHNICAL

Final state:
Recommendation = PostgreSQL
Decision = WAITING_FOR_HUMAN
```

The future Assurance/Challenge ADR must preserve this boundary.

## Relation to Jobs

Future Jobs should treat authority as a prerequisite.

Example:

```text
ImplementationStage requires:

- approved requirements
- all required domain decisions resolved
- all required technical decisions resolved
- no blocking risk decisions
```

If those prerequisites are missing, implementation should not begin.

This allows Jobs to adapt their workflow based on what authority already exists.

## Relation to Audit

Governance-relevant decision lifecycle should be auditable.

Expected semantic events may include:

```text
decision.required
decision.recommended
decision.approved
decision.rejected
decision.superseded
authority.applied
```

Audit should store concise decision rationale and authority references, not private model chain-of-thought.

## Decision Reuse

An approved decision may be reusable when its scope clearly applies to future work.

The architecture should avoid repeatedly asking the same question.

However, decision reuse must respect scope.

For example:

```text
Decision:
Use PostgreSQL for Maestro Audit v1.
```

does not automatically mean:

```text
Every future project must use PostgreSQL.
```

Future implementation must represent enough scope/context to prevent inappropriate reuse.

## Decision Supersession

Decisions may be superseded.

The preferred conceptual model is explicit history:

```text
decision A approved
       ↓
new evidence / architecture change
       ↓
decision B approved
       ↓
decision A superseded
```

rather than silently rewriting history.

This aligns with the proposed append-oriented Audit architecture.

## Proposed Invariants

Before acceptance, validate whether these should become permanent invariants:

1. Facts, inferences, recommendations, and decisions are distinct concepts.
2. AI confidence does not create authority.
3. Domain ambiguity cannot be resolved by undocumented model interpretation.
4. Material new technical architecture requires human or explicitly delegated authority.
5. Risk acceptance requires human or explicitly delegated authority.
6. Existing accepted authority should eliminate repetitive questions.
7. Current implementation is evidence, not automatic requirements authority.
8. Local reversible implementation decisions may remain autonomous.
9. Challenge/adjudication cannot elevate a recommendation into an authorized decision.
10. Jobs must resolve required authority before entering dependent execution stages.
11. Decision lifecycle is auditable.
12. Authority scope and supersession must remain explicit.

## Open Questions Before Acceptance

The architecture review should challenge:

### Authority representation

How should authority be represented in Maestro?

Examples:

```text
artifact references
policy IDs
decision records
typed authority objects
```

### Scope

How precisely must the scope of a decision be represented?

### Existing documents

How does Maestro determine whether a PRD/ADR/document is accepted and current?

### Conflicts

What happens when two authoritative sources conflict?

### Delegation

How are automated decision classes explicitly delegated?

### Expiration

Can authority expire?

### Human identity

How much identity information is required for approval?

### Approval channels

Can approval come from:

```text
Jira
GitHub
CLI
MCP client
future UI
```

and how is authenticity established?

### Small technical choices

Where should the line between autonomous implementation detail and human technical authority be drawn?

### Decision persistence

Should decision records belong directly to Audit, Job state, a dedicated Decision model, or a combination?

## Non-Goals

This ADR does not define:

* UI for approvals;
* Jira/GitHub adapters;
* Audit schema;
* exact `DecisionAuthority` classes;
* Job orchestration implementation;
* challenge implementation;
* PRD format;
* Technical Design format;
* authentication mechanisms for approvals;
* policy language;
* a general rules engine.

## Consequences if Accepted

### Positive

This model would:

* prevent silent invention of business rules;
* prevent model preference from becoming architecture;
* reduce unnecessary human questions through authority reuse;
* preserve human control over meaningful choices;
* allow aggressive automation of low-risk work;
* make approval points explicit and auditable;
* provide a foundation for adaptive Jobs.

### Negative

Authority classification introduces complexity.

Incorrect classification may either:

* ask humans too often; or
* grant too much autonomy.

Accepted artifacts need reliable status and scope.

Conflicting authorities require resolution semantics.

These costs are accepted because autonomous engineering without explicit authority boundaries is unsafe and difficult to govern.

## Decision Summary

This ADR proposes:

```text
FACT
→ may be resolved automatically

INFERENCE
→ may be produced automatically

RECOMMENDATION
→ may be produced and challenged automatically

DECISION
→ requires explicit delegated authority
```

with:

```text
Maestro automates reasoning aggressively.
Authority remains explicit.
```

The proposal remains **Proposed** until validated against the current Maestro architecture and the planned Audit/Job model.
