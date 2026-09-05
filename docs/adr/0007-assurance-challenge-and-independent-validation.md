# ADR-0007: Proportional Assurance and Independent Validation

Date: 2026-08-25

Status: Proposed

Depends on: ADR-0006

Related: ADR-0008, ADR-0011, ADR-0013

## Context

An agent saying that its own work is correct is not independent validation. At the same time,
running several agents, judges, and voting rounds for every change adds cost and latency without
necessarily reducing correlated errors.

Maestro needs enough assurance for the risk of the action, with deterministic checks doing the
work that does not require model judgement.

The first concrete consumer is the proposed `review_pull_request` Job in ADR-0008. That Job may
ask one worker to review a revision, another to address findings, and then must determine whether
the resulting revision satisfies the review and repository gates.

## Proposed Decision

Assurance is proportional to risk and uses the smallest combination of these roles that provides
independent evidence:

- **Producer** creates a conclusion or change.
- **Challenger** searches for contradictions, defects, and unsupported assumptions.
- **Validator** checks the resulting artifact against explicit acceptance criteria and
  deterministic evidence.
- **Human authority** decides matters reserved by ADR-0006.

These are roles, not permanent agent types. One worker may perform more than one role only when
independence is not required for that outcome.

## Deterministic Minimums

Model judgement never replaces available deterministic checks. Depending on the change, assurance
includes the relevant subset of:

- exact repository revision checks;
- schema and semantic validation;
- tests, type checking, linting, and security gates required by project policy;
- evidence that findings refer to the reviewed revision;
- validation that external side effects reached their intended state;
- explicit authority checks for decisions or actions governed by ADR-0006.

An operational failure is not reported as uncertainty, and uncertainty is not coerced into a pass.

## Risk Profiles

The initial vocabulary is deliberately small:

- **Routine**: established, reversible work with no material security, data, architecture, or
  public-contract impact. Deterministic gates may be sufficient.
- **Standard**: meaningful implementation work requiring review plus deterministic validation.
- **Elevated**: security, data, architecture, irreversible, or public-contract impact requiring an
  independent challenger or validator and explicit authority where applicable.

No numeric risk score or configurable policy engine is introduced initially. The first Job may
encode a small deterministic policy and collect evidence for later refinement.

## Independence

Independence comes primarily from:

- fresh execution context;
- an objective different from producing or fixing the artifact;
- independently gathered repository evidence;
- validation against an exact revision and explicit criteria;
- inability to mark one's own work complete when policy requires another validator.

Different model providers may reduce some correlated errors, but provider diversity is optional
and is not proof of independence. Multiple votes over the same context are not an assurance model.

## First Job Constraint

For `review_pull_request`:

1. the review is bound to an exact PR revision;
2. a worker that changes the code does not perform the final independent validation;
3. deterministic repository gates run at the depth required by project policy;
4. validation confirms that material findings are fixed, explicitly rejected with evidence, or
   escalated;
5. correction and validation rounds are bounded;
6. unresolved disagreement or unavailable authority becomes a durable checkpoint, not forced
   consensus.

The review worker and final validator may use the same runtime implementation if they have fresh
contexts and distinct objectives. Whether a different model materially improves outcomes is an
evaluation question under ADR-0011.

## Audit and Evidence

Work Management contains findings and decisions needed to continue. Job state contains the
checkpoint and exact revisions needed to resume. Audit records only material assurance outcomes
as bounded by ADR-0013. Low-level prompts, votes, and tool traces are observability data or are
discarded.

## Not Decided Here

This proposal does not define:

- a generic multi-agent debate framework;
- an adjudicator for every disagreement;
- model-provider routing;
- numeric scoring or configurable risk rules;
- Job persistence or MCP contracts;
- authority beyond ADR-0006.

## Acceptance Evidence

Before this ADR is accepted, the first Job design should demonstrate:

- which review outcomes require independent validation;
- how exact-revision validation is enforced;
- which deterministic gates are mandatory;
- how bounded retries and human escalation behave;
- an evaluation comparing the chosen assurance flow with a simpler control arm.

## Consequences

### Positive

- Assurance effort follows material risk.
- Self-validation is prevented where it matters.
- Deterministic checks remain the primary evidence for deterministic claims.
- The first implementation does not require a general debate or voting system.

### Negative

- The initial policy is intentionally narrow and may need revision after evaluations.
- Independent validation adds cost and latency to non-routine work.
- Some outcomes will correctly stop for human input rather than complete autonomously.
