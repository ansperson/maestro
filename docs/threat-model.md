# Maestro v1 threat model

## Scope and assumptions

This model covers the local stdio `resolve_codebase_fact` capability and its PostgreSQL Audit
tracer at server version 1.0.0. It excludes remote transport, Jobs, non-Audit durable state,
external MCP/integrations, PR/Issue work,
multi-tenancy, and subagents. The operator and host kernel are trusted; the MCP caller,
repository, model/runtime output, and dependency supply chain are not. The model provider is
trusted to receive selected authorized-repository data under the operator's provider terms.
Native mode trusts the host user boundary. Hardened mode additionally trusts the Docker daemon,
Linux kernel/VM, official pinned base manifests, and configured host filesystem sharing.

## Threats, controls, and residual risk

| Threat | Implemented controls | Residual risk / deployment assumption |
|---|---|---|
| Malicious caller input | Strict bounded Pydantic models, forbidden extras, control-character rejection, neutralization, safe SDK validation-error remapping | Semantic prompt attacks may survive deterministic neutralization; policy/model handling remains defense-in-depth |
| Repository prompt injection | Repository/caller text delimited as untrusted, fixed versioned developer policy, no project instructions/skills/MCPs/apps/subagents | A model can still disobey; deterministic evidence/output checks limit acceptance but cannot prove reasoning integrity |
| Repository-controlled execution | Policy forbids tests/builds/scripts; fingerprint discovery uses the current interpreter in isolated mode with a package-owned module, a working directory outside the authorized repository, structured stdin, no repository import path, closed descriptors, and no shell; Git is fixed-argument and hardened; hardened mode provides no-exec temporary state, read-only image/repository mounts, non-root execution, dropped capabilities, and no privilege escalation | Isolated Python module discovery is not an OS sandbox. The image retains the minimal base utilities needed by Python/Codex; the fingerprint helper has no OS network sandbox; policy remains necessary and a compromised dependency may execute available binaries |
| Path traversal/symlink escape | Canonical non-anchor allowed roots, explicit filesystem-anchor and `..` rejection, `relative_to` containment, no-follow bounded descriptor reads, canonical evidence recheck | Concurrent directory namespace replacement cannot be fully excluded without OS mount/namespace isolation |
| Host filesystem confidentiality | Authorized cwd preserved, minimal environment/home, caches/vendor skipped, no absolute paths in output; hardened mode mounts only configured roots and one optional auth file | Every configured root is readable by the shared container; the Docker daemon and host kernel/VM remain trusted |
| Model-provider data egress | Only authorized roots are mounted, no web/apps, explicit credentials, bridge network without host mode or ports | Provider control-plane traffic is necessary; Level 2 cannot distinguish provider traffic from arbitrary compromised-process egress |
| Malformed/malicious model output | One turn, output schema, strict Pydantic envelope/result, semantic invariants, evidence and total-size bounds; Audit detectors collect original-input credential/secret/root/path/control spans, merge overlaps deterministically, and apply bounded redactions once | Novel path syntax, secret encodings, deliberate obfuscation, or misleading concise findings may evade heuristic redaction |
| Credential leakage | Explicit single auth source, no inherited full environment, mode-0700/0600 temporary files, shell env allowlist, log/output redaction; hardened mode mounts only the dedicated file read-only | Docker inspectors can see environment API keys; same-user/daemon process inspection, compromised runtime/provider, or cleanup failure can expose secrets |
| Recursion | No worker MCP configuration, depth marker propagated to shell, server startup/context recursion guards | A compromised runtime could bypass process policy; external process controls remain stronger |
| Denial of service | Input/file/byte/output bounds, timeout, bounded concurrency and queue, bounded stdout/stderr, and process-group terminate/bounded-wait/kill/reap for fingerprint/Git/AI children after handle acquisition; hardened launcher adds memory, CPU, PID, and tmpfs limits plus init | Supported asynchronous process creation is awaited directly: before a child handle is returned, normal runtime cancellation applies, but the application cannot independently enforce a hard deadline or own reaping. Provider/tool latency and host-wide Docker resource contention remain; defaults need operational tuning for unusually large workloads |
| Audit persistence failure | Mandatory typed configuration, lazy short transactions, fail-closed start/completion, three attempts in a five-second budget only for failures known not committed, safe public errors/log metadata | Ambiguous writes fail conservatively and can leave a start-only or complete-but-unacknowledged trail; duplicate verification and recovery are deferred |
| Repository mutation | SDK read-only/deny-all, evidence file identity checks, before/after content/Git fingerprint; hardened mode bind-mounts roots recursively read-only | Native mode retains the upstream bypass risk; Docker daemon/kernel compromise remains outside Level 2 |
| Compromised dependency/runtime | Exact MCP/Codex/runtime pins, lockfile, startup version check, digest-pinned image inputs, pip-audit, Trivy, Dependabot, CodeQL, pinned actions | Locking/scanning is not compromise prevention; the Docker daemon, host kernel/VM, registry, and scanner databases remain trusted dependencies |
| Container breakout or daemon exposure | Non-root UID, all capabilities dropped, no-new-privileges, default seccomp/LSM, no privileged/host namespaces/devices/socket, read-only root | Kernel/runtime vulnerabilities and access to the host Docker daemon are outside the container boundary; rootless Docker further reduces daemon impact on native Linux |
| Future external MCP trust | No external MCP server is configured or reachable in v1 | Any future integration needs a new ADR, authentication/authorization, data-flow review, and threat-model extension |

## Data lifecycle

Requests arrive over the local MCP stream. Only bounded question/context and the authorized
path enter application memory. A package-owned fingerprint helper gets only the canonical root
and numeric bounds over stdin, starts from a trusted directory outside the authorized repository,
and returns a size-bounded versioned result that the parent treats as untrusted. The helper does
not provide an OS network sandbox. The AI worker gets a non-secret request, explicit credentials via
its isolated environment/home, and reads the repository during one ephemeral Codex thread and
turn. The provider may receive selected repository content. The parent accepts only a bounded
private JSON envelope, validates and redacts it, and returns structured MCP content. Temporary
Codex state is recursively removed best-effort. PostgreSQL retains bounded, sanitized semantic
Audit records; it does not retain caller context, prompts, transcripts, repository content, raw
model output, or credentials. No cache, telemetry, or external log sink exists.

In hardened mode the MCP client starts a local Docker CLI process over stdio. The launcher
canonicalizes configured roots, builds an argument vector without a shell, and mounts those roots
at the same paths read-only. Temporary server/worker state lives on a bounded tmpfs and disappears
with the container. The provider remains reachable over the container bridge network.

## Security invariants to revalidate on upgrades

- The official SDK still honors `ApprovalMode.deny_all`, `Sandbox.read_only`, ephemeral
  threads, turn interruption, structured output, isolated config, and disabled features.
- MCP tool schemas/annotations and safe error mapping retain their locked digest.
- The upstream read-only write-bypass report is resolved and regression-tested before any
  stronger write-prevention claim.
- Model/runtime/policy changes pass deterministic tests, real E2E, and the versioned eval
  corpus before release.
- Dockerfile checks, built-image security assertions, Trivy policy, stdio Inspector behavior,
  base digests, default seccomp/LSM availability, and resource-limit support remain effective.
