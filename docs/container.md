# Hardened local container execution

Maestro Level 2 runs the unchanged local stdio MCP server inside a hardened Linux container.
The container is the recommended repository-investigation mode because its read-only bind mount
prevents writes that application-level fingerprint detection can only discover after the fact.
Native execution remains supported for development and troubleshooting.

ADR-0003 defines the durable decisions. This guide owns operator procedures and commands.

## Boundary and non-goals

The container enforces a read-only root and repository view, non-root execution, dropped
capabilities, no privilege escalation, default seccomp/available LSM confinement, private process
namespaces, no host networking or published ports, no Docker socket/devices, bounded temporary
state, and memory/CPU/PID limits. Existing repository authorization, Codex sandboxing, mutation
fingerprints, and evidence validation remain active inside it.

Level 2 does not provide provider-only egress, per-worker containers, confidentiality between
multiple configured allowed roots, protection from a compromised Docker daemon/host kernel, or
byte-identical OCI builds. It adds no Jobs, persistence, integration, subagent, or remote MCP
behavior.

## Requirements and tested platforms

- Docker Engine with BuildKit/buildx and Linux containers.
- Linux kernel 5.12 or newer for recursively read-only bind mounts.
- A host path shared with the Docker daemon/VM.
- Python 3.13 project environment for the deployment launcher.
- Trivy 0.74.0 for the local image scan.

CI validates native Linux/amd64. Local discovery and validation used Colima Linux/arm64 on macOS.
Docker Desktop on macOS follows the same container command but has its own filesystem-sharing and
VM configuration. Windows containers are unsupported.

Colima exposes the user's home by default; paths outside `/Users/$USER` require an explicit Colima
mount and restart. Colima may reject mount roots containing spaces in configurations that require
a new VM mount. The launcher passes spaces safely as single arguments, but it cannot override an
engine restriction. Use a shared parent without spaces or change the VM mount configuration; do
not interpolate a Docker command through a shell.

On native Linux, prefer rootless Docker when available and verify it under `Security Options` in
`docker info`. Rootless mode is not required for Docker Desktop or Colima.

## Build

The Dockerfile pins the Dockerfile frontend, official Python base manifest, and official uv image
manifest. `uv.lock` selects the production dependency graph; the project is installed
non-editably and development tools are absent from the runtime stage. The final image adds only a
version-pinned Trixie Git client so Maestro's existing HEAD/dirty-state fingerprint remains
active, then removes the unused system Python package installer. Ripgrep and a general developer
toolchain are intentionally absent; bounded standard utilities remain available for inspection.

```bash
docker build --check .
docker build --tag maestro-verifier:local .
```

`# check=error=true` makes official Dockerfile findings fatal. Dependabot tracks the Dockerfile
references. Review base updates with the complete container gate; do not update only one repeated
Python base reference.

The base is the current Debian Trixie slim variant rather than Bookworm oldstable. Git's direct
version is pinned, and its transitive packages resolve from the same immutable Debian/security
snapshot recorded by the pinned official Python image. Review the two base references, snapshot
timestamp, Git version, and scanner database together rather than updating only one input.

The pinned inputs and lockfile make the selected dependency graph reproducible. OCI layer bytes
may still differ between builders because timestamps and builder metadata are not normalized.

## Configure and run

Build first, then configure canonical host paths:

```bash
export MAESTRO_ALLOWED_ROOTS=/absolute/path/to/authorized/repositories
export MAESTRO_CODEX_AUTH_FILE=/absolute/path/to/codex-auth.json
export MAESTRO_DOCKER_IMAGE=maestro-verifier:local

.venv/bin/python scripts/maestro_container.py
```

Multiple allowed roots use the host path separator (`:` on supported hosts). The launcher resolves
each root before Docker starts, rejects missing/non-directory roots and ambiguous comma-bearing
`--mount` paths, and mounts only those roots at identical absolute paths with
`readonly,bind-recursive=readonly,bind-propagation=rprivate`. The MCP caller cannot add mounts.

The image defaults to UID/GID 65532. The launcher normally overrides this with the invoking
non-root UID/GID so read-only host files remain readable. A launcher invoked as root fails closed;
set `MAESTRO_DOCKER_UID` and `MAESTRO_DOCKER_GID` to a deliberate non-root identity that can read
the roots and authentication file. Do not solve permissions by selecting UID 0 or making mounts
writable.

The preferred credential is one regular, non-symlink auth file. It is mounted read-only at
`/run/maestro-auth/auth.json`; the complete home and `.codex` directory are never exposed. API-key
forwarding remains compatible through `MAESTRO_CODEX_API_KEY`, but Docker records environment
values in inspectable daemon metadata, so it is not the recommended hardened mechanism. Configure
exactly one source.

The deployment settings are:

| Variable | Default | Purpose |
|---|---:|---|
| `MAESTRO_DOCKER_IMAGE` | `maestro-verifier:local` | Built image reference |
| `MAESTRO_DOCKER_MEMORY` | `2g` | Container memory limit |
| `MAESTRO_DOCKER_CPUS` | `2` | CPU quota |
| `MAESTRO_DOCKER_PIDS_LIMIT` | `256` | Process limit |
| `MAESTRO_DOCKER_TMPFS_SIZE` | `512m` | Ephemeral `/tmp` upper bound |
| `MAESTRO_DOCKER_UID` / `GID` | invoking identity | Non-root bind-read identity |

The initial 2 GiB/2 CPU/256 PID defaults leave substantial headroom over the approximately
120 MiB platform Codex runtime plus Python/MCP process tree without granting unbounded host
resources. The 512 MiB tmpfs counts against the memory limit. Tune deployment values for measured
workloads and rerun both deterministic container tests and the live Codex check.

The tmpfs is `nosuid,nodev,noexec`. Normal Python, MCP, and Codex executables run from the
read-only image, not temporary storage. If a future pinned runtime proves it needs temporary
execution, treat changing `noexec` as a security decision with a regression and ADR review.

## MCP clients

Official Codex documentation supports stdio MCP servers as `command`, `args`, and explicit
environment forwarding. Add the following to the applicable trusted Codex `config.toml`:

```toml
[mcp_servers.maestro]
command = "/absolute/path/to/maestro/.venv/bin/python"
args = ["/absolute/path/to/maestro/scripts/maestro_container.py"]
env_vars = ["MAESTRO_ALLOWED_ROOTS", "MAESTRO_CODEX_AUTH_FILE", "MAESTRO_DOCKER_IMAGE"]
enabled_tools = ["resolve_codebase_fact"]
required = true
startup_timeout_sec = 30
tool_timeout_sec = 360
default_tools_approval_mode = "auto"
```

Run `codex mcp list` or `/mcp` to verify the connection. Codex CLI, the IDE extension, and the
ChatGPT desktop Codex host share this configuration. See
<https://developers.openai.com/codex/mcp/>.

Claude Code supports the equivalent local stdio command. An explicit user/local configuration
can be added with `claude mcp add-json`, or represented as:

```json
{
  "mcpServers": {
    "maestro": {
      "type": "stdio",
      "command": "/absolute/path/to/maestro/.venv/bin/python",
      "args": ["/absolute/path/to/maestro/scripts/maestro_container.py"],
      "env": {
        "MAESTRO_ALLOWED_ROOTS": "${MAESTRO_ALLOWED_ROOTS}",
        "MAESTRO_CODEX_AUTH_FILE": "${MAESTRO_CODEX_AUTH_FILE}",
        "MAESTRO_DOCKER_IMAGE": "${MAESTRO_DOCKER_IMAGE:-maestro-verifier:local}"
      }
    }
  }
}
```

Use absolute paths and run `claude mcp get maestro` or `/mcp` to verify it. Project-scoped
`.mcp.json` is repository-controlled and Claude Code asks for approval; prefer a user/local entry
when the repository itself is untrusted. See <https://code.claude.com/docs/en/mcp>.

Both clients launch the local Docker CLI and communicate with the container solely over stdio.
No port or remote MCP transport is involved.

## Networking and credentials

The container uses Docker's bridge network because the Codex SDK must reach the model provider.
It does not use host networking or publish ports. The application disables worker web search,
apps, external MCPs, and related integrations, but the container network cannot technically
distinguish provider traffic from other egress by a compromised process. Provider allowlisting or
a controlled proxy is a future Level 3 design.

Never pass credentials during `docker build`, place them in Dockerfile `ARG`/`ENV`/labels, or add
them to the build context. `.dockerignore` is a strict allowlist containing only the files required
for the package build.

## Security and quality gates

Run the existing native gate first. Then run:

```bash
docker build --check .
docker build --tag maestro-verifier:local .

MAESTRO_CONTAINER_IMAGE=maestro-verifier:local \
MAESTRO_CONTAINER_SKIP_BUILD=1 \
uv run pytest -m "container and not e2e" tests/container

trivy image --config trivy.yaml --ignore-unfixed=false --exit-code 0 maestro-verifier:local
trivy image --config trivy.yaml maestro-verifier:local
```

The container tests execute the built image and verify effective UID/capabilities,
`NoNewPrivs`, seccomp, root/tmpfs/repository writes, production-only dependencies, inspectable
mount/network/resource policy, MCP discovery/call/authorization, and attached-process shutdown.

`trivy.yaml` is the authoritative blocking policy. The first pass reports all HIGH/CRITICAL
findings without failing for vulnerability presence; scanner errors still fail. The second pass
scans OS and Python packages, filesystem secrets, and image-configuration secrets. Fixable HIGH
or CRITICAL vulnerabilities and HIGH/CRITICAL secret findings fail. Do not add broad ignore rules.

Run the real Inspector through the launcher as documented in `CONTRIBUTING.md`. The required
checks are `tools/list`, a credential-free normative call, invalid input, and an existing path
outside the allowed roots. Every response must remain machine-parseable and logs must stay off
stdout.

With explicit provider authorization, run the separate live security probe:

```bash
MAESTRO_CONTAINER_IMAGE=maestro-verifier:local \
MAESTRO_CONTAINER_SKIP_BUILD=1 \
uv run pytest -m "container and e2e" tests/container/test_codex_container_e2e.py
```

The probe mounts only a synthetic fixture and asks a dedicated test-only Codex worker to attempt a
controlled write under the same image, UID, read-only mount, and resource profile. It never asks
the production `resolve_codebase_fact` Capability to violate its inspection-only policy. Model
behavior is nondeterministic, so the deterministic write tests remain authoritative. If no
credential is authorized, keep the probe present and report it as unexecuted.

## Observable and residual controls

The launcher does not override Docker's default seccomp or available AppArmor/SELinux policy.
Tests assert seccomp filter mode and reject explicit unconfined settings. `docker info` identifies
daemon security options; a macOS host does not itself gain a Linux LSM merely because the VM
reports one.

Shutdown tests verify the attached Docker process and named container disappear, which covers the
container PID namespace and Docker init/reaping behavior. They do not prove the absence of
unrelated VM processes. Trivy checks the final filesystem, configuration, and reachable image
metadata; it does not make claims about unrelated local BuildKit caches.
