"""Claude Code binary runtime adapter.

The binary resolves the operator's own authentication, so Maestro passes no credential,
token, or key of its own, per ADR-0010.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re

from pydantic import ValidationError

from maestro.agents.claude.protocol import ClaudeResultEnvelope
from maestro.agents.runtime import InvestigationRequest
from maestro.capabilities.resolve_codebase_fact.contracts import VerificationResult
from maestro.capabilities.resolve_codebase_fact.policy import (
    VERIFIER_INSTRUCTIONS,
    build_verifier_prompt,
)
from maestro.config import ClaudeRuntimeConfiguration
from maestro.errors import AgentRuntimeError, InvalidAgentOutputError

# resolve_codebase_fact reports repository state, so its worker receives read access only.
# A Capability that legitimately modifies a repository passes its own set (ADR-0010).
_ALLOWED_TOOLS = ("Read", "Glob", "Grep")
_DISALLOWED_TOOLS = ("Write", "Edit", "NotebookEdit", "Bash", "WebSearch", "WebFetch", "Task")

# The binary reaches the operator's credential store, which on macOS requires USER in
# addition to HOME. Nothing else is inherited.
_ENVIRONMENT_NAMES = ("HOME", "PATH", "USER")

_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_TERMINATION_GRACE_SECONDS = 5.0


class ClaudeAgentRuntime:
    """Run each investigation as one bounded, read-only Claude Code invocation."""

    def __init__(
        self,
        configuration: ClaudeRuntimeConfiguration,
        executable: str | None = None,
    ) -> None:
        self._configuration = configuration
        self._executable = executable or configuration.executable

    async def investigate(self, request: InvestigationRequest) -> VerificationResult:
        """Launch one owned invocation and validate its bounded structured response."""

        command = self._build_command(request)
        prompt = build_verifier_prompt(request.question, request.context)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=request.repository_root,
                env=self._environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                close_fds=True,
            )
        except OSError as exc:
            raise AgentRuntimeError("The configured Claude runtime could not start.") from exc
        try:
            stdout = await _communicate_bounded(
                process, prompt.encode("utf-8"), request.max_output_bytes
            )
        except _OutputLimitError as exc:
            await _terminate(process)
            raise InvalidAgentOutputError("The Claude runtime exceeded its output bound.") from exc
        except BaseException:
            await _terminate(process)
            raise
        if process.returncode != 0:
            raise AgentRuntimeError("The Claude runtime did not complete successfully.")
        return _validated_result(stdout)

    def _build_command(self, request: InvestigationRequest) -> tuple[str, ...]:
        """Build one shell-free argument vector; no value is interpolated into a string."""

        schema = json.dumps(VerificationResult.model_json_schema(mode="validation"))
        return (
            self._executable,
            "--print",
            "--output-format",
            "json",
            "--model",
            request.model.value,
            "--effort",
            self._configuration.effort.value,
            "--max-budget-usd",
            str(self._configuration.max_budget_usd),
            "--json-schema",
            schema,
            "--system-prompt",
            VERIFIER_INSTRUCTIONS,
            "--allowed-tools",
            ",".join(_ALLOWED_TOOLS),
            "--disallowed-tools",
            ",".join(_DISALLOWED_TOOLS),
        )

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            name: value
            for name in _ENVIRONMENT_NAMES
            if (value := os.environ.get(name)) is not None
        }


def _validated_result(stdout: bytes) -> VerificationResult:
    """Read only the approved envelope fields, then validate the strict semantic result."""

    try:
        envelope = ClaudeResultEnvelope.model_validate_json(stdout)
    except ValidationError as exc:
        raise InvalidAgentOutputError(
            "The Claude runtime returned an unsupported envelope."
        ) from exc
    if envelope.is_error or envelope.subtype != "success":
        raise AgentRuntimeError("The Claude runtime reported an unsuccessful investigation.")
    try:
        return VerificationResult.model_validate_json(envelope.result, strict=True)
    except ValidationError as exc:
        raise InvalidAgentOutputError("The Claude runtime returned an invalid result.") from exc


class _OutputLimitError(Exception):
    """The runtime produced more output than the configured bound."""


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
    payload: bytes,
    limit: int,
) -> bytes:
    if process.stdin is None or process.stdout is None:
        raise AgentRuntimeError("The Claude runtime did not expose its owned pipes.")
    stdin, stdout = process.stdin, process.stdout
    stdin.write(payload)
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await stdin.drain()
    stdin.close()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await stdin.wait_closed()
    data = await _read_bounded(stdout, limit)
    await process.wait()
    return data


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await stream.read(min(65_536, limit + 1 - len(data))):
        data.extend(chunk)
        if len(data) > limit:
            raise _OutputLimitError
    return bytes(data)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Terminate, then kill, then reap the owned process group without leaking a child."""

    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        process.terminate()
    try:
        async with asyncio.timeout(_TERMINATION_GRACE_SECONDS):
            await asyncio.shield(asyncio.ensure_future(process.wait()))
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            process.kill()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(asyncio.ensure_future(process.wait()))


def verify_executable_version(executable: str, minimum: tuple[int, int, int]) -> str:
    """Return the installed version, failing closed when it is absent or too old.

    The binary updates itself, so an exact pin would break on every upstream release. A
    floor keeps the startup gate meaningful while surviving routine updates.
    """

    try:
        completed = _run_version(executable)
    except OSError as exc:
        raise AgentRuntimeError("The configured Claude runtime is not available.") from exc
    match = _VERSION_PATTERN.search(completed)
    if match is None:
        raise AgentRuntimeError("The Claude runtime version could not be determined.")
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if version < minimum:
        raise AgentRuntimeError("The installed Claude runtime version is unsupported.")
    return ".".join(str(part) for part in version)


def _run_version(executable: str) -> str:
    import subprocess  # noqa: PLC0415 - startup-only probe, not on the request path

    completed = subprocess.run(  # noqa: S603 - fixed argument vector, no shell
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            name: value
            for name in _ENVIRONMENT_NAMES
            if (value := os.environ.get(name)) is not None
        },
    )
    if completed.returncode != 0:
        raise AgentRuntimeError("The configured Claude runtime is not available.")
    return completed.stdout
