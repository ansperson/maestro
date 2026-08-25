# Implementation discovery — 2026-08-25

This note records the dependency and runtime discovery completed before Maestro v1
implementation. The architectural sources are `README.md`, ADR-0001, and ADR-0002. The
adjacent skills repository, including `grill`, was inspected only as consumer context.

## Selected stable interfaces

- MCP specification: `2026-07-28`.
- Official MCP Python SDK: `mcp==2.1.0` (stable v2 line).
- Official Codex Python SDK: `openai-codex==0.147.0`.
- Bundled Codex runtime: `openai-codex-cli-bin==0.147.0`, pinned by the SDK.
- Python: `>=3.13`.
- Initial verifier model: `gpt-5.4`, an explicit model identifier used by the current
  official Python SDK documentation and configurable through `MAESTRO_CODEX_MODEL`.
- Integration: the asynchronous `AsyncCodex` API, a fresh ephemeral thread, structured
  output through `output_schema`, `ApprovalMode.deny_all`, and `Sandbox.read_only` at both
  thread and turn boundaries. Maestro does not locate a `codex` executable on `PATH`.

Primary references:

- <https://modelcontextprotocol.io/specification/2026-07-28>
- <https://py.sdk.modelcontextprotocol.io/>
- <https://github.com/openai/codex/tree/main/sdk/python>
- <https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md>
- <https://developers.openai.com/codex/config-reference/>

## Meaningful API differences and decisions

The stable MCP v2 high-level server is `mcp.server.MCPServer`; the v1
`mcp.server.fastmcp.FastMCP` import is obsolete. MCP roots, sampling, and protocol logging
were deprecated by the 2026-07-28 specification, so Maestro uses none of them. MCP Tasks
are also outside v1. `MCPServer` derives input and output schemas from type annotations and
Pydantic models, emits structured content, and can be tested through the official in-memory
`Client`.

The high-level SDK's default function-argument error renders Pydantic's validation message,
including the rejected input value. That conflicts with Maestro's non-reflection requirement
for secret-like caller data. Maestro keeps the high-level API but narrows its public
`call_tool` seam to map argument-validation failures to the stable `INVALID_INPUT` payload;
all other SDK tool and protocol behavior remains unchanged.

The Codex SDK now exposes asynchronous thread and turn lifecycle APIs directly. A turn
handle supports interruption, and a turn accepts a JSON output schema. `Sandbox.read_only`
and `ApprovalMode.deny_all` are public presets. The SDK installs and resolves its own exact
runtime package, so no arbitrary executable lookup is needed.

`CodexConfig.env` is an overlay on `os.environ`, not a replacement. Maestro therefore runs
the SDK in a dedicated child worker whose parent environment is explicitly allowlisted.
The worker uses a temporary `CODEX_HOME`/`HOME` with mode `0700`, no inherited MCP server,
plugin, skill, hook, or project configuration, and guaranteed best-effort cleanup. Only an
explicit authentication source is copied or forwarded.

## Supported controls

- One fresh ephemeral Codex thread and exactly one turn per Capability invocation.
- Structured model output plus strict Pydantic validation.
- Explicit read-only sandbox selection on the thread and turn.
- Denial of permission escalation.
- Application-owned timeout, cancellation, child-process termination, concurrency, and
  admission bounds.
- Repository fingerprint discovery in an isolated package-owned Python helper with strict
  versioned stdin/stdout contracts, bounded output, minimal environment, closed descriptors,
  and terminate/kill/reap cleanup shared by every Git inspection subprocess.
- Isolated Codex configuration/home and an allowlisted process environment.
- Web search, apps/connectors, multi-agent tools, goals, hooks, skill dependency installs,
  and project instruction loading disabled through isolated configuration.
- No configured MCP servers and no Maestro endpoint in the worker configuration.
- Post-investigation repository and evidence fingerprints before accepting a result.

## Unsupported controls and residual risk

The public SDK does not provide a repository-read-only tool allowlist, a maximum tool-action
count, or a complete repository-only confidentiality boundary. Codex's shell can technically
execute a repository-controlled program even though Maestro policy forbids it. High-assurance
deployment therefore requires an external OS/container sandbox with a read-only repository
mount, a minimal/no-exec tool image, restricted process and network namespaces, and resource
limits.

More seriously, an open upstream report against SDK/runtime 0.147.0 states that a managed
file-edit operation can persist a write despite `Sandbox.read_only`:
<https://github.com/openai/codex/issues/40229>. Maestro applies the strongest supported SDK
settings and rejects results if the repository fingerprint changes, but that detection
cannot prevent or undo such a write. Maestro does not claim technical write prevention until
the upstream issue is fixed and verified or the runtime is enclosed by a read-only OS mount.

Sandboxed tool network access and web search are disabled, but model-provider control-plane
communication necessarily remains enabled. Selected repository content may leave the host
for the configured model provider. The current SDK does not supply a hard maximum agent tool
count; Maestro bounds one turn by wall-clock time and output size instead.

These are implementation-control limitations, not changes to ADR-0001 or ADR-0002, so no new
architectural decision record is required.

## Container boundary discovery — 2026-08-25

ADR-0003 adds an external deployment boundary for the read-only limitation above. Discovery was
performed against current official documentation and the local Docker context before the image
or launcher was implemented.

### Tested toolchain and daemon

- Docker client `28.1.1`, API `1.49`, on macOS/arm64.
- Docker Engine `29.2.0`, API `1.53`, Linux/arm64 through Colima.
- Buildx `0.29.1` and Dockerfile frontend `1.19.0`.
- Linux kernel `6.8`, cgroup v2, `runc 1.3.4`, and `docker-init 0.19.0`.
- The daemon reports memory, swap, CPU quota/shares, and PID limits.
- Daemon security options report builtin seccomp, AppArmor, and a private cgroup namespace.
- The local Colima daemon is not rootless. Rootless Docker remains a native-Linux hardening
  recommendation rather than a cross-platform requirement.

### Meaningful API and control findings

- `docker build --check` reports official Dockerfile build checks. Dockerfile
  `# check=error=true` makes findings fatal. Pinning the frontend version avoids an unreviewed
  future check becoming a surprise build failure.
- `--mount type=bind,...,readonly` prevents writes and fails when a source path does not exist;
  the older `-v` syntax may silently create a missing directory. `bind-recursive=readonly`
  requires Linux kernel 5.12 or newer and fails closed on an unsupported host. Current Docker
  also requires it to be paired explicitly with `bind-propagation=rprivate`; the launcher does
  so to prevent mount events from propagating to or from the host.
- `--read-only` makes the container root filesystem read-only except for explicit mounts.
- `--tmpfs` supports `nosuid`, `nodev`, `noexec`, size, mode, UID, and GID options. The tmpfs
  counts against the container memory limit.
- `--security-opt=no-new-privileges=true`, `--cap-drop=ALL`, and `--init` are supported by
  `docker run`. `NoNewPrivs`, effective capabilities, and seccomp mode are observable in
  `/proc/self/status`.
- Docker's builtin seccomp allowlist is applied unless explicitly overridden. AppArmor/SELinux
  availability remains daemon/platform-dependent, so validation checks that confinement was not
  intentionally disabled instead of claiming universal LSM enforcement.
- Docker applies no resource limits by default; memory, CPU, and PID limits must be explicit.
- Colima and Docker Desktop run the Linux daemon in a VM. Host filesystem sharing rules can reject
  paths even when subprocess argument quoting is correct, so the launcher validates what it can
  and reports engine failures without falling back to unsafe interpolation.
- Trivy image scans enable filesystem vulnerability and secret scanning by default, but image
  configuration secret scanning is separate. Maestro enables both explicitly and applies one
  documented failure policy.
- Trivy 0.74's generated configuration schema nests scanner selection under `scan`, image
  configuration scanners under `image`, package types under `pkg`, and unfixed-vulnerability
  handling under `vulnerability`. Legacy top-level spellings are ignored rather than rejected,
  so `trivy.yaml` uses only the generated v0.74 hierarchy. CI runs a reporting pass with
  `--ignore-unfixed=false --exit-code 0` before the blocking fixable-finding pass.
- The 2026-08-25 final-image audit found no embedded secrets and no fixable HIGH/CRITICAL
  vulnerability. Its non-blocking all-findings pass reported 34 HIGH and 12 CRITICAL findings:
  31 `affected` and 15 `fix_deferred`. Removing the runtime's unused system `pip` also removed its
  vendored, fixable `msgpack` and `setuptools` findings instead of suppressing them. Counts are a
  dated scanner-database snapshot and can change independently of source.
- The base image has `grep` and `find` but no Git or ripgrep. Git is required by Maestro's
  existing HEAD/dirty-state fingerprint, so the runtime adds only the current pinned Trixie Git
  package. Ripgrep is useful but not required for correctness and would widen the executable and
  package surface; it remains absent.
- The current official `python:3.13.15-slim-trixie` index digest is
  `sha256:7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4`.
  Trixie is the current stable Debian slim base; the tested Bookworm oldstable image with Git
  reported 56 HIGH and 14 CRITICAL findings, so it was rejected.
- The pinned official Trixie image records Debian and Debian-security snapshot timestamp
  `20260824T000000Z`. The runtime resolves pinned Git and its transitive packages from those same
  snapshot indexes so APT does not introduce a moving package graph.
- Codex and Claude Code both support local stdio MCP servers as a command, argument array, and
  explicit environment. The container launcher preserves stdio and requires no remote transport.
- The current official MCP Inspector package validated on 2026-08-25 is 2.2.0 and requires Node
  22.19 or newer. Its mode flag must precede CLI arguments. The tested `--cli` contract still
  supports child `-e` environment values, `--tool-args-json`, and JSON output; tool `isError`
  responses produce two JSON lines and exit status 5.

Primary references:

- <https://docs.docker.com/reference/build-checks/>
- <https://docs.docker.com/engine/storage/bind-mounts/>
- <https://docs.docker.com/engine/storage/tmpfs/>
- <https://docs.docker.com/reference/cli/docker/container/run/>
- <https://docs.docker.com/engine/security/seccomp/>
- <https://docs.docker.com/engine/security/apparmor/>
- <https://docs.docker.com/engine/security/rootless/>
- <https://docs.docker.com/engine/containers/resource_constraints/>
- <https://docs.astral.sh/uv/guides/integration/docker/>
- <https://trivy.dev/docs/latest/guide/target/container_image/>
- <https://trivy.dev/docs/latest/guide/scanner/secret/>
- <https://github.com/abiosoft/colima/blob/main/docs/FAQ.md>
- <https://developers.openai.com/codex/mcp/>
- <https://code.claude.com/docs/en/mcp>
- <https://www.npmjs.com/package/@modelcontextprotocol/inspector>
