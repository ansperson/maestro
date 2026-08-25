"""Console entry point for the local stdio MCP server."""

from __future__ import annotations

import logging
import os

from maestro import __version__
from maestro.agents.codex import CodexAgentRuntime
from maestro.audit import AuditRecorder, AuditRuntimeMetadata
from maestro.audit.postgres import PostgresAuditPort
from maestro.capabilities.resolve_codebase_fact.policy import POLICY_VERSION
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import Settings
from maestro.errors import RecursionNotAllowedError
from maestro.mcp.server import create_server
from maestro.observability import configure_logging
from maestro.versions import CODEX_RUNTIME_VERSION, verify_runtime_versions


def build_service(settings: Settings) -> ResolveCodebaseFactService:
    """Build production dependencies without leaking transport types inward."""

    audit = AuditRecorder(
        PostgresAuditPort(settings.audit_database_url),
        AuditRuntimeMetadata(
            server_version=__version__,
            runtime_name="codex",
            runtime_version=CODEX_RUNTIME_VERSION,
            model=settings.codex_model,
            prompt_policy_version=POLICY_VERSION,
        ),
    )
    return ResolveCodebaseFactService(settings, CodexAgentRuntime(settings), audit)


def main() -> None:
    """Validate configuration, log versions to stderr, and run stdio transport."""

    if os.environ.get("MAESTRO_VERIFIER_DEPTH") is not None:
        raise RecursionNotAllowedError
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from BaseSettings env
    versions = verify_runtime_versions()
    configure_logging(settings.log_level)
    logging.getLogger("maestro").info(
        "server starting",
        extra={
            "metadata": {
                "server_version": __version__,
                "mcp_sdk_version": versions.mcp_sdk,
                "codex_sdk_version": versions.codex_sdk,
                "codex_runtime_version": versions.codex_runtime,
                "model": settings.codex_model,
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
