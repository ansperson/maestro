Implement **Maestro Level 2 hardened local container execution**.

The existing Maestro v1 implementation is already functional and must remain architecturally unchanged.

This task is a security/deployment hardening layer around the existing application.

Do not redesign `resolve_codebase_fact`.

Do not introduce a Docker-specific dependency into Maestro domain/application code.

## Source of truth

Before changing anything:

1. Read `AGENTS.md`.
2. Read `README.md`.
3. Read every ADR under `docs/adr/`.
4. Read `SECURITY.md`.
5. Read `docs/threat-model.md`.
6. Read `docs/discovery.md`.
7. Inspect the existing MCP, runtime, repository security, admission, and Codex adapter implementation.
8. Inspect the existing CI and deterministic quality gates.
9. Validate the current official Docker documentation for the security controls used by this task.

Preserve all existing v1 behavior and public contracts.

The current public capability remains exactly:

```text id="mwyux1"
resolve_codebase_fact
```

No public MCP schema change is expected.

---

# Why this task exists

The current native execution model already provides:

```text id="oa4goj"
Maestro
+
Codex Sandbox.read_only
+
repository authorization
+
before/after repository fingerprints
+
evidence validation
```

However, `Sandbox.read_only` is not currently considered a reliable filesystem security boundary.

The upstream Codex runtime has a known issue in which managed edit operations may persist filesystem changes despite `read_only`.

Maestro therefore needs an additional operating-system-level boundary.

The intended Level 2 model is:

```text id="k9wz5y"
MCP Client
    |
    | stdio
    v
+--------------------------------+
| Hardened Maestro container     |
|                                |
| Maestro MCP                    |
|      ↓                         |
| EngineeringVerifier            |
|      ↓                         |
| CodexRuntime                   |
|      ↓                         |
| Codex worker                   |
|                                |
| repository mounts = READ ONLY  |
| root filesystem   = READ ONLY  |
| temp state        = ephemeral  |
+--------------------------------+
```

This is **defense in depth**.

Do not remove or weaken existing Maestro controls merely because Docker is added.

The desired chain is:

```text id="orhl0q"
verifier policy
+
Codex sandbox
+
container isolation
+
read-only repository mount
+
repository fingerprint validation
+
evidence validation
```

---

# Deployment modes

After this task Maestro should support two documented execution modes.

## Native development mode

Existing behavior:

```text id="q5kj3f"
Maestro running directly on host
```

Useful for development and troubleshooting.

Its current security limitations remain documented.

## Hardened container mode

Recommended for repository investigation:

```text id="6xkdpu"
Maestro inside hardened container
+
authorized repositories bind-mounted read-only
```

Do not remove native mode.

Do not make application/domain code aware of which deployment mode is used.

---

# Architectural constraint

Docker surrounds Maestro.

Do NOT introduce architecture such as:

```text id="305ero"
EngineeringVerifier
    ↓
DockerRuntime
    ↓
CodexRuntime
```

for v1.

Prefer:

```text id="ng79if"
Docker
└── Maestro
    └── EngineeringVerifier
        └── AgentRuntime
            └── CodexRuntime
```

Containerization is an operational/deployment boundary.

`AgentRuntime` remains the AI provider/runtime port.

---

# Expected artifacts

Implement the smallest coherent set of artifacts.

Likely artifacts include:

```text id="nnfi37"
Dockerfile
.dockerignore

scripts/
└── maestro-container   # if justified

docs/
└── container.md

container-specific tests

CI additions
```

A `compose.yaml` is optional.

Do not add Docker Compose merely because Docker is used.

Add it only if it materially improves the local MCP workflow without duplicating a simpler launcher.

---

# Dockerfile

Use a production-quality multi-stage Dockerfile.

Requirements:

* use an official/trusted minimal Python base appropriate to the project;
* preserve the project's supported Python version;
* use multi-stage construction;
* build/install from the existing locked dependency graph;
* final image must not contain development dependencies;
* final image must not contain compilers/build tools unless runtime genuinely requires them;
* install only runtime OS packages actually required by Maestro/Codex investigation;
* run Maestro as a non-root user;
* use exec-form `ENTRYPOINT`/`CMD`;
* use a minimal final filesystem;
* set `PYTHONDONTWRITEBYTECODE=1`;
* avoid unnecessary caches;
* avoid copying `.git`, local environments, credentials, coverage files, caches, editor files, or secrets into the image.

Investigate which system tools the Codex verifier actually needs.

Potential legitimate read-only investigation tools may include:

```text id="x0qwcg"
git
ripgrep
basic POSIX utilities
```

Do not install a broad developer toolchain by default.

Do not install package managers or build tools solely because an investigated repository might use them.

`resolve_codebase_fact` remains inspection-only.

---

# Reproducible dependency installation

The container build must respect `uv.lock`.

Do not perform an unconstrained dependency re-resolution inside Docker.

Use the current recommended `uv` mechanism for installing the locked production dependency set.

The resulting image must use the same pinned MCP and Codex runtime versions expected by Maestro startup checks.

Preserve existing runtime-version mismatch protections.

---

# Base image pinning

Use a trusted official base image.

Prefer pinning the runtime base image by immutable digest while retaining a readable version tag, unless current project dependency automation provides a clearly better reproducible strategy.

If digest pinning is used, configure dependency automation to keep it maintainable.

Do not use `latest`.

Document the update process.

---

# Docker build checks

Use current Docker BuildKit/Dockerfile build checks.

Make Dockerfile check violations fail validation where supported.

Prefer an explicit Dockerfile syntax version if required to make check behavior deterministic.

Add a project command/documented gate equivalent to:

```text id="6mompy"
docker build --check .
```

or the current supported Buildx equivalent.

Do not add a redundant Dockerfile linter unless official build checks demonstrably leave an important gap.

---

# Runtime filesystem policy

The hardened container must run with a **read-only root filesystem**.

Conceptually:

```text id="1d4n5x"
--read-only
```

Only explicitly necessary ephemeral locations should be writable.

Use tmpfs for ephemeral runtime state.

Likely examples:

```text id="dkvsa1"
/tmp
temporary worker home/configuration
```

Use restrictive tmpfs options such as:

```text id="57ocaa"
nosuid
nodev
```

where supported.

Evaluate `noexec` experimentally.

Use it where Maestro/Codex functions correctly, but do not force it if the runtime legitimately requires executable temporary content.

If `noexec` cannot be used, document why.

No persistent application state should be written inside the container during normal v1 operation.

---

# Repository mounts

Authorized repository roots must be mounted **read-only at the container boundary**.

Use bind mounts with read-only semantics.

The container must never receive a read-write mount of an investigated repository for `resolve_codebase_fact`.

Preserve the existing `MAESTRO_ALLOWED_ROOTS` security model.

## Path compatibility

Avoid changing the public `repository_path` contract merely because Docker is introduced.

Prefer mounting configured allowed roots at the **same absolute path inside the container** when the host/container platform supports that safely.

Conceptually:

```text id="jdhr0t"
host:
/Users/example/Developer

container:
/Users/example/Developer
```

with:

```text id="xexfvh"
readonly=true
```

This allows:

```text id="vtyp9x"
repository_path
```

to mean the same thing in native and container modes.

If same-path mounts cannot be implemented robustly on the supported platform, introduce the smallest deployment-layer path mapping necessary.

Do not push host/container path translation into the Maestro domain.

The tool caller must never control arbitrary Docker mounts.

Only configured allowed roots may become mounts.

---

# Host path constraints

Document platform-specific filesystem sharing requirements.

For Docker Desktop/macOS, authorized host roots must be locations Docker Desktop is permitted to mount.

For native Linux, document ownership/UID considerations.

Container execution must remain non-root.

If host UID/GID mapping is necessary for reliable read access, implement it safely at the launcher/deployment layer and test it.

Do not solve permission problems by running Maestro as root.

---

# Container privilege policy

The hardened runtime must use least privilege.

Require or preserve equivalents of:

```text id="0xs3nc"
non-root user

--cap-drop=ALL

--security-opt=no-new-privileges=true
```

Do not run:

```text id="s3l7qp"
--privileged
```

Do not add Linux capabilities unless a concrete runtime requirement is proven.

Any required added capability must be:

* individually justified;
* documented;
* tested;
* treated as a security decision.

No Docker socket may be mounted into the Maestro container.

Do not expose:

```text id="d157wx"
/var/run/docker.sock
```

or equivalent container-engine control sockets.

Do not use host PID, host IPC, host user namespace, or host networking merely for convenience.

Do not pass host devices into the container.

---

# Seccomp and LSM policy

Keep Docker's default seccomp profile enabled.

Do not use:

```text id="3qz905"
seccomp=unconfined
```

Do not disable the default AppArmor/SELinux confinement where the host platform provides it.

Do not create a custom seccomp/AppArmor profile in this task unless testing demonstrates a specific material requirement or a clearly beneficial tightening that can be maintained safely.

The initial Level 2 objective is to compose reliable standard Docker isolation controls, not invent a bespoke sandbox policy.

On native Linux, document that rootless Docker provides an additional daemon/runtime hardening option and should be preferred where available.

Do not make rootless Docker mandatory for Docker Desktop or environments where it does not apply cleanly.

---

# Process lifecycle

Use an init/reaper mechanism where appropriate so Codex child processes are correctly reaped and signals are forwarded.

The container must shut down cleanly on SIGTERM/SIGINT.

Existing Maestro graceful shutdown semantics must continue to:

* stop admission;
* cancel queued work;
* cancel active work;
* terminate Codex child processes;
* clean temporary state.

Test this through Docker, not only natively.

---

# Resource limits

Docker containers have effectively unbounded host CPU/memory access unless constrained.

The recommended hardened launcher must configure explicit resource limits.

Cover at least:

```text id="bpr4cy"
memory
CPU
PIDs
```

Choose conservative defaults based on actual Maestro/Codex behavior.

Do not select arbitrarily tiny values merely to appear secure.

Make operational limits configurable at the **deployment layer**, not through the Maestro domain model.

Examples of launcher configuration may include equivalents to:

```text id="ptjz87"
MAESTRO_DOCKER_MEMORY
MAESTRO_DOCKER_CPUS
MAESTRO_DOCKER_PIDS_LIMIT
```

Document defaults and rationale.

Test that resource limits do not break a normal investigation.

---

# Networking

Do **not** use:

```text id="h7r45l"
--network=none
```

for the entire Maestro container if doing so prevents the Codex SDK from reaching the configured model provider.

Distinguish:

```text id="i40jwx"
Maestro/Codex → model-provider communication
```

from:

```text id="ri4z6r"
agent shell/tools → arbitrary internet
```

The former is required for hosted model execution.

The latter should remain disabled by the existing runtime policy.

Do not use host networking.

Do not publish ports.

MCP remains stdio.

Document the residual risk that container-level networking cannot currently distinguish model-provider egress from arbitrary process egress without a separate controlled-egress design.

Controlled egress/proxying is Level 3 future hardening, not part of this task.

---

# Authentication and secrets

Never bake authentication material into:

* Docker image layers;
* Dockerfile `ENV`;
* build arguments;
* image labels;
* source files;
* build context.

Do not mount the user's complete home directory.

Do not mount the user's complete:

```text id="zs39k0"
~/.codex
~/.ssh
~/.aws
~/.config
```

into the container.

Inspect Maestro's current explicit Codex authentication flow and preserve the principle of **minimum credential exposure**.

If runtime credentials are file-based, prefer mounting only the dedicated credential material required by Maestro, read-only.

If runtime credentials are environment-based, pass only the required variables.

The containerized worker must continue using isolated temporary Codex configuration/home state rather than inheriting a general user configuration.

Update `.dockerignore` to aggressively exclude likely secrets and local authentication material.

---

# Build-time secrets

No build-time credentials should normally be required.

If discovery proves one is required, use BuildKit secret mounts.

Never use `ARG` or baked environment values for secrets.

---

# Supply-chain security

Existing:

```text id="4wyc5v"
pip-audit
deptry
detect-secrets
```

remain required.

Containerization introduces OS/image dependencies, so add one dedicated **container image vulnerability/security scan**.

Prefer a local/open scanner such as Trivy unless the project already has a better existing solution.

Do not add multiple overlapping container scanners.

The image scan should cover at least:

* OS/package vulnerabilities;
* secrets accidentally included in the image.

Define a documented failure policy for serious known vulnerabilities.

Do not hide findings through broad ignore rules.

Any ignore entry must be specific and justified.

---

# Image contents

Verify that the final runtime image does NOT contain:

```text id="57ln80"
.git/
.env files
developer credentials
Codex auth copied from host
coverage output
test caches
local virtualenv
source-control secrets
Docker socket
build credentials
```

Use image inspection/history tests where useful.

No secret may exist in an intermediate layer that ends up reachable in the final image.

---

# Local launcher

Provide a safe and ergonomic way for MCP clients to launch hardened Maestro over stdio.

A small launcher is preferred over requiring users to manually reconstruct a long `docker run` command.

The launcher should:

1. read explicitly configured allowed roots;
2. canonicalize them;
3. mount only those roots;
4. mount them read-only;
5. preserve same-path mounts where supported;
6. pass only allowed Maestro configuration and auth;
7. configure filesystem hardening;
8. configure privilege restrictions;
9. configure resource limits;
10. keep stdin/stdout attached for MCP stdio;
11. use `--rm`;
12. avoid shell-string construction from untrusted values.

Prefer an implementation that safely handles paths containing spaces and other valid characters.

Do not construct the Docker command through unsafe shell interpolation.

If implemented as Python, keep it clearly as deployment tooling rather than Maestro application/domain code.

If implemented as shell, use arrays/strict quoting and test edge cases.

---

# MCP client portability

Document how the containerized stdio server can be consumed by at least:

```text id="cxst4d"
Codex
Claude Code
```

using their current supported MCP configuration mechanisms.

Verify current official client configuration syntax before documenting examples.

Do not make Maestro dependent on either client.

Conceptually both clients should launch the same hardened wrapper:

```text id="oi5fac"
Codex ───────┐
             ├─ stdio → hardened Maestro container
Claude Code ─┘
```

---

# No public API changes

Containerization must not change:

* MCP server identity unnecessarily;
* tool name;
* tool schema;
* `structuredContent`;
* semantic statuses;
* repository fact semantics;
* error semantics.

Existing MCP contract snapshot tests should continue to pass unchanged.

If a public contract change appears necessary, stop and justify it before implementing it.

---

# Tests

Containerization requires dedicated deterministic tests.

Add tests that verify the actual built image/runtime, not just Dockerfile text.

At minimum validate:

## Image build

```text id="r0q9pm"
Dockerfile build checks PASS
image builds successfully
runtime image starts
```

## Non-root

Inside the runtime container:

```text id="3b39qu"
effective UID != 0
```

## Read-only root filesystem

Attempting to write an arbitrary path in the image filesystem must fail.

## Writable ephemeral area

The explicitly configured temp area must remain writable.

## Read-only repository mount

With a synthetic fixture repository mounted read-only:

```text id="603kmq"
read existing file → succeeds
create file         → fails
modify file         → fails
delete file         → fails
```

Verify from the **host after container exit** that fixture contents are unchanged.

## Repository access

`resolve_codebase_fact` must still be able to inspect the mounted synthetic repository.

## Authorization

A path outside mounted/configured allowed roots must still fail before AI execution.

## stdio

Through the container:

```text id="hqxcr7"
tools/list
```

must work with machine-parseable stdout and no log corruption.

Use a credential-free semantic path such as `human_decision_required` where possible to exercise the tool without model-provider access.

## Signals

Terminate the container while work is active or simulated.

Verify clean shutdown and no orphaned host/container processes.

## Resource constraints

Verify the recommended launcher actually applies configured memory/CPU/PID limits.

## No excessive privileges

Inspect the running container and verify:

```text id="f42xh1"
non-root
capabilities dropped
no-new-privileges active where observable
no privileged mode
no host networking
no published ports
no Docker socket
repository mounts readonly
root filesystem readonly
```

---

# Real Codex container security E2E

Keep this separate and opt-in because it requires model-provider authorization.

Using **only a synthetic fixture repository**, add an E2E/security test that validates the exact boundary we introduced Docker to provide.

The test should cause a real Codex worker to attempt a controlled write inside the read-only mounted fixture repository.

The test must verify externally that:

```text id="e9xrmq"
host repository before == host repository after
```

regardless of whether the Codex managed-edit path attempts the mutation.

Do not run this against a real/private repository.

Do not send private repository content merely to perform this test.

If provider authorization is unavailable, keep the test present and report it as unexecuted rather than weakening it.

---

# Existing test/quality gates

All existing Maestro deterministic gates remain mandatory.

Containerization must not reduce Python coverage or quality standards.

Run the full existing gate, including:

```text id="y5tm6h"
Ruff
Pyright strict
pytest + branch coverage
Vulture
deptry
pip-audit
detect-secrets
pre-commit
package build/check
```

Then run the new container gates.

---

# Suggested new container gates

Add maintainable project commands/scripts for equivalents of:

```text id="g8yndv"
Dockerfile build checks
container image build
container smoke tests
container security assertions
container vulnerability/secret scan
```

Avoid giant opaque shell scripts.

Each gate should fail clearly and explain what invariant was violated.

---

# CI

Extend CI without weakening existing jobs.

The container CI path should:

1. run Dockerfile build checks;
2. build the hardened runtime image;
3. execute deterministic container security/smoke tests;
4. scan the final image for vulnerabilities and embedded secrets.

Do not inject real Codex/model credentials into ordinary PR CI.

Real AI container E2E remains opt-in/trusted-environment only.

Pin third-party GitHub Actions by immutable commit SHA according to the existing repository policy.

If the base Docker image is digest-pinned, configure Dependabot or the project's dependency automation to track Docker base-image updates.

---

# Documentation

Update:

```text id="6bnsm4"
README.md
SECURITY.md
docs/threat-model.md
AGENTS.md
```

and add a focused container guide such as:

```text id="1t37km"
docs/container.md
```

Document:

* native vs hardened execution;
* what the container actually protects;
* what it does NOT protect;
* repository mount model;
* authentication setup;
* allowed roots;
* resource limits;
* model-provider egress;
* Codex MCP setup;
* Claude Code MCP setup;
* troubleshooting;
* Docker Desktop/macOS notes;
* native Linux notes;
* how to run all container quality gates.

Do not claim that Level 2 provides perfect sandboxing.

Residual risks must remain explicit.

---

# ADR

This task changes Maestro's security/deployment model in a durable way.

Create a new ADR following the repository's existing numbering/conventions.

The ADR should record approximately:

```text id="e2pmsv"
Decision:
Hardened container execution is the recommended repository-investigation
deployment mode.

Native execution remains supported for development.

Docker/containerization is an outer deployment/security boundary,
not part of Maestro domain architecture.

Repositories are mounted read-only.

The container root filesystem is read-only.

Workers remain subject to existing Maestro and Codex runtime controls.

Container networking still permits required model-provider egress and is
not yet a controlled-egress security boundary.

Higher-assurance isolation may require future per-worker containers,
controlled egress, or stronger OS policies.
```

Do not rewrite previous ADR history.

---

# AGENTS.md update

Extend `AGENTS.md` minimally with permanent container invariants.

Future agents should be told that hardened container execution must not be weakened casually.

Capture rules such as:

```text id="z2sjow"
- containerization is an outer adapter/security boundary;
- repository mounts for read-only capabilities remain read-only;
- root container filesystem remains read-only;
- containers run non-root;
- no privileged mode;
- no Docker socket;
- default seccomp/LSM confinement remains enabled;
- container security tests are mandatory when Docker behavior changes.
```

Do not duplicate the entire container guide into `AGENTS.md`.

---

# Do not overengineer

Do NOT implement as part of this task:

```text id="ntvy66"
Kubernetes
container orchestration platform
per-job container scheduler
ContainerRuntime domain port
remote MCP
service mesh
custom network proxy
custom seccomp profile
custom AppArmor profile
Vault
database
durable Jobs
PR Jobs
Issue Jobs
Terraform orchestration
```

The objective is one hardened local container execution path for the existing Maestro v1.

---

# Discovery requirement

Before implementation, explicitly validate current official Docker behavior for:

* read-only bind mounts;
* read-only root filesystem;
* non-root execution;
* capability dropping;
* no-new-privileges;
* default seccomp;
* AppArmor/SELinux defaults;
* PID/resource limits;
* rootless Docker where applicable;
* BuildKit Dockerfile checks;
* supported image vulnerability scanning approach.

If the current platform cannot enforce a requested control, do not fake it.

Implement the strongest supported control and document the residual risk.

---

# Development order

Use approximately:

```text id="au5seu"
1. Read repository policies/docs/current implementation.
2. Validate current Docker security/build APIs.
3. Write concise discovery/security notes.
4. Define container threat boundary.
5. Create Dockerfile and .dockerignore.
6. Build image.
7. Establish non-root/read-only runtime.
8. Design safe allowed-root mounting/launcher.
9. Wire minimal auth/config injection.
10. Add deterministic container security tests.
11. Validate MCP stdio inside container.
12. Add image security scan.
13. Add optional real Codex read-only-mount E2E.
14. Extend CI.
15. Write container documentation.
16. Add ADR.
17. Update SECURITY/threat model/AGENTS.
18. Run existing full quality gate.
19. Run all container gates.
20. Review complete diff for unnecessary complexity.
```

Do not perform unrelated refactors.

---

# Acceptance criteria

Do not declare Level 2 complete unless all applicable existing Maestro gates pass **and** the following are demonstrated:

```text id="5py12p"
[ ] Dockerfile official build checks pass
[ ] image builds reproducibly from locked dependencies
[ ] runtime container runs non-root
[ ] container root filesystem is read-only
[ ] authorized repository mounts are read-only
[ ] temporary runtime state is ephemeral
[ ] cap-drop=ALL or equivalent effective policy is applied
[ ] no-new-privileges is enabled
[ ] default seccomp remains enabled
[ ] AppArmor/SELinux confinement is not intentionally disabled
[ ] no privileged mode
[ ] no Docker socket
[ ] no host networking
[ ] no exposed ports
[ ] memory limit applied
[ ] CPU limit applied
[ ] PID limit applied
[ ] MCP stdio works through container
[ ] existing MCP schema snapshot remains unchanged
[ ] allowed-root security remains effective
[ ] container cannot mutate synthetic host repository
[ ] image contains no embedded credentials
[ ] image vulnerability/secret scan passes project policy
[ ] graceful shutdown works
[ ] Codex/Claude Code container-launch documentation is validated
[ ] native mode still works
[ ] README/SECURITY/threat model/ADR/AGENTS are consistent
```

The real Codex managed-edit security E2E is required to exist but may remain explicitly unexecuted if model-provider authorization is unavailable.

Do not silently substitute deterministic Docker write tests for that live-runtime E2E; report them separately.

---

# Final report

At completion report:

## Discovery

* Docker/BuildKit versions and platform tested;
* relevant Docker security features available;
* important platform limitations.

## Image

* base image and pinning strategy;
* build strategy;
* runtime packages;
* final image size;
* runtime user.

## Isolation

* root filesystem policy;
* repository mounts;
* writable tmpfs locations;
* capabilities;
* no-new-privileges;
* seccomp;
* AppArmor/SELinux;
* resource limits;
* networking;
* authentication exposure.

## MCP

* how Codex and Claude Code launch containerized Maestro;
* confirmation public tool contract remained unchanged.

## Security validation

* deterministic write-prevention results;
* host repository before/after verification;
* image scan results;
* secrets checks;
* live Codex managed-edit E2E status.

## Quality

* existing Maestro gates;
* Docker build checks;
* container tests;
* CI results.

## Documentation

* ADR added;
* SECURITY/threat-model changes;
* AGENTS changes;
* container guide.

## Residual risks

Clearly state anything Level 2 still cannot guarantee.

Do not describe Level 2 as equivalent to a hostile-code sandbox unless the implementation actually demonstrates that guarantee.
