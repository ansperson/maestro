# Maestro

Maestro is an engineering execution platform for AI-assisted software development.

> **Current implementation scope: `resolve_codebase_fact` only.** The Job, Checkpoint,
> PR/Issue orchestration, external integration, remote transport, and
> multi-agent concepts described later in this architectural README are future direction and
> are not implemented in v1.

## Maestro v1

V1 is a local stdio MCP server with one deterministic tool catalog entry. It accepts an
authorized repository path and one objective question, runs at most one isolated Codex
investigation, validates every evidence anchor against a stable repository fingerprint, and
returns one of `resolved`, `uncertain`, or `human_decision_required`.

Runtime requirements are Python >= 3.13 and `uv`. The implementation uses the official MCP
Python SDK v2, the official `openai-codex` Python SDK and its pinned runtime, Pydantic v2,
`pydantic-settings`, and `asyncio`. The development gate uses pytest, pytest-asyncio,
pytest-cov, pytest-timeout, Hypothesis, Ruff, Pyright strict, Vulture, deptry, pip-audit,
pre-commit, and detect-secrets. Packaging is defined in `pyproject.toml`, locked by `uv.lock`,
and uses a `src/` layout.

The v1 package mirrors the boundaries that already own behavior:

```text
src/maestro/
├── mcp/                                  # stdio/MCP adapter
├── audit/                                # contracts, recorder, PostgreSQL adapter/migrations
├── capabilities/resolve_codebase_fact/  # contracts, policy, sanitization, service
├── repository/                           # authorization, isolated fingerprint helper, evidence guard
├── agents/                               # runtime protocol and Codex adapter
├── execution/                            # admission and lifecycle control
├── observability/                        # structured stderr logging
├── config.py
├── errors.py
└── versions.py
```

Transport depends on the capability service, the service depends on the agent-runtime
protocol and repository/execution controls, and only the Codex adapter imports the official
Codex SDK. Capability models remain with `resolve_codebase_fact`; no generic contract bucket
or placeholder package exists for future features.

### Install and run

```bash
uv sync --frozen --all-groups
cp .env.example .env  # copy values into your launcher; Maestro does not load this file itself

MAESTRO_ALLOWED_ROOTS=/absolute/repository/root \
MAESTRO_CODEX_AUTH_FILE=/absolute/path/to/codex-auth.json \
MAESTRO_AUDIT_DATABASE_URL=postgresql://audit-writer@localhost/maestro \
uv run maestro
```

`MAESTRO_ALLOWED_ROOTS` is required and accepts multiple canonical roots separated by the OS
path separator (`:` on POSIX). Each root must be a directory below the filesystem anchor;
the anchor itself (`/` on POSIX, or the platform equivalent) is rejected. Configure exactly
one explicit Codex authentication source:
`MAESTRO_CODEX_AUTH_FILE` or `MAESTRO_CODEX_API_KEY`. Neither is inherited by repository
shell commands. `MAESTRO_AUDIT_DATABASE_URL` is also required. It is validated at startup;
connectivity is checked lazily when an audited call starts, and normal startup never migrates the
database. stdout is reserved for newline-delimited MCP protocol messages; structured JSON
application logs go to stderr.

An MCP client configuration can launch the server with `uv`:

```json
{
  "command": "uv",
  "args": ["--directory", "/absolute/path/to/maestro", "run", "maestro"],
  "env": {
    "MAESTRO_ALLOWED_ROOTS": "/absolute/repository/root",
    "MAESTRO_CODEX_AUTH_FILE": "/absolute/path/to/codex-auth.json",
    "MAESTRO_AUDIT_DATABASE_URL": "postgresql://audit-writer@localhost/maestro"
  }
}
```

For repository investigations, the recommended Level 2 mode runs the same stdio server inside
the hardened Linux container:

```bash
docker build --check .
docker build --tag maestro-verifier:local .

export MAESTRO_ALLOWED_ROOTS=/absolute/repository/root
export MAESTRO_CODEX_AUTH_FILE=/absolute/path/to/codex-auth.json
.venv/bin/python scripts/maestro_container.py
```

The launcher mounts only canonical allowed roots, read-only at the same absolute paths, and
applies a read-only root filesystem, ephemeral no-exec tmpfs, non-root identity, dropped
capabilities, no-new-privileges, init, bridge networking without ports, and memory/CPU/PID
limits. Docker remains outside `src/maestro`; native behavior and the MCP contract are unchanged.
See `docs/container.md` and ADR-0003 for setup, client configurations, supported platforms,
tests, and residual risks.

### Public tool contract

Tool metadata is stable at server version `1.0.0`:

```text
name: resolve_codebase_fact
title: Resolve codebase fact
readOnlyHint: true
destructiveHint: false
openWorldHint: false
```

Example input:

```json
{
  "repository_path": "/absolute/allowed/repository",
  "question": "Does customer_id currently have a uniqueness constraint?",
  "context": "This arose during a design review."
}
```

Example structured result:

```json
{
  "status": "resolved",
  "answer": "customer_id has a unique constraint.",
  "confidence": "high",
  "evidence": [
    {
      "path": "migrations/004_customer.sql",
      "line_start": 8,
      "line_end": 8,
      "symbol": "customers_customer_id_key",
      "finding": "The migration declares a unique constraint on customer_id."
    }
  ],
  "conflicts": [],
  "reason": "The current schema migration directly establishes the constraint."
}
```

`confidence` describes evidence strength, not correctness probability. `resolved` requires an
answer and validated evidence. `uncertain` means the investigation completed but evidence was
missing or contradictory. `human_decision_required` is reserved for normative/authority
questions and never includes an answer. Operational failures are typed tool errors, never
`uncertain`: `INVALID_INPUT`, `REPOSITORY_NOT_ALLOWED`, `REPOSITORY_NOT_FOUND`,
`REPOSITORY_CHANGED_DURING_INVESTIGATION`, `SERVER_BUSY`, `AGENT_TIMEOUT`,
`AGENT_CANCELLED`, `AGENT_RUNTIME_ERROR`, `INVALID_AGENT_OUTPUT`,
`EVIDENCE_VALIDATION_ERROR`, `RECURSION_NOT_ALLOWED`, `OUTPUT_LIMIT_EXCEEDED`, and
`AUDIT_UNAVAILABLE`, `AUDIT_PERSISTENCE_ERROR`, and `INTERNAL_ERROR`.

Audit is fail-closed. Maestro attempts each start or terminal write at most three times within
one five-second budget, with fixed 100 ms and 250 ms backoffs. Only failures established as
transient and not committed are retried. Exhausted availability failures return
`AUDIT_UNAVAILABLE`; permanent or commit-ambiguous failures return `AUDIT_PERSISTENCE_ERROR`.
Without a durable start, neither normative evaluation nor the AI worker runs. Without an
established durable completion, Maestro withholds the semantic result.

After a durable start, a typed operational failure is recorded as the single sequence-two
`execution.failed` event before the original operational error is returned. Its payload contains
only the safe error code, lifecycle stage, and approved runtime/version metadata. If that terminal
write also fails, the Audit operational error takes precedence. Cooperative cancellation remains
the exception to error precedence: after owned worker cleanup, Maestro attempts the failure write
in a separate task. The PostgreSQL adapter registers the failure-event identity before connecting;
on timeout it marks a pending connection aborted or synchronously finishes the active libpq
connection before task cancellation. The one-second cooperative persistence-and-drain budget
reserves time for that abort and reap. Maestro then joins the task to quiescence and always
propagates the caller's original cancellation. If connection finish itself fails, that joined drain
can extend past the cooperative budget rather than leave orphan work. A durable start without
either terminal event is explicitly incomplete. Abrupt process loss can therefore leave an
incomplete Trail; startup does not reconcile it or invent an outcome.

Stated conservatively, this is a one-second cooperative persistence-attempt budget followed by
joined quiescence, not an unconditional hard wall-clock return bound.

### Configuration and bounds

Defaults are a 300-second deadline, two concurrent workers, four queued callers, 4,000
question characters, 8,000 context characters, 128 KiB worker stdout/stderr, 64 KiB public
result, 20 evidence items, 10 conflicts, 10,000 discovered files, 64 MiB aggregate discovery,
and 1 MiB per file. Every limit is environment-configurable with the corresponding
`MAESTRO_` setting in `.env.example`. Invalid configuration fails before the server starts.
Allowed roots are canonicalized and filesystem anchors are prohibited; repository requests for
an anchor fail with the existing `REPOSITORY_NOT_ALLOWED` public error.

`MAESTRO_CODEX_MODEL` is an Audit- and log-safe identifier, not free-form metadata. It must begin
with an ASCII letter or digit, contain only ASCII letters, digits, dots, underscores, or hyphens,
and be at most 128 characters. This deliberately rejects URI credentials, filesystem identities,
secret assignments, controls, unsafe Unicode, and prose before startup.

File discovery does not follow symlinks, skips `.git`, dependency/build caches, binary,
non-UTF-8, and oversized content, and preserves an explicitly requested subdirectory rather
than widening it to a Git root. Git inspection disables prompts, pagers, hooks, external
diffs, filesystem monitors, global/system configuration, and optional locks. Nested
repositories and submodules are inspected only as files below the authorized root; they are
not initialized or traversed through Git. Evidence always uses normalized repository-relative
paths.

### Security boundary and limitations

Each AI call runs in an owned child process with a fresh temporary `HOME` and `CODEX_HOME`, a
minimal allowlisted environment, no inherited MCPs/skills/plugins, no project instructions,
no apps, web search, hooks, goals, memories, subagents, or escalation. Thread and turn both
select `deny_all` approvals and `read_only`. Repository content, caller text, Git history, and
model output remain untrusted data. Results are redacted, bounded, schema-checked, evidence-
checked, and rejected if the repository changes. Durable Audit text redaction detects configured
repository roots, selected private absolute-path forms, credential-bearing URI user information,
common secret forms, and unsafe controls from the same original input before applying bounded
replacements once. It is heuristic and does not establish a general data-loss-prevention boundary.

These controls do not make the cloud investigation offline: selected repository content is
sent to the configured model provider. The SDK has no repository-only confidentiality
boundary, hard shell-command allowlist, or maximum tool-action count. Its shell can
technically run repository-controlled code despite policy, and an upstream 0.147.0 report
states that managed file edits can bypass `read_only`. Native Maestro detects repository
mutation but cannot prevent or undo it. The hardened local-container mode adds a read-only Linux
mount boundary, minimal image, no-exec temporary filesystem, namespace isolation, and resource
limits and is recommended for repository investigation. Provider egress and all configured
allowed-root reads remain possible. See `SECURITY.md`, `docs/container.md`,
`docs/threat-model.md`, and `docs/discovery.md` before using sensitive repositories.

### Development and validation

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest --strict-config --strict-markers --cov=maestro --cov-branch \
  --cov-report=term-missing --cov-report=xml --cov-fail-under=90
uv run vulture src tests --min-confidence 100
uv run deptry .
uv run pip-audit
uv run pre-commit run detect-secrets --all-files
uv build
```

Container changes additionally require the built-image gates documented in `docs/container.md`:
official Dockerfile checks, image build, deterministic container security tests, Trivy image
package/secret scanning, and real stdio MCP Inspector validation.

Real Codex checks are deliberately separate and require explicit credentials:

```bash
uv run pytest -m e2e
uv run python scripts/run_evals.py
```

The real stdio server can be checked with the current MCP Inspector CLI. Inspector launches
the target with only the environment passed through its `-e` options:

```bash
npx --yes @modelcontextprotocol/inspector@2.2.0 --cli \
  /absolute/path/to/maestro/.venv/bin/maestro \
  -e MAESTRO_ALLOWED_ROOTS=/absolute/repository/root -e MAESTRO_LOG_LEVEL=WARNING \
  -e MAESTRO_AUDIT_DATABASE_URL=postgresql://audit-writer@127.0.0.1:1/maestro \
  --method tools/list --format json
```

Use `--method tools/call --tool-name resolve_codebase_fact --tool-args-json '<json>'` for a
call. A normative question with the deliberately unavailable example database is the
credential-free fail-closed smoke path and returns `AUDIT_UNAVAILABLE`. `CONTRIBUTING.md`
contains the complete local, package, schema, and secret-baseline workflow.

It coordinates AI agents, skills, engineering capabilities, external systems, and human decisions to carry engineering work from intent to a verified outcome.

Maestro is not a replacement for agent skills.

Skills continue to define how individual agents perform specialized work. Maestro provides the execution infrastructure that coordinates those agents when work spans multiple independent executions, systems, or human checkpoints.

Examples include:

```text
Question
  → investigate
  → validate evidence
  → answer

Pull Request
  → review
  → address findings
  → validate independently
  → complete

Issue
  → clarify
  → request human decisions
  → implement
  → test
  → review
  → address
  → validate
  → complete
```

## Why Maestro Exists

AI coding workflows frequently require several independent agents to collaborate.

Without orchestration, the user often becomes the orchestrator:

```text
Agent A
  ↓
result

User copies result

Agent B
  ↓
result

User opens another session

Agent C
  ↓
validation
```

Maestro removes this coordination burden.

Instead:

```text
User / Coding Agent
        |
        v
      Maestro
        |
        +-- Agent A
        |
        +-- Agent B
        |
        +-- Agent C
        |
        v
Verified Outcome
```

The user should participate when human judgment is actually required, not because multiple agent executions need manual coordination.

## Core Concepts

Maestro distinguishes four primary concepts:

```text
Skills
Capabilities
Jobs
Checkpoints
```

### Skills

Skills define how an agent performs a specialized role.

Examples:

```text
grill
pr-review
pr-address
implementation
architecture-review
```

A skill may define:

* reasoning methodology;
* workflow behavior;
* review criteria;
* interaction style;
* domain rules;
* when a human decision is required.

Skills remain independently usable outside Maestro.

Maestro does not absorb a skill merely because it can execute it.

### Capabilities

Capabilities are reusable bounded operations with explicit inputs and outputs.

Example:

```text
resolve_codebase_fact
```

Conceptually:

```text
input
  ↓
operation
  ↓
structured output
```

Capabilities may be deterministic or AI-backed.

An AI-backed capability may run an isolated agent while still exposing a narrow, stable contract.

Example:

```text
resolve_codebase_fact(
  repository,
  question
)
```

may internally:

```text
spawn isolated verifier
        ↓
inspect repository
        ↓
validate evidence
        ↓
return structured result
```

### Jobs

Jobs coordinate multiple executions toward an engineering outcome.

A job may combine:

* agents;
* skills;
* capabilities;
* repositories;
* external integrations;
* state;
* retries;
* policies;
* human checkpoints.

Examples:

```text
review_pull_request
implement_issue
investigate_incident
```

Jobs are not simply large prompts.

They are explicit orchestration state machines.

### Checkpoints

A checkpoint represents a point where execution cannot or should not continue automatically.

The most important example is:

```text
WAITING_FOR_HUMAN
```

Human involvement is an explicit workflow state, not an execution failure.

For example:

```text
Grill Agent
    |
    | finds unresolved business decision
    v
Maestro
    |
    v
Checkpoint
WAITING_FOR_HUMAN
    |
    | question published to Jira
    |
    ... time passes ...
    |
    | human answers
    v
Maestro resumes Job
```

The active agent does not need to remain alive while waiting.

## Fundamental Model

Maestro follows this model:

```text
Skills
= agent expertise and behavior

Agents
= disposable workers

Capabilities
= reusable engineering primitives

Jobs
= durable orchestration

Checkpoints
= explicit pauses requiring external input

Maestro
= engineering execution and control plane
```

A central architectural principle is:

> Agents are disposable workers. Jobs own state.

The state of an engineering task must not depend on a specific agent process remaining alive.

## Architecture

At a high level:

```text
                    Maestro
          Engineering Execution Platform

                         |
        +----------------+----------------+
        |                |                |
        v                v                v
     Jobs          Capabilities      Integrations
        |                |                |
        |                |          Jira / GitHub /
        |                |          Terraform / etc.
        |                |
        +--------+-------+
                 |
                 v
             Agent Runtime
                 |
        +--------+--------+
        |        |        |
        v        v        v
      Agent    Agent    Agent
        |        |        |
        v        v        v
      Skill    Skill    Skill
```

MCP is initially one interface through which clients access Maestro.

```text
Codex
  |
  | MCP
  v
Maestro
```

MCP is not the architecture of Maestro itself.

The application and domain layers must remain independent from the transport so that Maestro capabilities and jobs can later be exposed through other interfaces if needed.

## Initial Capability

The first capability is:

```text
resolve_codebase_fact
```

Its purpose is:

> Independently investigate an objective question about the current state of an allowed repository and return a structured answer backed by validated evidence.

Example:

```text
resolve_codebase_fact(
  question =
    "Can an Order currently have multiple Payments?"
)
```

Possible result:

```text
status: resolved
confidence: high

answer:
The current model supports multiple Payments per Order.

evidence:
- src/domain/payment.py
- migrations/0042_payments.sql
- tests/test_payments.py
```

If the question is:

```text
Should an Order support multiple Payments?
```

the expected result is:

```text
human_decision_required
```

Maestro discovers facts.

It does not invent product decisions.

## Pull Request Review Job

A PR workflow is a useful example of why Maestro exists.

The skills may remain:

```text
pr-review
pr-address
pr-validate
```

Each one defines how an agent performs its role.

Maestro coordinates them:

```text
                    PR HEAD abc123
                         |
                         v
                  Review Agent
                skill: pr-review
                   READ ONLY
                         |
                         v
                     findings
                         |
                         v
                 Address Agent
               skill: pr-address
                     WRITE
                         |
                         v
                  tests / lint
                         |
                         v
                  commit + push
                         |
                         v
                   HEAD def456
                         |
                         v
                Validation Agent
              skill: pr-validate
                   READ ONLY
                         |
                   +-----+-----+
                   |           |
                   v           v
                 PASS         FAIL
                   |           |
                   |      bounded retry
                   |           |
                   v           v
                COMPLETE   human attention
```

The reviewer, addressor, and validator are independent workers.

The final validator must not merely trust the addressor's claim that findings were resolved.

It validates the resulting repository state independently.

## Pull Request Revision Safety

A PR job must track repository identity and revision.

For example:

```text
initial HEAD = abc123
```

Before applying changes, Maestro verifies that the PR still points to the expected revision.

If another actor updates the PR:

```text
abc123 → xyz999
```

Maestro must not silently continue applying conclusions derived from stale code.

Depending on policy, the Job may:

```text
restart review
pause
fail safely
request human attention
```

After the address stage:

```text
final HEAD = def456
```

The validation agent evaluates that exact revision.

## Future Issue Implementation Job

A future `implement_issue` Job may coordinate an entire engineering task:

```text
Issue
  |
  v
Load Context
  |
  v
Grill Agent
skill: grill
  |
  +-- repository facts
  |       |
  |       v
  |   resolve_codebase_fact
  |
  +-- human decision needed
          |
          v
      update Jira
          |
          v
   WAITING_FOR_HUMAN
          |
          v
        resume
          |
          v
Implementation Agent
          |
          v
Tests / Lint
          |
          v
Create / Update PR
          |
          v
PR Review Job
          |
          v
Final Validation
          |
          v
Update Issue
          |
          v
COMPLETE
```

This is a durable Job.

It may execute over minutes, hours, or days.

No individual agent needs to remain alive for the entire lifecycle.

## Job State

Jobs should have explicit state.

A likely model is:

```text
QUEUED
RUNNING
WAITING_FOR_HUMAN
WAITING_FOR_EXTERNAL
BLOCKED
FAILED
COMPLETED
CANCELLED
```

State transitions must be explicit and validated.

A Job should retain enough information to resume execution safely.

Potential persisted artifacts include:

```text
issue context
repository revision
grill result
human decisions
implementation plan
commits
test results
PR
review findings
address results
validation results
```

## Human Checkpoints

Human intervention should occur for decisions requiring authority rather than factual investigation.

Examples include:

```text
product behavior
business requirements
architecture trade-offs
breaking compatibility
risk acceptance
destructive infrastructure operations
```

A Job may publish the question through an integration such as Jira.

Conceptually:

```text
Job
  |
  v
request_human_input(...)
  |
  v
Jira Adapter
  |
  v
Human
```

The Job then persists:

```text
WAITING_FOR_HUMAN
```

When a response is received, the Job resumes from the checkpoint.

## Autonomy Is Bounded by Policy

End-to-end execution does not mean unlimited authority.

Jobs should operate within explicit policies.

Example:

```text
allowed:
  read repository
  create working branch
  modify working tree
  run tests
  create commits
  push feature branch

approval required:
  destructive migration
  breaking public API
  ambiguous business decision
  production infrastructure change
  secret modification

not allowed:
  bypass required checks
  silently modify protected branches
  deploy production without policy authorization
```

Permissions should be enforced technically where possible.

## Independent Agents

Agents used by Maestro should be independent when independence improves reliability.

For example:

```text
Reviewer
  ↓
findings

Addressor
  ↓
changes

Validator
  ↓
independent verdict
```

The validator should evaluate:

```text
original intent
+
final diff
+
repository state
+
tests
```

rather than simply consuming:

```text
"The address agent says everything is fixed."
```

This reduces confirmation bias.

## Bounded Retries

Agent workflows must not create unbounded loops.

For example:

```text
Reviewer
   ↓
Addressor
   ↓
Validator
   |
   +-- PASS → COMPLETE
   |
   +-- FAIL
          |
          v
      Addressor
          |
          v
      Validator
```

with:

```text
max_address_rounds = 2
```

After the configured limit:

```text
HUMAN_ATTENTION_REQUIRED
```

or another explicit terminal/checkpoint state.

## Security Model

AI instructions are not security boundaries.

Where possible, Maestro must enforce:

```text
read-only agents
write-scoped agents
network restrictions
allowed repositories
secret isolation
tool restrictions
```

For example:

```text
Reviewer
filesystem: read-only
GitHub: read

Addressor
filesystem: write worktree
GitHub: push feature branch

Validator
filesystem: read-only
GitHub: read
```

Agents receive the minimum permissions required for their stage.

## Repository Evidence

AI-generated evidence is untrusted until validated.

If an agent reports:

```text
src/domain/order.py:92
```

Maestro should verify that:

* the file exists;
* it belongs to the expected repository;
* the path cannot escape the repository;
* the referenced line is valid.

A factual conclusion should not become trusted merely because an AI reports high confidence.

## Integrations

Maestro should integrate with existing systems rather than unnecessarily reimplement them.

Potential integrations include:

```text
GitHub
Jira
Terraform
CI providers
cloud platforms
observability systems
```

Where a mature external MCP server or API already exists, Maestro should prefer composition.

For example:

```text
Maestro
   |
   +-- internal orchestration
   |
   +-- internal agentic capabilities
   |
   +-- GitHub integration
   |
   +-- Terraform integration
```

The Job remains responsible for orchestration.

External systems remain responsible for their domain-specific operations.

## Skills vs Maestro

The default remains:

```text
new specialized behavior
        |
        v
      Skill
```

A skill does not become Maestro infrastructure merely because Maestro can execute it.

Promote reusable primitives when they materially benefit from:

* isolated execution;
* stable contracts;
* deterministic validation;
* independent runtime;
* reuse;
* external invocation;
* permissions enforcement;
* observability;
* reliability controls.

Use a Maestro Job when the recurring problem is:

> Several independent workers, capabilities, integrations, and human decisions must be coordinated toward one durable engineering outcome.

## Non-Goals

Maestro is not:

* a replacement for agent skills;
* a repository of prompts;
* a generic `ask_ai` service;
* an unrestricted autonomous coding agent;
* a reason to duplicate mature external integrations;
* a reason to expose every workflow as an MCP tool;
* a monolithic implementation of every engineering system.

## Guiding Principles

### Skills define expertise

Agents should be able to perform specialized work through independently maintained skills.

### Agents are disposable

An agent execution is a worker, not the owner of the task.

### Jobs own state

The engineering task survives agent restarts, process failures, and human waiting periods.

### Human checkpoints are first-class

Waiting for a legitimate human decision is a valid state.

### Validation should be independent

An agent responsible for validating work should not blindly trust the agent that produced it.

### Autonomy is bounded

Permissions and retries must be explicit.

### Prefer strong primitives over many tools

A small number of reliable capabilities is more valuable than a large catalog of overlapping prompts exposed as tools.

### Orchestrate outcomes, not conversations

Maestro should coordinate engineering execution.

Agent-specific reasoning methodology and conversational behavior remain primarily in skills.

## Vision

Maestro's long-term purpose is:

> Turn engineering intent into a verified outcome by coordinating disposable agents around durable jobs, reusable capabilities, external systems, explicit policies, and human checkpoints.
