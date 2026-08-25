# ADR-0003: Hardened Local Container Execution

Date: 2026-08-25

Status: Accepted

## Context

Maestro v1 exposes one local stdio MCP Capability, `resolve_codebase_fact`. Its application
controls authorize repository roots, isolate the Codex worker, request a deny-all/read-only
Codex sandbox, and reject results when repository or evidence fingerprints change.

The current Codex SDK/runtime does not provide a dependable write-prevention boundary. In
particular, a managed edit may persist despite the requested read-only sandbox. Fingerprint
validation can detect a mutation but cannot prevent or undo it. Deployments that need stronger
write prevention therefore require an operating-system boundary outside Maestro.

Containerization changes the durable deployment and security model, but it does not justify a
new application runtime, Capability, transport, or domain dependency.

## Decision

Maestro will provide a Level 2 hardened local-container deployment mode in addition to its
existing native development mode.

Docker remains an outer adapter:

```text
Docker
└── Maestro
    └── EngineeringVerifier
        └── AgentRuntime
            └── CodexRuntime
```

No Docker type or behavior enters `src/maestro`. The public MCP tool name, schemas, semantic
statuses, annotations, stdio transport, and native entry point remain unchanged.

### Supported platform boundary

The image runs Linux containers. The tested platform set is:

- native Linux/amd64 in CI;
- Linux/arm64 through Colima on macOS for local validation.

Docker Desktop on macOS is documented as a supported operating model, but host filesystem
sharing and Linux security-module observability depend on its VM and daemon configuration.
Windows containers, Kubernetes, remote MCP transport, and multi-platform image publishing are
outside this decision.

The launcher must fail closed when an allowed root cannot be represented safely. It accepts
spaces through argument arrays, but a container engine or VM may still reject particular host
mount paths. Such a platform limitation is reported rather than hidden by unsafe shell
interpolation or an application-level path translation.

### Image and supply chain

The production image is multi-stage and uses immutable, readable Python and uv image references.
It installs the `uv.lock` production graph non-editably and copies only the resulting virtual
environment into the runtime stage. The final stage contains no build tools added by Maestro and
runs as a non-root numeric user by default. The only added OS investigation package is a
version-pinned Trixie Git client because Maestro's existing HEAD/dirty-state fingerprint invokes
Git. Ripgrep and broader developer tooling are not required for correctness and are not installed.
Git and its transitive packages resolve from the immutable Debian and Debian-security snapshots
recorded by the pinned official Python base. The unused system Python package installer is removed
from the final stage.

“Reproducible” means that the selected base manifests, uv tool, and Python dependency graph are
immutable and locked. It does not claim byte-for-byte identical OCI output across builders.
Dependabot tracks Docker references.

Trivy is the single dedicated image scanner. It scans OS and Python packages, image filesystem
secrets, and image-configuration secrets. The gate fails for fixable HIGH or CRITICAL
vulnerabilities and for detected HIGH or CRITICAL secrets. Unfixed vulnerabilities remain
visible in a preceding non-blocking report but do not fail this initial gate; scanner failures
still fail both passes. The blocking policy uses Trivy 0.74's generated nested configuration
schema. Any future ignore must be narrow, reviewed, and justified.

### Runtime isolation profile

The authoritative launcher applies:

- a read-only root filesystem;
- read-only, recursively read-only, private-propagation bind mounts for configured allowed
  repository roots;
- one bounded, ephemeral `/tmp` tmpfs with `nosuid`, `nodev`, and `noexec`;
- a non-root UID/GID, normally mapped from the invoking user for reliable host reads;
- all Linux capabilities dropped;
- `no-new-privileges`;
- Docker's default seccomp and any available default AppArmor/SELinux confinement;
- a private/default bridge network with no published ports;
- no host namespaces, devices, privileged mode, or container-engine socket;
- explicit memory, CPU, and PID limits; and
- Docker's init process for signal forwarding and child reaping.

The tool caller never chooses mounts. Only canonical roots from `MAESTRO_ALLOWED_ROOTS` are
mounted, preferably at the same absolute paths used by the public `repository_path` argument.
Mount grammar that cannot be represented safely is rejected.

The image defaults to UID/GID 65532. The launcher uses the invoking non-root UID/GID so native
Linux and macOS VM mounts remain readable without making them writable. An operator running the
launcher as root must explicitly choose a non-root deployment UID/GID.

### Credentials and writable state

The preferred Codex authentication mechanism is one explicitly configured regular,
non-symlink credential file mounted read-only at a fixed container path. The user's home,
`.codex`, SSH, cloud, and general configuration directories are never mounted. Maestro keeps
copying the minimum authentication material into each worker's isolated ephemeral Codex home.

API-key environment forwarding remains compatible with native behavior, but Docker records
container environment values in inspectable daemon metadata. The file-based mechanism is the
recommended hardened mode.

No build credential is required or accepted. Normal runtime state is confined to the temporary
tmpfs and disappears with the container.

### Networking

The container retains outbound networking because the Codex SDK must reach the configured model
provider. Host networking and published ports are forbidden; MCP remains stdio. The application
continues to disable agent web/search integrations, but the container network cannot distinguish
provider traffic from arbitrary egress by a compromised process. Controlled egress is a future
Level 3 boundary and is not part of v1.

### Validation and claim boundaries

Deterministic tests exercise the built image and launcher, not only Dockerfile text. They verify
the effective UID, capability mask, `NoNewPrivs`, seccomp status, root and repository mount
write failures, ephemeral writable state, resource settings, network/port/socket absence, MCP
stdio behavior, authorization, and shutdown.

AppArmor/SELinux is required not to be intentionally disabled when the daemon provides it. Tests
report daemon support but do not claim a macOS host itself runs a Linux LSM. Process-lifecycle
tests prove cleanup of the named container and its PID namespace; they do not prove the absence
of unrelated processes inside a Docker Desktop or Colima VM. Image scans cover final filesystem,
configuration, and reachable image metadata; they do not make claims about unrelated local
BuildKit caches.

A separate opt-in security probe may ask a real Codex SDK worker to attempt a controlled write
to a synthetic read-only mount under the same image, user, and isolation profile. It is test-only
and does not route through or weaken the production inspection-only Capability. Model behavior is
nondeterministic, so deterministic host-side mutation attempts remain the authoritative
write-prevention gate.

## Consequences

### Positive

- Repository write prevention is enforced by the Linux mount boundary before application-level
  mutation detection.
- Native mode remains available for development and troubleshooting.
- The application architecture and public MCP contract remain unchanged.
- The recommended local deployment has explicit least-privilege and resource controls.
- Security claims are tied to observable container/daemon state and documented residual risks.

### Negative

- Operators need a compatible Docker/BuildKit environment and shared host paths.
- Provider access means container-level arbitrary egress is not technically prevented.
- The shared Maestro container still has read access to every configured allowed root; per-worker
  containers are outside v1.
- File permissions and LSM reporting differ across native Linux, Docker Desktop, and Colima.
- Trivy database updates require network access and can surface newly published findings without
  a source change.

## Rejected alternatives

- A `DockerRuntime` inside `EngineeringVerifier`: leaks deployment concerns into the application.
- Docker Compose: adds no required behavior for one local stdio process.
- Custom seccomp/AppArmor policies: premature maintenance cost without a demonstrated gap.
- `--network=none`: breaks required model-provider access.
- Mounting a complete user home or `.codex`: exposes unnecessary credentials and configuration.
- Asking `resolve_codebase_fact` to perform a write for testing: contradicts its public,
  inspection-only contract.
- Multiple overlapping image scanners: increases policy drift without improving the defined gate.
- Bookworm oldstable as the Python/Git runtime base: its otherwise equivalent local image reported
  materially more unfixed HIGH/CRITICAL findings than current stable Trixie.
