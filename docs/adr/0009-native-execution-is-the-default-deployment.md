# ADR-0009: Native Execution Is the Default Deployment

Date: 2026-08-30

Status: Accepted

Amends: ADR-0003, ADR-0005

## Context

ADR-0003 established a hardened local container as the recommended deployment, and ADR-0005
extended it with a second PostgreSQL container. Both remain implemented, tested, and correct.

Provider authentication is what makes them unusable today. The Codex runtime has no available
credit, and the intended replacement is a Claude adapter that invokes the locally installed
`claude` binary so the user's existing subscription supplies authentication and no credential
enters Maestro.

That inheritance does not cross the container boundary on the primary development platform. On
macOS the binary stores its credential in the operating-system Keychain, not in a file, so there
is nothing to mount or copy into a Linux container. Inside a container the same binary
authenticates strictly through `ANTHROPIC_API_KEY`, which is billed per use and defeats the
reason for choosing the binary. On Linux the credential is a file and the existing bind-mount
delivery would work unchanged.

The alternatives all cost more than they return right now: a metered API key contradicts the
subscription model; extracting the Keychain credential and injecting it makes Maestro handle a
rotating secret it deliberately never sees; running the worker on the host while Maestro runs in
a container removes the mount boundary that is ADR-0003's entire purpose.

## Decision

Native execution is the supported default for running and developing Maestro.

The container deployment is placed on hold. It is neither removed nor deprecated: the images,
Compose adapter, launchers, and their tests are retained, and the container gates stay mandatory
in CI so the security boundary keeps being verified against every change.

The hold is lifted when provider authentication is resolved for a containerized worker — by a
Linux deployment where the credential is a file, or by a decision that accepts one of the
alternatives above.

Documentation presents native execution as the normal path and the container deployment as the
hardened option whose worker authentication is unresolved.

## Consequences

### Positive

- Maestro is usable today with the authentication the user already has, at no additional cost.
- The container security boundary stays under test rather than decaying while it is unused.
- No credential handling is added to Maestro to work around a platform limitation.

### Negative

- The default deployment loses the mount boundary that prevents repository writes, so
  ADR-0003's application-level mutation detection is again the only protection.
- Native execution shares the operator's user account, so the worker is not isolated from the
  wider filesystem or from Audit credentials beyond the existing environment controls.
- Container support is exercised only by CI until the hold is lifted.

## Rejected alternatives

- Removing the container deployment: discards reviewed, working security controls for a
  constraint expected to be temporary.
- Skipping the container tests in CI while on hold: the ADR-0003 and ADR-0005 boundaries would
  stop being verified exactly while the code around them changes.
- An API key inside the container: metered billing, which is the constraint that led here.
- Injecting a Keychain-extracted credential: gives Maestro a rotating secret it is designed not
  to hold.
- Running the worker on the host beside a containerized Maestro: removes the read-only mount
  boundary while keeping the operational cost of containers.
