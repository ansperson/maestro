from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, TextContent

from maestro.agents import FakeAgentRuntime
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind
from maestro.audit.testing import FakeAuditPort, fake_audit_recorder
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Evidence,
    ResolveCodebaseFactRequest,
    VerificationResult,
    VerificationStatus,
)
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import Settings
from maestro.errors import ServerBusyError
from maestro.mcp.server import (
    SERVER_INSTRUCTIONS,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_TITLE,
    create_server,
)

SettingsFactory = Callable[..., Settings]


def _first_text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _resolved_result() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="The model stores zero or more payments.",
        confidence=Confidence.HIGH,
        evidence=[
            Evidence(
                path="src/models.py",
                line_start=1,
                line_end=3,
                symbol="Order.payments",
                finding="The field is a list.",
            )
        ],
        conflicts=[],
        reason="The source directly establishes the current representation.",
    )


def _service(repository: Path, settings_factory: SettingsFactory) -> ResolveCodebaseFactService:
    settings = settings_factory(allowed_roots=(repository,))
    return ResolveCodebaseFactService(
        settings,
        FakeAgentRuntime(lambda _request: _resolved_result()),
        fake_audit_recorder(),
    )


@pytest.mark.asyncio
async def test_in_memory_tool_contract_and_schema_snapshot(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    service = _service(repository, settings_factory)
    server = create_server(service)
    assert server.name == "maestro"
    assert server.instructions == SERVER_INSTRUCTIONS

    async with Client(server) as client:
        first = await client.list_tools()
        second = await client.list_tools()

    assert first.tools == second.tools
    assert len(first.tools) == 1
    tool = first.tools[0]
    assert tool.name == TOOL_NAME
    assert tool.title == TOOL_TITLE
    assert tool.description == TOOL_DESCRIPTION
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is False
    assert tool.input_schema["required"] == ["repository_path", "question"]
    assert tool.output_schema is not None
    assert tool.output_schema["additionalProperties"] is False

    payload = tool.model_dump(
        mode="json",
        by_alias=True,
        exclude={"execution", "icons", "meta"},
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    snapshot = Path(__file__).parent / "snapshots" / "resolve_codebase_fact.schema.sha256"
    assert digest == snapshot.read_text(encoding="utf-8").strip()

    with pytest.raises(ServerBusyError, match="shutting down"):
        await service.execute(
            ResolveCodebaseFactRequest(
                repository_path=str(repository),
                question="Is the payment field a list?",
            )
        )


@pytest.mark.asyncio
async def test_in_memory_success_has_structured_and_text_content(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    async with Client(create_server(_service(repository, settings_factory))) as client:
        result = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository),
                "question": "Is the payment field a list?",
            },
        )

    assert result.is_error is False
    assert result.structured_content is not None
    validated = VerificationResult.model_validate_json(
        json.dumps(result.structured_content), strict=True
    )
    assert validated.status is VerificationStatus.RESOLVED
    assert len(result.content) == 1
    fallback = json.loads(_first_text(result))
    assert fallback == result.structured_content


@pytest.mark.asyncio
async def test_in_memory_errors_are_stable_and_do_not_echo_rejected_values(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    rejected_value = "sk-" + "this-value-must-not-be-reflected"
    async with Client(create_server(_service(repository, settings_factory))) as client:
        invalid = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository),
                "question": rejected_value,
                "context": "x" * 8_001,
            },
        )
        unauthorized = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository.parent),
                "question": "Is this allowed?",
            },
        )
        unknown = await client.call_tool("not_a_tool", {})

    invalid_text = _first_text(invalid)
    assert invalid.is_error is True
    assert "INVALID_INPUT" in invalid_text
    assert rejected_value not in invalid_text
    unauthorized_text = _first_text(unauthorized)
    assert unauthorized.is_error is True
    assert "REPOSITORY_NOT_ALLOWED" in unauthorized_text
    assert str(repository) not in unauthorized_text
    assert unknown.is_error is True
    assert unknown.structured_content is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED, "AUDIT_UNAVAILABLE"),
        (AuditWriteFailureKind.PERMANENT, "AUDIT_PERSISTENCE_ERROR"),
        (AuditWriteFailureKind.AMBIGUOUS, "AUDIT_PERSISTENCE_ERROR"),
    ],
)
async def test_in_memory_audit_failures_use_stable_public_errors(
    repository: Path,
    settings_factory: SettingsFactory,
    failure: AuditWriteFailureKind,
    code: str,
) -> None:
    def fail_start(_record: object) -> None:
        raise AuditWriteError(failure)

    settings = settings_factory(allowed_roots=(repository,))
    service = ResolveCodebaseFactService(
        settings,
        FakeAgentRuntime(lambda _request: _resolved_result()),
        fake_audit_recorder(FakeAuditPort(on_start=fail_start)),
    )
    async with Client(create_server(service)) as client:
        result = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository),
                "question": "Is the payment field a list?",
            },
        )

    text = _first_text(result)
    assert result.is_error is True
    assert code in text


@pytest.mark.asyncio
async def test_in_memory_untyped_audit_failure_does_not_expose_adapter_detail(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    private_detail = "SQLSTATE=99999 host=db.internal user=audit SQL=private"

    def fail_start(_record: object) -> None:
        raise RuntimeError(private_detail)

    settings = settings_factory(allowed_roots=(repository,))
    service = ResolveCodebaseFactService(
        settings,
        FakeAgentRuntime(lambda _request: _resolved_result()),
        fake_audit_recorder(FakeAuditPort(on_start=fail_start)),
    )
    async with Client(create_server(service)) as client:
        result = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository),
                "question": "Is the payment field a list?",
            },
        )

    text = _first_text(result)
    assert result.is_error is True
    assert "AUDIT_PERSISTENCE_ERROR" in text
    assert private_detail not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED, "AUDIT_UNAVAILABLE"),
        (AuditWriteFailureKind.PERMANENT, "AUDIT_PERSISTENCE_ERROR"),
    ],
)
async def test_real_stdio_maps_both_audit_error_categories(
    repository: Path,
    failure: AuditWriteFailureKind,
    code: str,
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            str(Path(__file__).parent / "helpers" / "fake_audit_error_server.py"),
            str(repository),
            failure.value,
        ],
        cwd=Path(__file__).parent.parent,
    )
    async with Client(parameters, read_timeout_seconds=5) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository),
                "question": "Is the payment field a list?",
            },
        )

    assert [tool.name for tool in tools.tools] == [TOOL_NAME]
    assert result.is_error is True
    assert code in _first_text(result)


@pytest.mark.asyncio
async def test_real_stdio_server_discovery_call_errors_and_clean_shutdown(repository: Path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "maestro.main"],
        cwd=Path(__file__).parent.parent,
        env={
            "MAESTRO_ALLOWED_ROOTS": str(repository),
            "MAESTRO_AUDIT_DATABASE_URL": "postgresql://audit-writer@127.0.0.1:1/maestro",
            "MAESTRO_LOG_LEVEL": "INFO",
        },
    )
    async with Client(parameters, read_timeout_seconds=5) as client:
        tools = await client.list_tools()
        invalid = await client.call_tool(TOOL_NAME, {})
        unauthorized = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository.parent),
                "question": "Is this repository allowed?",
            },
        )
        unavailable = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository),
                "question": "Should an Order support multiple Payments?",
            },
        )

    assert [tool.name for tool in tools.tools] == [TOOL_NAME]
    assert invalid.is_error is True
    assert "INVALID_INPUT" in _first_text(invalid)
    assert unauthorized.is_error is True
    assert "REPOSITORY_NOT_ALLOWED" in _first_text(unauthorized)
    assert unavailable.is_error is True
    assert "AUDIT_UNAVAILABLE" in _first_text(unavailable)


@pytest.mark.asyncio
async def test_unexpected_tool_failure_is_generic_and_safe(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    detail = "private-unexpected-detail"

    def crash(_request: object) -> VerificationResult:
        raise RuntimeError(detail)

    settings = settings_factory(allowed_roots=(repository,))
    service = ResolveCodebaseFactService(settings, FakeAgentRuntime(crash), fake_audit_recorder())
    async with Client(create_server(service)) as client:
        result = await client.call_tool(
            TOOL_NAME,
            {
                "repository_path": str(repository),
                "question": "Is the payment field a list?",
            },
        )
    text = _first_text(result)
    assert result.is_error is True
    assert "INTERNAL_ERROR" in text
    assert detail not in text
