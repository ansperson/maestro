"""Exercise an installed wheel's real stdio console entry point."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.types import TextContent

_EXPECTED_ARGUMENT_COUNT = 3


async def run(executable: Path, repository: Path) -> None:
    """Discover and call the package-installed stdio server without AI credentials."""

    with tempfile.TemporaryDirectory(prefix="maestro-package-smoke-") as temporary:
        password_file = Path(temporary) / "audit-writer-password"
        password_file.write_text("synthetic-password", encoding="utf-8")
        password_file.chmod(0o600)
        parameters = StdioServerParameters(
            command=str(executable),
            env={
                "MAESTRO_ALLOWED_ROOTS": str(repository),
                "MAESTRO_AUDIT_WRITER_HOST": "127.0.0.1",
                "MAESTRO_AUDIT_WRITER_PORT": "1",
                "MAESTRO_AUDIT_WRITER_USER": "maestro_audit_writer",
                "MAESTRO_AUDIT_WRITER_PASSWORD_FILE": str(password_file),
                "MAESTRO_AGENT_RUNTIME": "codex",
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
