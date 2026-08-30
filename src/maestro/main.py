"""Console entry point for the local stdio MCP server."""

from __future__ import annotations

import logging
import os

from maestro import __version__
from maestro.agents.codex import CODEX_PROVIDER, CodexAgentRuntime
from maestro.audit import AuditRecorder, AuditRuntimeMetadata
from maestro.audit.postgres import PostgresAuditPort
from maestro.capabilities.resolve_codebase_fact.policy import POLICY_VERSION
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import Settings
from maestro.errors import RecursionNotAllowedError
from maestro.mcp.server import create_server
from maestro.observability import configure_logging
from maestro.versions import verify_runtime_versions

# The provider this deployment is built against. Startup verification and Audit metadata
# read identity from it, so adding a second adapter changes this selection rather than the
# boundary around it.
PROVIDER = CODEX_PROVIDER


def build_service(settings: Settings) -> ResolveCodebaseFactService:
    """Build production dependencies without leaking transport types inward."""

    audit = AuditRecorder(
        PostgresAuditPort(settings.audit_writer_configuration()),
        AuditRuntimeMetadata(
            server_version=__version__,
            runtime_name=PROVIDER.name,
            runtime_version=PROVIDER.version,
            model=settings.codex_model,
            prompt_policy_version=POLICY_VERSION,
        ),
    )
    return ResolveCodebaseFactService(
        settings,
        CodexAgentRuntime(settings.codex_runtime_configuration()),
        audit,
    )


def main() -> None:
    """Validate configuration, log versions to stderr, and run stdio transport."""

    if os.environ.get("MAESTRO_VERIFIER_DEPTH") is not None:
        raise RecursionNotAllowedError
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from BaseSettings env
    versions = verify_runtime_versions(PROVIDER)
    configure_logging(settings.log_level)
    logging.getLogger("maestro").info(
        "server starting",
        extra={
            "metadata": {
                "server_version": __version__,
                "mcp_sdk_version": versions.mcp_sdk,
                "agent_runtime": versions.agent_runtime,
                "agent_runtime_version": versions.agent_runtime_version,
                "model": settings.codex_model.value,
            }
        },
    )
    server = create_server(build_service(settings))
    # MCPServer configures its own handler during construction; restore Maestro's
    # structured stderr-only policy before accepting requests.
    configure_logging(settings.log_level)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
