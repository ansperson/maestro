#!/usr/bin/env python3
"""Ask the running server one question over the real stdio MCP transport.

This is the development entry point behind `make ask`. A production caller is an MCP
client, not a person, so this exists to demonstrate the loop rather than to serve it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.types import TextContent

from maestro.capabilities.resolve_codebase_fact.contracts import VerificationResult

_PASSTHROUGH = ("HOME", "PATH", "USER")


def _server_environment(repository: str, port: str, secrets: str) -> dict[str, str]:
    """Build the server's environment explicitly.

    Ambient libpq variables are rejected for Audit, so the parent environment is never
    forwarded wholesale.
    """

    environment = {
        name: value for name in _PASSTHROUGH if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "MAESTRO_ALLOWED_ROOTS": repository,
            "MAESTRO_AGENT_RUNTIME": os.environ.get("MAESTRO_AGENT_RUNTIME", "claude"),
            "MAESTRO_AUDIT_WRITER_HOST": "127.0.0.1",
            "MAESTRO_AUDIT_WRITER_PORT": port,
            "MAESTRO_AUDIT_WRITER_DATABASE": "maestro",
            "MAESTRO_AUDIT_WRITER_USER": "maestro_audit_writer",
            "MAESTRO_AUDIT_WRITER_PASSWORD_FILE": str(Path(secrets) / "writer-password"),
            "MAESTRO_LOG_LEVEL": os.environ.get("MAESTRO_LOG_LEVEL", "WARNING"),
        }
    )
    for name in ("MAESTRO_CLAUDE_MODEL", "MAESTRO_CLAUDE_EFFORT", "MAESTRO_CLAUDE_MAX_BUDGET_USD"):
        if (value := os.environ.get(name)) is not None:
            environment[name] = value
    return environment


def _render(result: VerificationResult) -> None:
    print(f"\n  status      {result.status.value}")
    print(f"  confidence  {result.confidence.value}")
    if result.answer is not None:
        print(f"\n  {result.answer}")
    print(f"\n  reason      {result.reason}")
    if result.evidence:
        print("\n  evidence")
        for item in result.evidence:
            span = f":{item.line_start}" if item.line_start is not None else ""
            print(f"    {item.path}{span}  {item.finding}")
    for conflict in result.conflicts:
        print(f"\n  conflict    {conflict.description}")


async def main() -> int:
    """Start the server, ask one question, and render the validated result."""

    repository, port, secrets, question = sys.argv[1:5]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "maestro.main"],
        env=_server_environment(repository, port, secrets),
    )
    async with Client(parameters, read_timeout_seconds=300) as client:
        tools = await client.list_tools()
        print(f"  tools       {[tool.name for tool in tools.tools]}")
        print(f"  question    {question}")
        outcome = await client.call_tool(
            "resolve_codebase_fact", {"repository_path": repository, "question": question}
        )
    if outcome.is_error:
        for block in outcome.content:
            if isinstance(block, TextContent):
                print(f"\n  failed      {block.text}")
        return 1
    _render(
        VerificationResult.model_validate_json(json.dumps(outcome.structured_content), strict=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
