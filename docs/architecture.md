# Current Architecture

This document describes Maestro as implemented. Future direction belongs in
[Vision](vision.md); durable architectural rationale belongs in the
[ADR log](adr/README.md).

## Current Scope

Maestro is a local stdio MCP server with one public Capability:
`resolve_codebase_fact`. It answers objective questions about the current state of an explicitly
allowed repository and returns validated repository-relative evidence.

Decision authority from ADR-0006 is also implemented as an internal application service with a
GitHub `WorkItemPort` adapter and a development entry point, `make authority`. It is not a public
MCP tool.

Jobs, durable checkpoints, pull request orchestration, write-capable workers, remote MCP transport,
and additional public Capabilities are not implemented.

## Architectural Model

```text
Skills        = agent expertise and behavior
Agents        = disposable workers
Capabilities  = bounded reusable engineering primitives
Jobs          = durable orchestration toward an engineering outcome
Checkpoints   = durable pauses for human or external input
Integrations  = communication with external engineering systems
Maestro       = engineering execution and control plane
```

Only Agents, the `resolve_codebase_fact` Capability, bounded integrations, and supporting control
planes exist today. The fundamental ownership rule for future orchestration is:

> Agents are disposable workers. Jobs own durable state.

## Dependency Direction

```text
MCP adapter
    -> application Capability
        -> ports
            -> agent, repository, Audit, and integration adapters
```

The MCP handler validates and maps protocol data but contains no investigation policy. Runtime
specific behavior remains under `maestro.agents.claude` and `maestro.agents.codex`; the application
depends on their shared `AgentRuntime` protocol.

## `resolve_codebase_fact` Flow

```text
MCP request
  -> validate request and authorize canonical repository path
  -> acquire bounded execution capacity
  -> fingerprint the repository
  -> neutralize the question and record execution.started
  -> invoke one isolated read-only Claude or Codex worker
  -> validate structured output and repository-relative evidence
  -> confirm that the repository fingerprint did not change
  -> sanitize the result and enforce output bounds
  -> record investigation.completed
  -> return resolved, uncertain, or human_decision_required
```

Operational failures use typed errors and are not represented as epistemic uncertainty. Accepted
AI output must pass schema, semantic, evidence, and repository-revision validation.

The Capability intentionally does not run repository tests, builds, scripts, hooks, package
managers, or local binaries.

## Agent Runtime Boundary

`MAESTRO_AGENT_RUNTIME` explicitly selects `claude` or `codex`; there is no default.

- The Claude adapter invokes a supported local Claude Code executable with a bounded, read-only
  tool policy.
- The Codex adapter launches a private subprocess that owns one official asynchronous Codex SDK
  lifecycle and receives only its runtime-specific configuration.

Both implement the same provider-neutral request/result contract. Worker environments are built
from explicit allowlists, recursion is rejected, output and execution are bounded, and unrelated
credentials are not forwarded.

Native execution is the default deployment. A hardened container profile remains a tested outer
security adapter, not a dependency of the application model.

## Decision Authority

The authority service reads explicit authority blocks from configured repository documents and a
work item through `WorkItemPort`. The deterministic engine either permits the proposed choice,
requires human authority, or refuses conflicting authority. It does not ask a model to decide.

Only marked blocks are authority. Repository prose, model confidence, and a proposed action's own
classification cannot grant permission. An unreadable tracker fails closed. Applied authority is
recorded as `authority.applied`.

Pausing and resuming for approval are not implemented. Until Jobs exist, the operator reruns the
authority check after the work item has been updated.

## Information Planes

```text
Work Management = information needed to understand, authorize, or continue work
Job state        = information needed to resume execution safely (future)
Audit            = proof of material actions and outcomes
Observability    = operational and diagnostic detail
```

The current Audit implementation uses PostgreSQL and records the terminal behavior required by the
public Capability and authority service. Its scope is frozen by ADR-0013. Audit is not a transcript,
Job store, workflow engine, or source from which future transitions are reconstructed.

Structured logs go to stderr. stdout is reserved for the MCP protocol.

## Security Boundaries and Known Limits

Maestro enforces canonical allowed roots, traversal and symlink escape protection, repository
fingerprints, evidence file/line validation, strict output schemas, explicit runtime configuration,
bounded concurrency/time/output, and minimal worker environments.

Repository content is untrusted data and cannot override execution policy. Tool annotations and
model prompts are not treated as complete security boundaries.

The native runtime depends on the enforcement supplied by the selected agent runtime and host OS.
Model-provider communication is a data-egress boundary. PostgreSQL is currently required for Audit,
and the existing Capability fails closed when required Audit persistence cannot be established.
See [Security](../SECURITY.md), the [threat model](threat-model.md), and
[container controls](container.md) for the detailed claims and residual risks.

## Source Ownership

```text
src/maestro/mcp/                         MCP adapter
src/maestro/capabilities/resolve_codebase_fact/
                                         Capability contracts and application policy
src/maestro/agents/                      provider-neutral runtime port and adapters
src/maestro/repository/                  authorization, fingerprint, and evidence validation
src/maestro/authority/                   deterministic authority and WorkItem boundary
src/maestro/audit/                       bounded semantic Audit contracts and PostgreSQL adapter
src/maestro/execution/                   cross-capability execution admission
src/maestro/observability/               structured operational logging
src/maestro/config.py                    centralized typed configuration
```

## Documentation Map

- [README](../README.md): installation, operation, and public behavior.
- [Vision](vision.md): non-authoritative future direction.
- [ADR log](adr/README.md): decision status, rationale, and reading routes.
- [Contributing](../CONTRIBUTING.md): authoritative development and validation procedures.
- [Security](../SECURITY.md) and [threat model](threat-model.md): guarantees and residual risks.
