# Architecture Decision Log

This directory preserves Maestro's architectural decisions. Accepted decisions are historical
records: when the current decision changes, a new ADR supersedes the old one rather than rewriting
the path that produced the implementation.

For the system as it exists now, start with [Current Architecture](../architecture.md). Read an ADR
when changing the boundary it owns or when its rationale is needed.

## Current decisions

| ADR | Status | Current use |
| --- | --- | --- |
| [0001](0001-maestro-engineering-execution-platform.md) | Accepted | Core platform model: Skills, Agents, Capabilities, Jobs, Checkpoints, and Integrations. |
| [0002](0002-engineering-verifier-v1.md) | Accepted; extended by 0010 | `resolve_codebase_fact` contract and verifier boundary. |
| [0003](0003-hardened-local-container-execution.md) | Accepted; amended by 0009 | Hardened container boundary. Native execution is now the default. |
| [0004](0004-separate-work-management-audit-and-observability-planes.md) | Accepted | Separation of Work Management, Audit, and Observability. |
| [0005](0005-audit-as-a-first-class-governance-plane.md) | Superseded by 0013 | Historical Audit v1 implementation decision. |
| [0006](0006-decision-authority-and-human-approval.md) | Accepted | Deterministic decision authority and WorkItem boundary. |
| [0007](0007-assurance-challenge-and-independent-validation.md) | Proposed | Proportional assurance for future Jobs. Not implementation authority. |
| [0008](0008-adaptive-engineering-job-orchestration.md) | Proposed | Durable Job model and first `review_pull_request` Job. Not implementation authority. |
| [0009](0009-native-execution-is-the-default-deployment.md) | Accepted | Native runtime default; hardened containers retained and tested. |
| [0010](0010-multi-provider-worker-boundary.md) | Accepted | Provider-neutral runtime boundary with Codex and Claude adapters. |
| [0011](0011-tool-evaluation-policy.md) | Accepted | Ground-truth evals and model-backed control arms for tools. |
| [0012](0012-keep-audit-bounded-and-off-the-job-critical-path.md) | Superseded by 0013 | Historical decision that froze Audit expansion and removed it from the future Job critical path. |
| [0013](0013-bounded-audit-boundary.md) | Accepted | Current consolidated Audit scope and relationship to future Jobs. |

## Status meaning

- **Proposed**: under review and freely editable; it cannot authorize implementation.
- **Accepted**: current architectural authority unless superseded or amended.
- **Superseded**: retained as history; follow the linked replacement for current authority.
- **Rejected**: retained with rationale to avoid reopening the same path without new evidence.

## Reading rule

Do not load the full decision log by default. Select ADRs by the boundary being changed:

```text
public fact Capability or verifier -> 0002, 0010, 0011
repository/container security      -> 0003, 0009
work, Audit, and telemetry planes  -> 0004, 0013
decision authority                 -> 0006
future assurance and Jobs          -> 0007, 0008, 0013
```
