"""Official Codex Python SDK runtime adapter."""

from maestro.agents.codex.runtime import CodexAgentRuntime
from maestro.agents.runtime import AgentRuntimeProvider

CODEX_SDK_VERSION = "0.147.0"
CODEX_RUNTIME_VERSION = "0.147.0"

CODEX_PROVIDER = AgentRuntimeProvider(
    name="codex",
    version=CODEX_RUNTIME_VERSION,
    distributions={
        "openai-codex": CODEX_SDK_VERSION,
        "openai-codex-cli-bin": CODEX_RUNTIME_VERSION,
    },
)

__all__ = [
    "CODEX_PROVIDER",
    "CODEX_RUNTIME_VERSION",
    "CODEX_SDK_VERSION",
    "CodexAgentRuntime",
]
