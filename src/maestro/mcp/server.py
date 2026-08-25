"""Thin official-MCP adapter for Maestro's single public Capability."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, ToolAnnotations
from pydantic import Field, ValidationError

from maestro import __version__
from maestro.capabilities.resolve_codebase_fact.contracts import (
    MAX_CONTEXT_CHARS,
    MAX_QUESTION_CHARS,
    MAX_REPOSITORY_PATH_CHARS,
    ResolveCodebaseFactRequest,
    VerificationResult,
)
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.errors import MaestroError

SERVER_NAME = "maestro"
TOOL_NAME = "resolve_codebase_fact"
TOOL_TITLE = "Resolve codebase fact"
SERVER_INSTRUCTIONS = (
    "Use resolve_codebase_fact only for objective facts about the current state of an allowed "
    "repository. Do not use it for product, business, UX, risk, or architecture decisions. "
    "Results are resolved, uncertain, or human_decision_required."
)
TOOL_DESCRIPTION = (
    "Independently investigate one objective fact about the current state of an allowed "
    "repository and return validated, repository-relative evidence. Normative or authority "
    "questions return human_decision_required; insufficient or contradictory evidence returns "
    "uncertain."
)
_LOGGER = logging.getLogger("maestro.mcp")
_INVALID_INPUT_PAYLOAD = '{"code":"INVALID_INPUT","message":"The request is invalid."}'


class _SafeInputMCPServer(MCPServer[None]):
    """Use the high-level SDK while preventing rejected-value reflection."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],  # pyright: ignore[reportExplicitAny]
        context: Context[None, Any] | None = None,  # pyright: ignore[reportExplicitAny]
    ) -> CallToolResult | InputRequiredResult:
        """Map SDK argument validation to Maestro's stable safe input error."""

        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                raise ToolError(_INVALID_INPUT_PAYLOAD) from exc
            raise


def create_server(service: ResolveCodebaseFactService) -> MCPServer[None]:
    """Create the deterministic one-tool MCP server."""

    @asynccontextmanager
    async def lifespan(_server: MCPServer[None]) -> AsyncGenerator[None]:
        try:
            yield None
        finally:
            await service.shutdown()

    server: MCPServer[None] = _SafeInputMCPServer(
        name=SERVER_NAME,
        title="Maestro Engineering Verifier",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool(
        name=TOOL_NAME,
        title=TOOL_TITLE,
        description=TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            title=TOOL_TITLE,
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    async def resolve_codebase_fact(  # pyright: ignore[reportUnusedFunction]
        repository_path: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_REPOSITORY_PATH_CHARS,
                description="Path to the allowed repository or authorized subdirectory.",
            ),
        ],
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_QUESTION_CHARS,
                description="Objective question about what is currently true in the repository.",
            ),
        ],
        context: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=MAX_CONTEXT_CHARS,
                description="Optional untrusted background; never treated as evidence.",
            ),
        ] = None,
    ) -> VerificationResult:
        try:
            request = ResolveCodebaseFactRequest(
                repository_path=repository_path,
                question=question,
                context=context,
            )
            return await service.execute(request)
        except MaestroError as exc:
            raise ToolError(exc.public_json()) from exc
        except ValidationError as exc:
            raise ToolError(_INVALID_INPUT_PAYLOAD) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.exception("unexpected tool failure")
            raise ToolError(
                '{"code":"INTERNAL_ERROR","message":"Maestro could not complete the request."}'
            ) from exc

    return server
