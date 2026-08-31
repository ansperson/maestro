# Maestro — Context

Domain vocabulary for this project. Agents and contributors rely on these terms meaning
exactly this. Where a term comes from an accepted ADR, the ADR is authoritative and this
entry is a summary.

## Planes

Maestro separates three planes (ADR-0004). They reference one another through stable
identifiers and never substitute for one another.

### Work Management Plane
Engineering intent and coordination: what needs doing, its status, and the decisions and
clarifications humans provide. Lives in an issue tracker, so a human and an agent can both
read and act on it without database access.

### Audit Plane
The semantic governance history of execution: what Maestro decided and did, why, on what
evidence, and with what outcome. Fail-closed — an audited execution cannot succeed without a
durable Trail (ADR-0005). Not a troubleshooting convenience: it is the evidence that
governance happened.

### Observability Plane
How execution technically happened. Structured logs to stderr, never the record of what was
decided.

## Decisions and authority

### Decision lifecycle
Requesting, proposing, approving, rejecting, and superseding a decision. Coordination between
humans and agents, so it belongs to Work Management.

### Decision application
The fact that one execution used a given decision. History, so it belongs to Audit. The
distinction from lifecycle is why a decision is not simply "stored in Audit".

### Decision block
The structured section inside a work item that Maestro reads as authority. Each entry states
what was decided, who decided it, and its scope. Everything outside the block is context.
Its purpose is to keep an observation about the code from being written into an artifact and
acquiring authority that ADR-0006 denies it.

### Scope
What a decision applies to, plus how long it is valid — this work item, this project, or
until superseded. Reuse requires an explicit match. Declared validity is also how authority
expires.

### Authority engine
The deterministic component that evaluates written rules and already-approved decisions, then
either clears an action or records that approval is required. Deterministic by design: an
engine that judged would move the judgement rather than remove it.

### Unblocker
The component a working agent calls when it cannot proceed. It consults the authority engine
and either clears the action or updates the work item to request approval, so the working
agent carries no escalation logic of its own. Requires durable Jobs to pause and resume
(ADR-0008); until then the flow completes by re-running after approval.

### Delegation
Writing a rule delegates that class of decision to automation. `AGENTS.md` is the existing
body of delegation. There is no separate delegation mechanism.

## Verification

### WorkItemPort
The port through which Maestro reads and writes work items, named but not specified by
ADR-0004. Keeps the tracker replaceable and keeps tracker mechanics out of the domain.

### AgentRuntime
The provider-neutral worker boundary. An adapter behind it declares its own name, version,
and distribution pins (ADR-0010). Adding a provider is an addition behind this boundary.

### Control arm
An evaluation run that answers the corpus without the tool, so a tool's promotion argument
stays falsifiable (ADR-0011). Its prose is converted to the tool's own claims by an
extractor, and one deterministic rubric scores both arms.

### Verifiability
Whether evidence resolves against the repository, whether the fingerprint proves the
repository was unchanged, and whether a Trail was recorded. Measured, never judged. These are
the properties that distinguish a tool from a plain model invocation.

## Recurring rule

### Assessment asymmetry
A model assessment may raise a requirement and never lower one. It holds for assurance level
(ADR-0007), for self-reported confidence, and for an agent judging whether it may unblock
itself (ADR-0006). Concluding "this needs more scrutiny" is safe; concluding "this needs
less" is the claim deciding whether the claim is checked.
