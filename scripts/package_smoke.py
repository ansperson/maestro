"""Exercise an installed wheel's real stdio console entry point."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.types import TextContent

_EXPECTED_ARGUMENT_COUNT = 3


async def run(executable: Path, repository: Path) -> None:
    """Discover and call the package-installed stdio server without AI credentials."""

    parameters = StdioServerParameters(
        command=str(executable),
        env={
            "MAESTRO_ALLOWED_ROOTS": str(repository),
            "MAESTRO_AUDIT_DATABASE_URL": "postgresql://audit-writer@127.0.0.1:1/maestro",
            "MAESTRO_LOG_LEVEL": "WARNING",
        },
    )
    async with Client(parameters, read_timeout_seconds=10) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "resolve_codebase_fact",
            {
                "repository_path": str(repository),
                "question": "Should an Order support multiple Payments?",
            },
        )
    if [tool.name for tool in tools.tools] != ["resolve_codebase_fact"]:
        raise RuntimeError("installed server exposed an unexpected tool contract")
    block = result.content[0]
    if not result.is_error or not isinstance(block, TextContent):
        raise RuntimeError("installed server did not fail closed without Audit connectivity")
    if "AUDIT_UNAVAILABLE" not in block.text:
        raise RuntimeError("installed server returned an unexpected Audit error")
    print(
        json.dumps(
            {
                "package_smoke": "passed",
                "tool": "resolve_codebase_fact",
                "status": "audit_unavailable",
            },
            separators=(",", ":"),
        )
    )


def main() -> None:
    """Validate arguments and run the installed server smoke test."""

    if len(sys.argv) != _EXPECTED_ARGUMENT_COUNT:
        raise SystemExit("usage: package_smoke.py EXECUTABLE REPOSITORY")
    executable = Path(sys.argv[1]).resolve(strict=True)
    repository = Path(sys.argv[2]).resolve(strict=True)
    if not repository.is_dir():
        raise SystemExit("REPOSITORY must be a directory")
    asyncio.run(run(executable, repository))


if __name__ == "__main__":
    main()
