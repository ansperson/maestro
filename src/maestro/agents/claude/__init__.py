"""Claude Code binary runtime adapter."""

from maestro.agents.claude.runtime import ClaudeAgentRuntime, verify_executable_version
from maestro.agents.runtime import AgentRuntimeProvider

# The binary updates itself, so the gate is a floor rather than an exact pin. Raise it
# only when the adapter depends on behavior a lower version does not provide.
CLAUDE_MINIMUM_VERSION = (2, 1, 251)

CLAUDE_PROVIDER = AgentRuntimeProvider(
    name="claude",
    version=".".join(str(part) for part in CLAUDE_MINIMUM_VERSION),
    # The worker is an external binary, not a distribution, so there is no package pin to
    # verify. Its version is checked directly against the floor above.
    distributions={},
)

__all__ = [
    "CLAUDE_MINIMUM_VERSION",
    "CLAUDE_PROVIDER",
    "ClaudeAgentRuntime",
    "verify_executable_version",
]
