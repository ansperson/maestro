"""Pinned dependency versions used at the runtime trust boundary."""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass

from maestro.agents.runtime import AgentRuntimeProvider
from maestro.errors import AgentRuntimeError

MCP_SDK_VERSION = "2.1.0"


@dataclass(frozen=True, slots=True)
class RuntimeVersions:
    """Installed versions safe to include in structured startup logs."""

    mcp_sdk: str
    agent_runtime: str
    agent_runtime_version: str


def verify_runtime_versions(provider: AgentRuntimeProvider) -> RuntimeVersions:
    """Fail before serving if the transport or the selected provider's pins are unsatisfied.

    Only the supplied provider's distributions are required, so a deployment built against
    one provider does not need another provider's packages installed.
    """

    expected = {"mcp": MCP_SDK_VERSION, **provider.distributions}
    installed: dict[str, str] = {}
    try:
        for distribution in expected:
            installed[distribution] = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise AgentRuntimeError(
            "A required pinned SDK or agent runtime package is missing."
        ) from exc
    if installed != expected:
        raise AgentRuntimeError("The installed SDK or agent runtime version is unsupported.")
    return RuntimeVersions(
        mcp_sdk=installed["mcp"],
        agent_runtime=provider.name,
        agent_runtime_version=provider.version,
    )
