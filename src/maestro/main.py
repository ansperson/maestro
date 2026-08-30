"""Console entry point for the local stdio MCP server."""

from __future__ import annotations

import logging
import os

from maestro import __version__
from maestro.agents.claude import (
    CLAUDE_MINIMUM_VERSION,
    CLAUDE_PROVIDER,
    ClaudeAgentRuntime,
    verify_executable_version,
)
from maestro.agents.codex import CODEX_PROVIDER, CodexAgentRuntime
from maestro.agents.runtime import AgentRuntime, AgentRuntimeProvider
from maestro.audit import AuditRecorder, AuditRuntimeMetadata
from maestro.audit.postgres import PostgresAuditPort
from maestro.capabilities.resolve_codebase_fact.policy import POLICY_VERSION
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import AgentRuntimeName, Settings
from maestro.errors import RecursionNotAllowedError
from maestro.mcp.server import create_server
from maestro.observability import configure_logging
from maestro.versions import verify_runtime_versions

_PROVIDERS = {
    AgentRuntimeName.CODEX: CODEX_PROVIDER,
    AgentRuntimeName.CLAUDE: CLAUDE_PROVIDER,
}


def selected_provider(settings: Settings) -> AgentRuntimeProvider:
    """Return the provider this deployment selected, with its real installed version."""

    provider = _PROVIDERS[settings.agent_runtime]
    if settings.agent_runtime is not AgentRuntimeName.CLAUDE:
        return provider
    # The external binary is not a pinned distribution, so its version is verified
    # directly and the real one is recorded rather than the floor.
    installed = verify_executable_version(settings.claude_executable, CLAUDE_MINIMUM_VERSION)
    return AgentRuntimeProvider(
        name=provider.name, version=installed, distributions=provider.distributions
    )


def build_runtime(settings: Settings) -> AgentRuntime:
    """Construct the selected worker adapter."""

    if settings.agent_runtime is AgentRuntimeName.CLAUDE:
        return ClaudeAgentRuntime(settings.claude_runtime_configuration())
    return CodexAgentRuntime(settings.codex_runtime_configuration())


def build_service(settings: Settings) -> ResolveCodebaseFactService:
    """Build production dependencies without leaking transport types inward."""

    provider = selected_provider(settings)
    audit = AuditRecorder(
        PostgresAuditPort(settings.audit_writer_configuration()),
        AuditRuntimeMetadata(
            server_version=__version__,
            runtime_name=provider.name,
            runtime_version=provider.version,
            model=settings.agent_model(),
            prompt_policy_version=POLICY_VERSION,
        ),
    )
    return ResolveCodebaseFactService(settings, build_runtime(settings), audit)


def main() -> None:
    """Validate configuration, log versions to stderr, and run stdio transport."""

    if os.environ.get("MAESTRO_VERIFIER_DEPTH") is not None:
        raise RecursionNotAllowedError
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from BaseSettings env
    versions = verify_runtime_versions(selected_provider(settings))
    configure_logging(settings.log_level)
    logging.getLogger("maestro").info(
        "server starting",
        extra={
            "metadata": {
                "server_version": __version__,
                "mcp_sdk_version": versions.mcp_sdk,
                "agent_runtime": versions.agent_runtime,
                "agent_runtime_version": versions.agent_runtime_version,
                "model": settings.agent_model().value,
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
