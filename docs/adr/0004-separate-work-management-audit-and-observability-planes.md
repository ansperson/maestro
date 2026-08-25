# ADR-0004: Separate Work Management, Audit, and Observability Planes

* **Status:** Accepted
* **Date:** 2026-08-25
* **Decision owners:** Project maintainers
* **Related:** ADR-0001 — Maestro as an Engineering Execution Platform

## Context

Maestro is expected to evolve from a single bounded capability into an engineering execution platform capable of coordinating:

* AI agents;
* skills;
* capabilities;
* durable Jobs;
* human Checkpoints;
* external engineering systems.

As Maestro gains autonomy, several different categories of information will naturally be produced during engineering work.

For example, while implementing an issue, Maestro may need to track:

* what work was requested;
* decisions made during execution;
* evidence supporting those decisions;
* human decisions and checkpoints;
* PRs and commits created;
* validation outcomes;
* model calls;
* tool executions;
* latency;
* failures;
* token usage;
* execution traces.

These categories serve different purposes.

If they are stored in the same system or represented using the same model, several problems emerge.

A Jira issue or GitHub issue can become polluted with internal execution details.

An audit trail can become an unreadable dump of tool calls.

An observability system can accidentally become the only record of important engineering decisions.

The architecture therefore requires explicit separation between:

1. **Work Management**
2. **Audit**
3. **Observability**

## Decision

Maestro will treat Work Management, Audit, and Observability as three distinct architectural planes.

```text
                    Maestro

                       |
        +--------------+--------------+
        |              |              |
        v              v              v

 Work Management      Audit      Observability
      Plane            Plane          Plane

   intent and       semantic       technical
 coordination       governance      execution
```

Each plane has a different source of truth, data model, lifecycle, and purpose.

They may reference one another through stable identifiers, but they must not be used as substitutes for one another.

The core distinction is:

```text
Work Management
= what needs to be done

Audit
= what Maestro meaningfully decided and did

Observability
= how the execution technically happened
```

## Work Management Plane

The Work Management Plane represents engineering intent and coordination around work.

Typical systems include:

```text
Jira
GitHub Issues
Linear
other issue/task trackers
```

Typical information includes:

* issue title;
* problem statement;
* requirements;
* acceptance criteria;
* priority;
* assignee;
* workflow status;
* human comments;
* business context;
* externally visible progress.

The Work Management Plane answers questions such as:

> What work needs to be done?

> What is its current status?

> What are the requirements?

> What decisions or clarifications have humans provided?

Maestro may integrate with multiple work-management systems over time.

The architecture must not assume that Jira, GitHub Issues, or any other specific tracker is the canonical Maestro domain.

A future abstraction may expose external work items through a port such as:

```text
WorkItemPort
```

but the exact interface is not decided by this ADR.

## Audit Plane

The Audit Plane records the semantic governance history of Maestro execution.

It exists to answer questions such as:

> What did Maestro decide?

> What important actions did it take?

> Why was a decision made?

> What evidence supported it?

> Which human decisions influenced the outcome?

> What validation occurred?

> What was the final result?

Typical audit information includes:

* significant decisions;
* concise decision rationale;
* evidence references;
* important actions;
* state transitions;
* human checkpoints;
* external side effects;
* validation outcomes;
* security-relevant events;
* final outcomes;
* references to issues, PRs, commits, repositories, and executions.

The Audit Plane is intended to be:

* semantically meaningful;
* human-readable;
* structured;
* queryable;
* durable.

The detailed architecture and implementation of Audit are intentionally delegated to a separate ADR.

That ADR will initially have status:

```text
Proposed
```

until its implementation model has been reviewed against the current Maestro codebase.

## Observability Plane

The Observability Plane records technical execution telemetry.

It exists primarily for:

* debugging;
* troubleshooting;
* performance analysis;
* reliability analysis;
* model/runtime evaluation;
* operational monitoring.

Typical observability information may include:

* traces;
* spans;
* model calls;
* tool calls;
* latency;
* token usage;
* retries;
* exceptions;
* resource usage;
* runtime metadata;
* low-level execution timing.

Future implementations may use technologies such as:

```text
OpenTelemetry
Langfuse
other tracing/observability backends
```

This ADR does not select an observability backend.

Observability is not the authoritative record of Maestro's semantic decisions.

## Separation of Responsibilities

The three planes must remain conceptually distinct.

### Work Management is not Audit

Issue trackers should not become detailed execution journals.

For example, a Jira issue may contain:

```text
PAY-123

Implement partial payments.

Status: In Progress

Business decision:
Partial payments must use the Order currency.
```

It should not accumulate:

```text
Agent inspected file A.
Agent searched symbol B.
Verifier called tool C.
Verifier changed hypothesis.
Model response ...
```

Those details either belong in Audit or Observability.

### Audit is not Observability

Audit must not become a dump of every execution event.

Events such as:

```text
file read
grep executed
tool invocation
token generated
model latency
```

normally belong to Observability.

Audit should contain only events with semantic or governance significance.

### Observability is not Audit

A trace backend must not become the only place where important engineering decisions are recorded.

Trace retention, sampling, backend replacement, or telemetry failures must not erase the authoritative semantic history of a Maestro execution.

## Correlation

Although the planes remain separate, they must be correlatable.

Future Maestro entities may use identifiers such as:

```text
job_id
audit_id
execution_id
trace_id
work_item_ref
```

Example:

```text
Jira PAY-123
     |
     +---- Job job_456
              |
              +---- Audit aud_789
              |
              +---- Execution exec_101
                       |
                       +---- Trace trace_202
```

The identifiers provide relationships without forcing one plane to duplicate the full contents of another.

## External References

Maestro should prefer references over duplication.

For example, an Audit record referencing a Jira issue may contain information equivalent to:

```text
provider: jira
type: issue
id: PAY-123
```

rather than copying the entire Jira issue into the Audit Store.

Likewise:

```text
provider: github
type: pull_request
repository: example/payments
id: 891
```

may reference a PR.

External systems remain responsible for their own canonical content.

## Human Decisions

Human decisions may originate through Work Management, a future Maestro interface, or another integration.

When a human decision materially affects Maestro execution:

```text
Work Management / Human Interface
               |
               v
        human decision
               |
        +------+------+
        |             |
        v             v
      Job           Audit
    resumes      decision recorded
```

The Work Management Plane may contain the conversation or clarification relevant to the task.

The Audit Plane records the semantic fact that the decision affected execution.

The two records should be correlated rather than duplicated indiscriminately.

## Chain of Thought

Neither the Audit Plane nor the Work Management Plane exists to persist private model chain-of-thought.

Maestro must not treat raw internal model reasoning as an audit requirement.

Where rationale is useful, Maestro should record a concise, externally meaningful decision rationale.

For example:

```text
Decision:
Classified the billing relationship as uncertain.

Rationale:
Current implementation permits multiple billing accounts, but ADR-017
documents a 1:1 invariant and no superseding migration was found.

Evidence:
- ...
```

This is an appropriate Audit record.

A raw transcript of the model's private reasoning is not.

Technical prompts/responses may exist temporarily or within observability tooling when explicitly required for debugging and permitted by policy, but they are not the canonical semantic audit record.

## Failure Independence

The planes should evolve toward independent failure semantics.

For example:

```text
Observability backend unavailable
```

should not automatically imply:

```text
engineering Job cannot continue
```

Likewise, failure to update a work-management system may not necessarily invalidate an already completed engineering action.

Audit failure semantics are more sensitive because Audit may become a governance requirement.

Whether an Audit write failure blocks execution, retries, buffers events, or degrades gracefully is deliberately not decided here.

That behavior belongs to the Audit-specific design.

## Architectural Ports

The expected architectural direction is:

```text
                   Maestro Application

                           |
          +----------------+----------------+
          |                |                |
          v                v                v

    WorkItemPort        AuditPort      TelemetryPort

          |                |                |
          v                v                v

       Jira /           Audit Store       OTel /
    GitHub / etc.                        Langfuse /
                                          etc.
```

The exact interfaces are not defined by this ADR.

The important decision is that these responsibilities remain independent ports/adapters rather than becoming coupled to each other.

## Storage Independence

This ADR does not select:

* Jira;
* GitHub Issues;
* PostgreSQL;
* SQLite;
* Langfuse;
* OpenTelemetry;
* pgweb;
* any other specific backend.

Those technologies belong to their respective implementation/design decisions.

The domain-level separation remains valid regardless of which adapters are selected.

## Human Readability

The Audit Plane must ultimately provide a human-readable path for understanding Maestro's meaningful execution history.

This does not mean the Work Management or Observability planes must expose the same information.

A future Audit implementation may expose information through:

* MCP read capabilities;
* CLI;
* database inspection;
* generic database UI;
* dedicated Maestro Audit UI.

The exact experience is outside this ADR.

## Examples

### Repository Fact Resolution

```text
Work Management
    none or external issue reference

Audit
    investigation started
    fact resolved
    evidence recorded
    result recorded

Observability
    model call
    tool executions
    latency
    token usage
```

### PR Review Job

```text
Work Management
    GitHub PR / linked issue

Audit
    review completed
    findings accepted
    fixes applied
    validation passed
    final outcome

Observability
    agent executions
    model/tool traces
    timing
    errors/retries
```

### Issue Implementation Job

```text
Work Management
    Jira PAY-123
    requirements
    human comments
    workflow status

Audit
    clarification required
    human decision received
    implementation decision
    PR created
    review findings
    findings addressed
    final validation
    outcome

Observability
    model calls
    subprocesses
    latency
    token usage
    traces
```

## Consequences

### Positive

This separation:

* keeps issue trackers readable;
* prevents semantic Audit from becoming an execution trace dump;
* prevents observability systems from becoming the only record of decisions;
* allows each plane to have appropriate retention and storage policies;
* permits independent backend replacement;
* improves future human inspection;
* supports correlation across issues, Jobs, agents, and traces;
* preserves clear hexagonal boundaries.

### Negative

The architecture introduces multiple related data planes rather than one universal event store.

Correlation identifiers must be designed carefully.

Some information may intentionally appear in more than one plane in different forms.

Future Jobs will need policies describing which events belong in which plane.

Operational complexity will eventually increase as adapters and persistence are implemented.

These costs are accepted because the three responsibilities have materially different consumers and semantics.

## Non-Goals

This ADR does not define:

* the Audit event schema;
* the Audit database;
* the WorkItemPort interface;
* Jira or GitHub adapter implementation;
* an observability backend;
* OpenTelemetry conventions;
* Langfuse integration;
* Audit retention;
* Audit failure semantics;
* Audit UI;
* Audit MCP query tools;
* Audit summary generation;
* multi-tenancy;
* authentication for future UIs.

Those decisions require separate design work.

## Follow-up Decisions

The next related architectural decision will define:

```text
Audit as a First-Class Governance Plane
```

That ADR will initially be **Proposed** and will define the expected Audit semantics, including:

* automatic recording;
* semantic event selection;
* decision rationale;
* append-oriented history;
* `AuditPort`;
* external references;
* human readability;
* storage expectations;
* future summaries;
* read/query capabilities.

Dedicated Work Management and Observability ADRs should only be created when Maestro has concrete decisions in those areas.

## Decision Summary

Maestro will maintain three separate but correlatable planes:

```text
Work Management
= what needs to be done

Audit
= what Maestro meaningfully decided and did

Observability
= how execution technically happened
```

No plane should be used as a substitute for another.

This separation is an accepted domain-level architectural invariant and should guide future Maestro Jobs, capabilities, integrations, persistence, and governance design.
