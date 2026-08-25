# Security policy

## Supported version

Maestro v1 (`1.x`) is the only supported implementation line. It is a local stdio process,
not a remotely exposed service. Report security issues privately to the repository owner;
do not include live credentials, private repository content, prompts, or model transcripts in
a public issue. Include a minimal synthetic reproduction, affected version, operating system,
and the control that failed.

## Trust boundaries

The local operator controls the server process, allowed roots, model, and explicit Codex
authentication source. An MCP caller, every repository byte (including ADRs, tests, Git
history, and instructions), the Codex runtime/model, and model output are untrusted. The model
provider is an explicit data-egress recipient. A stdio client gets the privileges of the user
who launches Maestro; v1 has no remote authentication, authorization, or multi-tenancy.

Maestro canonicalizes allowed roots, rejects filesystem anchors plus traversal/symlink escape,
performs bounded no-follow file discovery, isolates the Codex home and environment, disables
inherited MCPs/skills/apps/web/subagents, selects deny-all/read-only at both SDK boundaries,
validates structured output and evidence, sanitizes results, fingerprints the repository before
and after investigation through an isolated package-owned helper with a versioned bounded protocol,
bounds admission/deadlines/pipes/results, owns helper/Git/worker termination and reaping after a
child-process handle is acquired, and requires durable Audit start/completion records before AI
work/result release. Audit connections
are lazy and short-lived; no database connection or transaction remains open during AI work.
Logs exclude request text, source, credentials, transcripts, model responses, and absolute
paths. The recommended Level 2 deployment additionally encloses the unchanged application in a
hardened Linux container with read-only repository mounts and root filesystem, ephemeral
temporary state, a non-root identity, no capabilities or privilege escalation, default seccomp,
no host namespaces/ports/socket, and explicit resource limits. See ADR-0003 and
`docs/container.md`.

## Credentials and privacy

Configure exactly one of `MAESTRO_CODEX_AUTH_FILE` or `MAESTRO_CODEX_API_KEY`. The former is
copied through a no-follow regular-file descriptor into a mode-0700 temporary Codex home; the
latter exists only in the worker process and is omitted from its shell environment. Temporary
state is removed on success, failure, timeout, and cancellation on a best-effort basis.

In hardened container mode, mount only the dedicated authentication file read-only. Do not mount
a complete home or `.codex` directory. File authentication is preferred because an API key passed
as a Docker environment variable is visible in daemon container metadata to principals allowed
to inspect Docker.

The cloud model control plane remains reachable and selected repository content may leave the
host. Do not authorize a repository whose contents may not be sent to that provider. Maestro
does not persist prompts, transcripts, repository content, or model output.

## Residual risks

- Codex SDK/runtime 0.147.0 has an open report that a managed edit can persist despite
  `read_only`. Native fingerprint rejection detects but cannot prevent or undo mutation. The
  hardened container's read-only repository mount prevents writes through that path.
- The SDK exposes no repository-only confidentiality boundary, shell-command allowlist, or
  maximum tool-action count. Policy forbids repository execution but is not enforcement.
- Sandbox tool/web networking is disabled where supported, but provider traffic is required;
  OS-specific sandbox behavior must be revalidated after runtime upgrades.
- Filesystem namespace races, a compromised Python/Codex/MCP dependency, kernel compromise,
  process inspection by the same user, and failed best-effort temporary cleanup remain outside
  the application boundary.
- The fingerprint helper receives only a canonical root and numeric limits over stdin, runs with
  isolated Python module discovery from a trusted directory outside the authorized repository,
  closed inherited descriptors, and a minimal environment, and never intentionally opens a network
  connection. It is not an OS network sandbox; a compromised interpreter or imported dependency
  remains covered by the broader process-compromise risk.
- Python's supported asynchronous subprocess API does not expose a child handle until process
  creation finishes. Maestro awaits that trusted operation directly and uses the runtime's normal
  cancellation behavior before handle acquisition; therefore it cannot claim an independent hard
  application deadline or application-owned reaping during that narrow pre-handle interval. Once a
  handle exists, cancellation and deadlines terminate, bounded-wait, kill if needed, and reap the
  helper or Git process before returning.
- Secret scanning and output redaction are heuristic. Audit redaction detects configured roots,
  selected private/drive/UNC paths, credential-bearing URI user information, common secret forms,
  and unsafe controls by collecting spans from the original input and applying bounded replacements
  once. Unrecognized path syntax, secret formats, encodings, or deliberate obfuscation may survive;
  neither control proves that a secret cannot be selected or encoded by the model.
- Audit retries are limited to failures known not committed. An unverifiable or ambiguous write
  returns `AUDIT_PERSISTENCE_ERROR` and may leave a start-only or complete-but-unacknowledged
  trail; duplicate verification and recovery remain outside this release boundary. Adapter
  details, SQL, SQLSTATE, hosts, users, and credentials are excluded from public errors and logs.
- Operational failures after a durable start persist only their safe error code, bounded lifecycle
  stage, and approved version metadata. Cancellation failure persistence has a separate one-second
  budget and is joined before the original cancellation propagates. Host/process loss or an
  unestablished terminal write can still leave a start-only Trail; startup deliberately does not
  manufacture a terminal outcome.
- The Level 2 container permits provider networking and cannot technically distinguish it from
  arbitrary egress by a compromised process. Every configured allowed root is readable by the
  shared container. Higher assurance requires controlled egress and/or per-worker containers.
- Docker Desktop and Colima enforce Linux controls inside a VM. Host path sharing, rootless mode,
  and AppArmor/SELinux availability differ by daemon. Validation reports the observable daemon
  state and does not claim universal host LSM coverage.

Use the hardened local-container mode for Level 2 repository write prevention. Deployments that
require provider-only egress, stronger confidentiality between allowed roots, or isolation from
the Docker daemon/host kernel need a separate Level 3 design. See `docs/threat-model.md` for the
full analysis.

## Dependency and baseline review

Dependencies and runtime packages are locked. Startup rejects missing/mismatched pinned MCP
and Codex packages. Container bases and uv are digest-pinned, and Trivy checks fixable
HIGH/CRITICAL image vulnerabilities plus filesystem/configuration secrets. CI runs pip-audit,
CodeQL, and the container gate; Dependabot maintains Python, action, and Docker pins.
Before accepting a detect-secrets baseline change, run the documented pre-commit hook,
inspect every added finding, remove real secrets and rotate them, and mark only
synthetic/documentation fixtures as false positives. The reviewed v1 entries are limited to
API-key presence/propagation tests and secret-redaction test strings; they are not usable
credentials. Never baseline a live credential.
