# ADR-0010: Multi-Provider Worker Boundary

Date: 2026-08-30

Status: Accepted

Extends: ADR-0002

## Context

ADR-0002 placed a runtime adapter behind the verifier and stated that Codex would be the first
implementation. It remains the only one. The Codex runtime has no available credit, so no
investigation can complete and the Capability cannot be exercised end to end.

`AgentRuntime` was already runtime-neutral, and the provider decoupling that followed ADR-0009 made
startup verification and Audit identity read from the adapter that supplies them rather than from
literals. What remains is a second adapter.

The candidate is the locally installed `claude` binary. It was measured against the fixture
repository before this decision:

* `--print --output-format json` returns an envelope whose `result` validated against
  `VerificationResult` with `strict=True` on the first attempt.
* Given the adversarial fixture, it reported the injected instruction as evidence instead of
  obeying it, and chose `human_decision_required` without an answer, as the contract requires.
* With `--allowedTools "Read,Glob,Grep"` and the write and execution tools disallowed, an explicit
  instruction to create a file and run a script produced neither. The repository tree was
  byte-identical before and after.
* `permission_denials` was empty in that same run. A disallowed tool is absent rather than
  refused, so there is nothing to deny and the field reports no attempt.
* Each investigation cost roughly USD 0.20 to 0.36 of subscription usage.

Authentication is what makes the binary attractive and what constrains it. The binary resolves the
operator's own credential, so Maestro never holds a provider secret. On macOS that credential is in
the operating-system keychain, which is why ADR-0009 placed containerization on hold.

## Decision

A worker adapter is the unit of provider support. Adding one is an addition behind `AgentRuntime`
and `AgentRuntimeProvider`; it does not change the Capability, the public MCP contract, the Audit
schema, or the prompt policy.

Maestro does not handle provider credentials. An adapter that wraps a local tool inherits the
operator's authentication from that tool and passes no credential, token, or key of its own. When
the tool is not authenticated, the adapter fails closed with a typed operational error naming the
command the operator must run. Maestro cannot prompt for a login: it is a stdio server with no
interactive channel during a tool call.

The permitted tool set is a parameter of the adapter invocation, not a fixed property of the
provider. `resolve_codebase_fact` is an inspection Capability and receives read access only,
because it reports repository state and must not have produced the state it reports. Capabilities
that legitimately modify a repository, such as the future `implement_issue`, will pass a different
set under their own decision record. Read-only enforcement remains two independent layers, as with
the Codex adapter's deny-all read-only sandbox: the tool set the adapter grants, and the
before-and-after repository fingerprint that the Capability already validates.

A provider's reported refusal of a blocked action is not evidence. Adapters must not treat a
provider's own denial metadata as the control that proves no write occurred; the fingerprint
comparison is that control.

The model identity an adapter runs under is pinned by the adapter and recorded in the Audit trail
through the existing `model` field, so a Trail states which model produced a result rather than
whichever default the tool happened to select.

Provider selection becomes a typed setting once a second adapter exists, with an explicit value per
supported provider and no silent fallback. A deployment verifies only its selected provider's pins.

## Consequences

### Positive

- The Capability becomes exercisable end to end using authentication the operator already has,
  with no metered credential in Maestro.
- Provider risk is reduced: an unusable or withdrawn provider is replaced by adding an adapter.
- Read-only enforcement stops depending on a single provider's sandbox implementation.

### Negative

- Investigation cost is real subscription usage, so end-to-end tests stay opt-in and few, and the
  typed fake remains the default for application tests.
- Each adapter carries its own output-conformance risk, since providers differ in how reliably they
  produce a strict structured result.
- An adapter that shells out to a local tool inherits that tool's release cadence and command-line
  surface, which is a weaker contract than a pinned SDK.
- Containerized deployment remains held for a local-tool adapter on macOS, per ADR-0009.

## Rejected alternatives

- A metered provider API key: contradicts using the operator's existing subscription, which is the
  reason for wrapping a local tool.
- Reading the operator's credential and passing it to the provider: gives Maestro a rotating secret
  it is designed never to hold, and would have to be redacted everywhere.
- Adopting a provider's full agent harness with its built-in tools: its default surface includes
  write and execution tools, making read-only a matter of configuration rather than of the tools
  the adapter chose to grant.
- Treating provider denial metadata as proof that no write occurred: measured empty for a blocked
  attempt, because absent tools produce no denial.
- A prompt policy specialized per provider: ADR-0002 requires the verifier's policy to originate
  from Maestro, and per-provider variants would let worker differences change semantics.
