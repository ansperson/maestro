# Product and Architecture Vision

This document describes direction, not shipped behavior or implementation authority. The
[current architecture](architecture.md) and accepted [ADRs](adr/README.md) are authoritative for
the repository today.

## Why Maestro

Engineering agents are useful workers, but long-running outcomes require control outside one model
conversation. Maestro aims to provide that control: bounded engineering primitives, explicit
authority, durable coordination, independent validation, and integrations with the systems where
work already lives.

```text
Skills = expertise
Agents = disposable workers
Capabilities = reusable primitives
Jobs = durable outcomes
Checkpoints = explicit waits
Integrations = external engineering systems
```

The platform should add a primitive only when independent infrastructure creates measurable value.
A specialized workflow remains a Skill unless stable contracts, isolation, validation,
permissions, reuse, observability, or durable coordination justify promotion.

## Next Product Slice

The next intended architectural slice is a durable pull request review Job:

```text
review an exact PR revision
-> publish actionable findings
-> wait for or perform bounded corrections
-> validate the resulting revision independently
-> complete or stop at an explicit checkpoint
```

ADR-0008 is the proposal for that Job. It deliberately favors one fixed end-to-end workflow over a
generic orchestration engine. Persistence, write isolation, GitHub side-effect reconciliation,
resume semantics, and assurance must be decided before implementation is authorized.

## Later Possibilities

After the review Job demonstrates durable state and recovery, the same evidence may support an
issue implementation Job. Broader feature orchestration, adaptive routing, more integrations,
remote transport, and reusable scheduling should be considered only when concrete use cases expose
a stable shared boundary.

These possibilities are not commitments and must not be scaffolded speculatively.

## Guiding Principles

- Keep public Capabilities few, bounded, and evaluable.
- Keep provider behavior behind runtime ports.
- Treat repository content and AI output as untrusted.
- Put continuation information in Work Management and resumable state in Jobs.
- Keep Audit semantic, bounded, and outside ordinary Job transitions.
- Prefer deterministic validation to model judgement.
- Make authority explicit and fail closed on conflict or unreadable sources.
- Bind conclusions and actions to exact repository revisions.
- Start with a tracer-bullet workflow; generalize only after repeated evidence.
- Measure model-backed tools against recorded ground truth and a simpler control arm.

The desired result is not maximum autonomy. It is the smallest trustworthy control plane that can
carry an engineering outcome across disposable workers and real-world interruptions.
