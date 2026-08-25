# Contributing

Use Python 3.13+ and `uv`. Keep v1 limited to `resolve_codebase_fact`; changes that introduce
Jobs, durable state, remote transport, integrations, or subagents require separate planning
and architectural decisions.

```bash
uv sync --frozen --all-groups
uv run pre-commit install
uv run pre-commit run --all-files
```

Before submitting a change, run the exact deterministic gate from the implementation task:

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

Verify the reviewed secret baseline without modifying it:

```bash
uv run pre-commit run detect-secrets --all-files
```

If a baseline update is intentional, generate the candidate, inspect every diff entry, delete
and rotate any real credential, and use `detect-secrets audit` to label only synthetic fixture
or documentation findings as false positives. The current reviewed entries cover API-key
presence/propagation tests and secret-redaction test strings; none is a usable credential. A
baseline is not an allowlist for real secrets.

Build and check distribution metadata:

```bash
uv build
uv run twine check dist/*
```

Install the wheel into a clean temporary environment and run `scripts/package_smoke.py`
against its installed `maestro` executable. The deterministic suite uses no network or AI
credential. Run opt-in model checks only from a trusted branch with explicit credentials:

```bash
uv run pytest -m e2e
uv run python scripts/run_evals.py
```

Changes to the Dockerfile, launcher, deployment settings, container tests, image scan, stdio
container wiring, or related security claims require the complete Level 2 gate:

```bash
docker build --check .
docker build --tag maestro-verifier:local .

MAESTRO_CONTAINER_IMAGE=maestro-verifier:local \
MAESTRO_CONTAINER_SKIP_BUILD=1 \
uv run pytest -m "container and not e2e" tests/container

trivy image --config trivy.yaml --ignore-unfixed=false --exit-code 0 maestro-verifier:local
trivy image --config trivy.yaml maestro-verifier:local
```

`trivy.yaml` is the authoritative blocking policy. The first pass reports all HIGH/CRITICAL
findings without failing for vulnerability presence; scanner errors still fail. The second pass
scans OS/library vulnerabilities, image-filesystem secrets, and image-configuration secrets and
fails for fixable HIGH/CRITICAL findings.
Do not add a broad `.trivyignore`. A specific exception requires a documented review. The live
Codex write probe is separate and trusted-environment only:

```bash
MAESTRO_CONTAINER_IMAGE=maestro-verifier:local \
MAESTRO_CONTAINER_SKIP_BUILD=1 \
uv run pytest -m "container and e2e" tests/container/test_codex_container_e2e.py
```

If provider authorization is unavailable, report that probe as unexecuted. Deterministic
host-side write attempts remain mandatory and are not replaced by the model-driven probe.

Validate the actual stdio executable with the official MCP Inspector. Replace the paths in
`SERVER` and `REPOSITORY`; Inspector's `-e` options, rather than its parent environment,
configure the child server:

```bash
SERVER=/absolute/path/to/maestro/.venv/bin/maestro
REPOSITORY=/absolute/allowed/repository

npx --yes @modelcontextprotocol/inspector@2.2.0 --cli "$SERVER" \
  -e MAESTRO_ALLOWED_ROOTS="$REPOSITORY" -e MAESTRO_LOG_LEVEL=WARNING \
  -e MAESTRO_AUDIT_DATABASE_URL=postgresql://audit-writer@127.0.0.1:1/maestro \
  --method tools/list --format json

npx --yes @modelcontextprotocol/inspector@2.2.0 --cli "$SERVER" \
  -e MAESTRO_ALLOWED_ROOTS="$REPOSITORY" -e MAESTRO_LOG_LEVEL=WARNING \
  -e MAESTRO_AUDIT_DATABASE_URL=postgresql://audit-writer@127.0.0.1:1/maestro \
  --method tools/call --tool-name resolve_codebase_fact \
  --tool-args-json "{\"repository_path\":\"$REPOSITORY\",\"question\":\"Should an Order support multiple Payments?\"}" \
  --format json

npx --yes @modelcontextprotocol/inspector@2.2.0 --cli "$SERVER" \
  -e MAESTRO_ALLOWED_ROOTS="$REPOSITORY" -e MAESTRO_LOG_LEVEL=WARNING \
  -e MAESTRO_AUDIT_DATABASE_URL=postgresql://audit-writer@127.0.0.1:1/maestro \
  --method tools/call --tool-name resolve_codebase_fact --tool-args-json '{}' --format json
```

The normative call returns `AUDIT_UNAVAILABLE` because the example Audit endpoint deliberately
refuses connections, while discovery remains available. For the expected authorization-error
check, call with an existing `repository_path` outside
`MAESTRO_ALLOWED_ROOTS`; the tool must return `REPOSITORY_NOT_ALLOWED`. The invalid and
operational-error commands return Inspector exit status 5 because `isError` is true. Inspector
2.2.0 emits two JSON lines for these cases: the tool result followed by its CLI error object.
Machine-parseable JSON/NDJSON from every invocation also confirms that server logs did not
corrupt stdout. Update the pinned Inspector version only after reviewing its current CLI contract.

For the container boundary, point Inspector at the launcher through the project interpreter:

```bash
PYTHON=/absolute/path/to/maestro/.venv/bin/python
LAUNCHER=/absolute/path/to/maestro/scripts/maestro_container.py
REPOSITORY=/absolute/allowed/repository

npx --yes @modelcontextprotocol/inspector@2.2.0 --cli "$PYTHON" "$LAUNCHER" \
  -e MAESTRO_ALLOWED_ROOTS="$REPOSITORY" -e MAESTRO_LOG_LEVEL=WARNING \
  -e MAESTRO_DOCKER_IMAGE=maestro-verifier:local \
  --method tools/list --format json
```

Also execute the credential-free normative `tools/call`, invalid-input call, and outside-root
operational-error call from the native Inspector procedure through this container command.

Every defect fix needs a regression test or eval. Contract changes must update the schema
snapshot, SemVer, documentation, and—if breaking—an ADR. Preserve stdout for MCP protocol;
tests and application diagnostics belong on stderr. Never log or commit prompts, full model
responses, repository source, absolute private paths, or credentials.
