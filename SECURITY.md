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

Maestro canonicalizes allowed roots, rejects traversal/symlink escape, performs bounded
no-follow file discovery, isolates the Codex home and environment, disables inherited
MCPs/skills/apps/web/subagents, selects deny-all/read-only at both SDK boundaries, validates
structured output and evidence, sanitizes results, fingerprints the repository before and
after investigation, bounds admission/deadlines/pipes/results, and owns worker cancellation.
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
- Secret scanning and output redaction are heuristic; neither proves that a secret cannot be
  selected by the model or encoded in an unexpected form.
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
