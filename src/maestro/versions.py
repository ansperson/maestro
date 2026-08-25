"""Pinned dependency versions used at the runtime trust boundary."""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass

from maestro.errors import AgentRuntimeError

MCP_SDK_VERSION = "2.1.0"
CODEX_SDK_VERSION = "0.147.0"
CODEX_RUNTIME_VERSION = "0.147.0"


@dataclass(frozen=True, slots=True)
class RuntimeVersions:
    """Installed versions safe to include in structured startup logs."""

    mcp_sdk: str
    codex_sdk: str
    codex_runtime: str


def verify_runtime_versions() -> RuntimeVersions:
    """Fail before serving if the pinned SDK/runtime set is absent or mismatched."""

    expected = {
        "mcp": MCP_SDK_VERSION,
        "openai-codex": CODEX_SDK_VERSION,
        "openai-codex-cli-bin": CODEX_RUNTIME_VERSION,
    }
    installed: dict[str, str] = {}
    try:
        for distribution in expected:
            installed[distribution] = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise AgentRuntimeError(
            "A required pinned SDK or Codex runtime package is missing."
        ) from exc
    if installed != expected:
        raise AgentRuntimeError("The installed SDK or Codex runtime version is unsupported.")
    return RuntimeVersions(
        mcp_sdk=installed["mcp"],
        codex_sdk=installed["openai-codex"],
        codex_runtime=installed["openai-codex-cli-bin"],
    )
