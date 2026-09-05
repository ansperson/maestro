# Maestro

Maestro is an engineering execution and control plane exposed through MCP.

> **Current implementation:** one read-only public tool, `resolve_codebase_fact`, plus an internal
> decision-authority service. Jobs and pull request orchestration are proposed, not implemented.

The public tool investigates one objective question about an allowed repository with a disposable
Claude or Codex worker. Maestro accepts the result only after strict schema, semantic,
repository-revision, and evidence validation. Material execution outcomes are persisted to
PostgreSQL Audit before success is returned.

For a precise implementation map, read [Current Architecture](docs/architecture.md). Future
direction is separated into [Vision](docs/vision.md), and architectural decisions are indexed in
the [ADR log](docs/adr/README.md).

## What It Does

`resolve_codebase_fact` is for questions whose answer already exists in repository evidence:

```text
Does the payment endpoint enforce idempotency?
Which module validates repository paths?
Is branch coverage enabled?
```

It is not an authority or recommendation tool. Questions such as “Should we use PostgreSQL?”
return `human_decision_required` rather than allowing the model to choose.

Each successful semantic response has:

- `status`: `resolved`, `uncertain`, or `human_decision_required`;
- `answer`: present for a resolved fact;
- `confidence`: `low`, `medium`, or `high` evidence strength;
- `evidence`: validated repository-relative paths and optional line ranges/symbols;
- `conflicts`: contradictory evidence when present;
- `reason`: a concise explanation of the outcome.

Operational failures are returned as typed MCP errors, not converted into `uncertain`.

## Requirements

- Python 3.13 or later;
- [`uv`](https://docs.astral.sh/uv/);
- Docker for the development PostgreSQL service;
- either a supported local Claude Code executable or explicit Codex authentication.

Install the locked environment:

```bash
uv sync --frozen --all-groups
```

## Quick Start

The Makefile configures Claude for the simplest local path and authorizes this checkout by
default:

```bash
make up REPO=/absolute/path/to/repository
make ask REPO=/absolute/path/to/repository Q="Is branch coverage enabled?"
```

`make up` creates separate local PostgreSQL role credentials, starts the pinned database on a
loopback-only port, bootstraps roles, and applies forward-only migrations. `make run` starts the
stdio server, `make read` queries curated Audit views, and `make clean` removes project containers,
volumes, and generated local credentials.

An MCP client normally launches the `maestro` executable and supplies the environment. Minimal
Claude configuration is:

```bash
MAESTRO_ALLOWED_ROOTS=/absolute/path/to/repository
MAESTRO_AGENT_RUNTIME=claude
MAESTRO_AUDIT_WRITER_HOST=localhost
MAESTRO_AUDIT_WRITER_PORT=5432
MAESTRO_AUDIT_WRITER_DATABASE=maestro
MAESTRO_AUDIT_WRITER_USER=maestro_audit_writer
MAESTRO_AUDIT_WRITER_PASSWORD_FILE=/absolute/path/to/writer-password
```

`MAESTRO_AGENT_RUNTIME` is required and accepts `claude` or `codex`; it has no default. Claude uses
the local executable's authentication. Codex requires exactly one explicit source:
`MAESTRO_CODEX_AUTH_FILE` or `MAESTRO_CODEX_API_KEY`. Authentication and Audit secrets must remain
outside every allowed repository root.

See [.env.example](.env.example) for all typed settings. Invalid configuration fails at startup.

## MCP Tool

```text
name: resolve_codebase_fact
readOnlyHint: true
openWorldHint: false
```

Input:

```json
{
  "repository_path": "/absolute/path/to/an/allowed/repository",
  "question": "Where is request concurrency bounded?",
  "context": "Optional untrusted background"
}
```

The repository path must resolve inside a configured canonical allowed root. Context and all
repository content are untrusted data; neither can override Maestro policy. Evidence paths are
normalized, repository-relative, and checked against the exact repository fingerprint inspected by
the worker.

The tool is inspection-only. It does not intentionally execute repository tests, builds, package
managers, scripts, binaries, hooks, or plugins.

## Runtime Boundary

Claude and Codex are adapters behind one provider-neutral `AgentRuntime` contract. A request runs
one bounded worker with an explicit minimal environment, no Maestro recursion, bounded output, and
read-only intent. Provider-specific imports and behavior do not leak into Capability code.

Native execution is the default. The hardened container remains available for deployments needing
the stronger outer boundary documented in [Container Execution](docs/container.md).

Model-provider communication is an explicit data-egress boundary. Runtime read-only settings and
MCP annotations are defense-in-depth metadata, not complete security guarantees. See
[Security](SECURITY.md) and the [Threat Model](docs/threat-model.md) for the complete controls and
residual risks.

## Decision Authority

ADR-0006 adds a deterministic authority engine and a GitHub `WorkItemPort` adapter. The development
entry point checks whether configured authority permits a proposed choice:

```bash
make authority \
  REPO=/absolute/path/to/repository \
  GITHUB_REPOSITORY=owner/name \
  ISSUE=123 \
  SUBJECT=storage.backend \
  CHOICE=postgresql
```

Only explicitly marked authority blocks in configured documents and the work item count. Conflicts
fail closed, and an unreadable tracker is never interpreted as absence of a decision. This service
is not currently a public MCP tool. Without durable Jobs, approval is followed by rerunning the
check.

## Audit

Audit records bounded semantic events for the shipped Capability and authority service. It is not
a transcript, workflow engine, Job store, artifact registry, or low-level observability system.

The current PostgreSQL and fail-closed behavior remain part of the implemented contract. New Audit
features are frozen until a concrete requirement demonstrates their value, and future Job progress
must not use Audit as resumable state or make a separate Audit write the ordinary transition
bottleneck. See [ADR-0013](docs/adr/0013-bounded-audit-boundary.md).

## Evaluation

The tool has versioned ground-truth cases and a model-backed control arm:

```bash
make eval                              # tool and control, three repetitions
make eval ARMS=tool                    # tool only
make eval REPS=5 EFFORT=low            # custom repetitions and control effort
```

These checks require configured provider access and are separate from deterministic CI. A tool
change is not validated against live model behavior unless the applicable E2E/evaluation was run.

## Development

The authoritative commands, change-class gates, MCP Inspector procedure, clean-wheel smoke test,
container security checks, and secret-baseline process live in [CONTRIBUTING.md](CONTRIBUTING.md).
The normal deterministic gate is:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest --strict-config --strict-markers --cov=maestro --cov-branch \
  --cov-report=term-missing --cov-report=xml --cov-fail-under=90
uv run vulture src tests --min-confidence 100
uv run deptry .
uv run pip-audit
```

Tests do not require a live AI provider. Changes to provider behavior, prompts, models, structured
output, or evidence semantics additionally require the applicable live E2E/evaluation.

## Documentation

- [Current Architecture](docs/architecture.md): implemented boundaries and data flow.
- [Vision](docs/vision.md): non-authoritative future direction.
- [ADR Log](docs/adr/README.md): current, proposed, and superseded decisions.
- [Contributing](CONTRIBUTING.md): developer procedures and quality gates.
- [Security](SECURITY.md): supported security posture and disclosure process.
- [Threat Model](docs/threat-model.md): trust boundaries and residual risks.
- [Container Execution](docs/container.md): hardened deployment profile.

License: [Apache-2.0](LICENSE).
