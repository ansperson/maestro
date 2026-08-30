from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from maestro.agents.claude import CLAUDE_MINIMUM_VERSION, verify_executable_version
from maestro.agents.claude.runtime import ClaudeAgentRuntime
from maestro.agents.runtime import InvestigationRequest
from maestro.config import ClaudeEffort, ClaudeRuntimeConfiguration
from maestro.errors import AgentRuntimeError, InvalidAgentOutputError
from maestro.model_identity import ModelIdentifier

_FIXTURE = Path(__file__).parent / "helpers" / "fake_claude_binary.py"


def _configuration(effort: ClaudeEffort = ClaudeEffort.MEDIUM) -> ClaudeRuntimeConfiguration:
    return ClaudeRuntimeConfiguration(executable=sys.executable, effort=effort, max_budget_usd=1.0)


def _runtime(
    mode: str, *arguments: str, effort: ClaudeEffort = ClaudeEffort.MEDIUM
) -> tuple[ClaudeAgentRuntime, list[str]]:
    """Build a runtime whose command runs the fixture instead of the real binary."""

    runtime = ClaudeAgentRuntime(_configuration(effort))
    prefix = [sys.executable, "-I", str(_FIXTURE), mode, *arguments]
    original = runtime._build_command  # pyright: ignore[reportPrivateUsage]

    def build(request: InvestigationRequest) -> tuple[str, ...]:
        return (*prefix, *original(request)[1:])

    runtime._build_command = build  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]
    return runtime, prefix


def _request(repository: Path, **overrides: object) -> InvestigationRequest:
    values: dict[str, object] = {
        "repository_root": repository,
        "question": "Can an Order have many Payments?",
        "context": None,
        "repository_fingerprint": "fixture-digest",
        "model": ModelIdentifier("claude-opus-5"),
        "max_output_bytes": 131_072,
    }
    values.update(overrides)
    return InvestigationRequest(**values)  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_successful_investigation_returns_validated_result(repository: Path) -> None:
    runtime, _ = _runtime("success")

    result = await runtime.investigate(_request(repository))

    assert result.status.value == "resolved"
    assert result.evidence[0].path == "src/models.py"


@pytest.mark.asyncio
async def test_command_grants_read_only_tools_and_pins_model_and_budget(
    repository: Path, tmp_path: Path
) -> None:
    """The worker must receive read access only, with the deployment's pinned identity."""

    record = tmp_path / "argv.json"
    runtime, _ = _runtime("record-argv", str(record), effort=ClaudeEffort.HIGH)

    await runtime.investigate(_request(repository))

    captured = json.loads(record.read_text(encoding="utf-8"))
    argv = captured["argv"]
    assert "--print" in argv
    allowed = argv[argv.index("--allowed-tools") + 1]
    disallowed = argv[argv.index("--disallowed-tools") + 1]
    assert allowed == "Read,Glob,Grep"
    for forbidden in ("Write", "Edit", "Bash", "WebSearch", "WebFetch"):
        assert forbidden in disallowed
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.0"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["status"] is not None


@pytest.mark.asyncio
async def test_worker_environment_excludes_audit_and_provider_credentials(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maestro passes no credential of its own; the binary resolves the operator's."""

    record = tmp_path / "argv.json"
    monkeypatch.setenv("MAESTRO_AUDIT_WRITER_PASSWORD_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-forwarded")
    runtime, _ = _runtime("record-argv", str(record))

    await runtime.investigate(_request(repository))

    forwarded = json.loads(record.read_text(encoding="utf-8"))["env"]
    assert "ANTHROPIC_API_KEY" not in forwarded
    assert not [name for name in forwarded if name.startswith("MAESTRO_")]
    # macOS injects its own locale variables into every process. They are outside the
    # launcher's allowlist and outside Maestro's control, so compare what Maestro passes.
    platform_injected = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
    assert set(forwarded) - platform_injected <= {"HOME", "PATH", "USER"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("runtime-error", AgentRuntimeError),
        ("nonzero", AgentRuntimeError),
        ("malformed-envelope", InvalidAgentOutputError),
        ("invalid-result", InvalidAgentOutputError),
        ("semantic-violation", InvalidAgentOutputError),
    ],
)
async def test_unusable_responses_fail_closed_with_safe_errors(
    repository: Path, mode: str, expected: type[Exception]
) -> None:
    runtime, _ = _runtime(mode)

    with pytest.raises(expected) as raised:
        await runtime.investigate(_request(repository))

    assert "fixture" not in str(raised.value)


@pytest.mark.asyncio
async def test_oversized_output_is_bounded_and_rejected(repository: Path) -> None:
    runtime, _ = _runtime("oversized", "5000")

    with pytest.raises(InvalidAgentOutputError):
        await runtime.investigate(_request(repository, max_output_bytes=1_024))


@pytest.mark.asyncio
async def test_missing_executable_reports_a_safe_startup_error(repository: Path) -> None:
    runtime = ClaudeAgentRuntime(
        ClaudeRuntimeConfiguration(
            executable=str(repository / "absent"), effort=ClaudeEffort.LOW, max_budget_usd=1.0
        )
    )

    with pytest.raises(AgentRuntimeError, match="could not start"):
        await runtime.investigate(_request(repository))


def _executable_reporting(tmp_path: Path, mode: str) -> str:
    """Write a real executable that answers `--version` like the binary would."""

    script = tmp_path / "claude-stub"
    script.write_text(
        f'#!/bin/sh\nexec {sys.executable} -I "{_FIXTURE}" {mode}\n', encoding="utf-8"
    )
    script.chmod(0o700)
    return str(script)


@pytest.mark.parametrize(
    ("mode", "match"),
    [
        ("old-version", "unsupported"),
        ("unparseable-version", "could not be determined"),
        ("version-failure", "not available"),
    ],
)
def test_version_gate_fails_closed(tmp_path: Path, mode: str, match: str) -> None:
    """The binary updates itself, so the gate is a floor rather than an exact pin."""

    with pytest.raises(AgentRuntimeError, match=match):
        verify_executable_version(_executable_reporting(tmp_path, mode), CLAUDE_MINIMUM_VERSION)


def test_version_gate_accepts_an_installed_version_at_or_above_the_floor(tmp_path: Path) -> None:
    executable = _executable_reporting(tmp_path, "version")

    assert verify_executable_version(executable, CLAUDE_MINIMUM_VERSION) == "2.1.251"


def test_version_gate_reports_a_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(AgentRuntimeError, match="not available"):
        verify_executable_version(str(tmp_path / "absent"), CLAUDE_MINIMUM_VERSION)


def test_environment_names_are_the_documented_minimum() -> None:
    """Measured on macOS: keychain access needs USER in addition to HOME."""

    from maestro.agents.claude import runtime as module  # noqa: PLC0415

    assert module._ENVIRONMENT_NAMES == ("HOME", "PATH", "USER")  # pyright: ignore[reportPrivateUsage]
    assert "ANTHROPIC_API_KEY" not in module._ENVIRONMENT_NAMES  # pyright: ignore[reportPrivateUsage]
    assert os.environ  # the adapter reads from the real environment, never a literal
