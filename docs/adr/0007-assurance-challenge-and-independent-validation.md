# ADR-0007: Assurance, Challenge, and Independent Validation

* **Status:** Proposed
* **Date:** 2026-08-25
* **Decision owners:** Project maintainers
* **Depends on:** ADR-0006 — Decision Authority and Human Approval
* **Related:** ADR-0001 — Maestro as an Engineering Execution Platform
* **Related:** ADR-0005 — Audit as a First-Class Governance Plane

## Context

Maestro should not blindly trust the first AI-generated conclusion.

Today, human users often provide an informal challenge layer by asking another agent:

> Is this answer actually correct?

> Try to find a reason this design is wrong.

> Does the repository contradict this conclusion?

As Maestro coordinates work autonomously, this manual challenge pattern must become an explicit assurance capability.

However, invoking several identical agents for every question would:

* increase latency;
* increase cost;
* produce correlated errors;
* create unnecessary complexity;
* still fail to establish authority.

The architecture therefore needs risk-proportional assurance rather than universal multi-agent voting.

## Proposed Decision

Maestro will use a policy-driven Assurance model.

The amount of independent challenge and validation applied to a conclusion or engineering outcome should be proportional to:

* risk;
* ambiguity;
* novelty;
* reversibility;
* security impact;
* data impact;
* architectural significance;
* quality of evidence.

The central pattern is:

```text
Produce
   ↓
Challenge when required
   ↓
Accept / Escalate / Adjudicate
   ↓
Validate outcome
```

Challenge is intended to falsify or qualify a conclusion, not merely repeat the original analysis.

## Roles

### Investigator / Producer

The Investigator answers:

> What does the evidence support?

For repository facts it may produce:

```text
answer
evidence
confidence
conflicts
```

For technical work a Producer may create:

* PRD draft;
* technical design;
* implementation;
* recommendation.

### Challenger / Skeptic

The Challenger answers:

> How might this conclusion or artifact be wrong, incomplete, unsafe, or unsupported?

Its goal is adversarial falsification.

The Challenger should actively search for:

* counterevidence;
* hidden assumptions;
* contradictions;
* unhandled edge cases;
* conflicting ADRs;
* incomplete requirements;
* security risks;
* backwards-compatibility issues;
* invalid inference;
* missing validation.

The Challenger is not a second agent asked merely:

```text
"Do you agree?"
```

### Adjudicator

The Adjudicator may be introduced when meaningful disagreement remains and evidence appears sufficient for resolution.

It answers:

> Given the competing claims and evidence, what conclusion is best supported?

Possible outcomes include:

```text
resolved
uncertain
recommendation
human_decision_required
```

The Adjudicator does not gain decision authority merely by acting as judge.

### Human / Policy Authority

Authority remains governed by ADR-0006.

For example:

```text
Investigator recommends PostgreSQL.
Challenger recommends SQLite.
Adjudicator favors PostgreSQL.

Authority class:
HUMAN_TECHNICAL

Result:
Recommendation = PostgreSQL
Decision = WAITING_FOR_HUMAN
```

## Challenger Before Judge

The initial preferred assurance model is:

```text
Investigator
     ↓
Challenger
     ↓
agree?
 ┌───┴───┐
yes      no
 │        │
accept   uncertain / escalate
```

An Adjudicator should not be introduced merely because it is possible.

The first implementation should determine from real cases whether adjudication materially improves outcomes.

Disagreement is itself useful evidence.

The correct result may be:

```text
uncertain
```

rather than forcing artificial consensus.

## Independence

Challenge quality depends on independence.

Prefer independence through:

1. fresh context;
2. independently gathered evidence;
3. different investigative objective;
4. adversarial instructions;
5. independent tools/harness when useful;
6. different model family when materially beneficial.

Do not assume that multiple model calls equal independent opinions.

Voting alone is not sufficient assurance.

## Confirmation Bias

The Challenger should not be primed unnecessarily with the Investigator's confidence or persuasive narrative.

Prefer neutral framing.

Poor:

```text
The first agent is highly confident that X is safe.
Verify it.
```

Better:

```text
Question:
Is X safe under the documented constraints?

Candidate evidence:
...

Search specifically for evidence that contradicts, qualifies,
or invalidates the candidate conclusion.
```

Where practical, the Challenger may first investigate the original question independently before seeing the candidate conclusion.

## Evidence-First Adjudication

If adjudication is used, it should compare evidence rather than agent rhetoric.

The Adjudicator should receive structured information such as:

```text
original question
candidate conclusion
counterclaim
evidence A
evidence B
known authority
rubric
```

It should not choose based on which agent sounds more confident.

## Assurance Dimensions

Assurance should not be based only on "complexity."

Relevant dimensions include:

```text
technical complexity
domain ambiguity
architecture novelty
security impact
data impact
external side effects
reversibility
breaking-change risk
confidence/evidence quality
```

A change can be:

```text
technically simple
+
security critical
```

and require strong assurance.

## Assurance Profiles

The initial conceptual profiles are:

```text
ROUTINE
STANDARD
ELEVATED
CRITICAL
```

Exact naming and thresholds remain open.

### ROUTINE

Typical characteristics:

* established implementation pattern;
* low ambiguity;
* reversible;
* no material security/data/public-contract impact.

Possible assurance:

```text
implementation
→ tests
→ deterministic validation
```

### STANDARD

Typical characteristics:

* moderate change;
* known domain behavior;
* some cross-component impact.

Possible assurance:

```text
implementation
→ review
→ validation
```

### ELEVATED

Typical characteristics:

* architecture novelty;
* meaningful schema/data change;
* non-trivial domain behavior;
* security-relevant change;
* external side effects.

Possible assurance:

```text
explicit design
→ grill/challenge
→ implementation
→ independent review
→ independent validation
```

### CRITICAL

Typical characteristics:

* destructive action;
* security boundary;
* production migration;
* material breaking change;
* high-risk data behavior.

Possible assurance:

```text
explicit human approval
→ multiple independent challenge stages
→ tightly bounded implementation
→ independent final validation
```

The policy must be validated empirically rather than implemented as arbitrary thresholds.

## Deterministic Assurance Signals

Some signals should impose minimum assurance regardless of model opinion.

Potential examples:

```text
breaking public API
→ at least ELEVATED

security boundary change
→ at least ELEVATED

destructive migration
→ CRITICAL

new persistence technology
→ explicit technical approval

production infrastructure action
→ explicit risk/technical approval

high domain ambiguity
→ requirements refinement required
```

An AI assessment may increase assurance.

It should not reduce a deterministic minimum imposed by policy.

## Agent Assessment

AI may contribute an assessment such as:

```text
architecture_impact: cross_cutting
domain_ambiguity: medium
security_impact: low
reversibility: medium
```

This is an input to policy, not the sole authority.

Conceptually:

```text
Assurance Profile
=
deterministic minimums
+
task assessment
+
existing artifacts
+
agent recommendation
```

## Grill as Challenge Workflow

The future role of `grill` is expected to become more explicitly adversarial.

Rather than rediscovering all requirements, `grill` should primarily pressure-test an existing artifact or proposal.

Examples:

```text
PRD
  ↓
grill
  ↓
domain gaps / contradictions / unsupported assumptions

Technical Design
  ↓
grill
  ↓
architecture gaps / risks / missing decisions
```

The Grill should avoid reopening decisions already settled by authoritative artifacts unless it discovers evidence that they are inconsistent or incomplete.

## Fine-Grained Challenger

The Maestro Challenger is conceptually narrower than the `grill` skill.

Example:

```text
Grill:
The technical design assumes legacy_account_id is unused.

        ↓

Challenger:
Attempt to falsify that assumption using repository evidence.
```

This allows broad artifact challenge to compose with targeted evidence challenge.

## Independent Validation

Validation should be independent when the assurance profile requires it.

The Validator should inspect actual artifacts/state.

It should not simply consume:

```text
"The implementation agent says all tests pass and the issue is complete."
```

A Validator may inspect:

```text
requirements
approved decisions
final diff
repository state
tests/results
review findings
relevant artifacts
```

Independent validation is particularly important before declaring a durable Job complete.

## Challenge Outcomes

Challenge should produce structured outcomes.

Conceptually:

```text
PASS
COUNTEREVIDENCE_FOUND
INCOMPLETE
UNCERTAIN
AUTHORITY_REQUIRED
```

Exact schemas remain open.

A challenge failure should return the workflow only to the stage necessary to resolve the gap where possible.

## Bounded Challenge

Challenge loops must be bounded.

Avoid:

```text
agent → challenger → agent → challenger → ...
```

without explicit policy limits.

Possible limits include:

```text
max challenge rounds
max address rounds
max wall-clock duration
max agent executions
```

When limits are reached, escalate explicitly.

## Relation to Decision Authority

Challenge may modify:

```text
confidence
recommendation
identified risks
evidence
```

Challenge cannot independently grant:

```text
domain authority
technical authority
risk acceptance
```

ADR-0006 remains authoritative for decision rights.

## Relation to Audit

Semantic assurance events should be auditable.

Examples:

```text
assurance.assessed
challenge.required
challenge.completed
counterevidence.found
adjudication.completed
validation.completed
```

Audit should record:

* outcome;
* concise rationale;
* relevant evidence;
* role/runtime;
* assurance profile.

It should not persist private chain-of-thought.

## Relation to Observability

Detailed agent traces, model calls, tool calls, token usage, and timing belong to Observability.

Audit records assurance meaning.

Example:

```text
Audit:
Independent challenge found no counterevidence.

Observability:
- model X
- 12 searches
- 28 files
- latency
- token usage
```

## Runtime Diversity

The architecture may later allow:

```text
Investigator → CodexRuntime
Challenger   → ClaudeCodeRuntime
```

or another combination.

Runtime diversity may improve independence but should not be assumed automatically superior.

The assurance contract must remain provider-independent.

## Eval Requirement

Assurance policies must eventually be evaluated empirically.

A future eval corpus should include:

* straightforward facts;
* subtle counterexamples;
* conflicting ADR/code;
* domain ambiguity;
* plausible but wrong technical recommendations;
* security-sensitive changes;
* missing requirements;
* hallucinated evidence.

Metrics may include:

```text
false acceptance
false escalation
counterevidence detection
unnecessary challenge rate
human disagreement
latency/cost
```

Assurance level should be calibrated using evidence, not intuition alone.

## Proposed Invariants

Before acceptance, validate:

1. Assurance is proportional to risk/ambiguity, not universal multi-agent voting.
2. Challenger and Investigator have distinct objectives.
3. Challenger actively seeks falsification/counterevidence.
4. Disagreement may legitimately yield `uncertain`.
5. Adjudication is optional, not mandatory.
6. Judge/adjudicator does not gain decision authority.
7. Deterministic policy can impose minimum assurance.
8. AI may increase but not bypass minimum assurance.
9. Independent validation inspects actual outcomes.
10. Challenge loops are bounded.
11. Assurance outcomes are auditable.
12. Detailed technical traces remain Observability.
13. Policies should be calibrated with evals.

## Open Questions Before Acceptance

### Profiles

Are four profiles appropriate?

### Scoring

Should policy use categorical rules, numeric score, or both?

### Challenger implementation

Should Challenger reuse `AgentRuntime` with a role-specific policy?

### Runtime diversity

When does using a different runtime materially improve assurance?

### Adjudicator

When is an Adjudicator worth its added cost/latency?

### Grill integration

Should `grill` call targeted Challenger capabilities directly or only through Maestro Job orchestration?

### Evidence isolation

How much of the candidate conclusion should Challenger see initially?

### Assurance persistence

Does the assurance profile belong in Job state, an artifact, Audit, or all three?

### Human override

Can a maintainer deliberately reduce required assurance for a Job, and how is that audited?

## Non-Goals

This ADR does not implement:

* multi-agent consensus;
* model voting;
* challenger runtime;
* adjudicator runtime;
* assurance scoring;
* new `grill` skill;
* PR review Job;
* feature Job;
* provider routing;
* automatic model selection.

## Consequences if Accepted

### Positive

Risk-proportional assurance would:

* avoid blindly trusting the first model;
* reduce unnecessary multi-agent cost;
* detect hidden assumptions;
* provide stronger validation for high-risk work;
* keep simple tasks fast;
* allow model/runtime diversity;
* integrate naturally with Audit and Jobs.

### Negative

Assurance policy itself can be wrong.

Overly aggressive challenge increases cost and latency.

Weak challenge creates false confidence.

Multiple agents may still make correlated errors.

The system needs evals and operational feedback to calibrate policies.

## Decision Summary

This ADR proposes:

```text
Produce
   ↓
Challenge proportional to risk
   ↓
Accept / Uncertain / Escalate
   ↓
Independent validation when required
```

with:

```text
Challenge improves evidence quality.
Challenge does not create authority.
```

The proposal remains **Proposed** until validated against the current Maestro architecture, Decision Authority model, and real evaluation cases.
