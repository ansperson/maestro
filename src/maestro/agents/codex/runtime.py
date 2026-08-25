"""Isolated subprocess adapter around the official asynchronous Codex SDK."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from maestro.agents.codex.protocol import (
    CodexWorkerFailure,
    CodexWorkerRequest,
    CodexWorkerSuccess,
)
from maestro.agents.runtime import InvestigationRequest
from maestro.capabilities.resolve_codebase_fact.contracts import VerificationResult
from maestro.config import Settings
from maestro.errors import AgentRuntimeError, InvalidAgentOutputError

_WORKER_RESPONSE: TypeAdapter[CodexWorkerSuccess | CodexWorkerFailure] = TypeAdapter(
    CodexWorkerSuccess | CodexWorkerFailure
)


class CodexAgentRuntime:
    """Run each official-SDK investigation in a minimal isolated process."""

    def __init__(self, settings: Settings, worker_command: tuple[str, ...] | None = None) -> None:
        self._settings = settings
        self._worker_command = worker_command or (
            sys.executable,
            "-m",
            "maestro.agents.codex.worker",
        )

    async def investigate(self, request: InvestigationRequest) -> VerificationResult:
        """Launch one owned worker and validate its bounded response."""

        temporary_root = Path(tempfile.mkdtemp(prefix="maestro-codex-"))
        await asyncio.to_thread(temporary_root.chmod, 0o700)
        try:
            environment = await asyncio.to_thread(self._prepare_environment, temporary_root)
            payload = CodexWorkerRequest(
                repository_root=request.repository_root,
                question=request.question,
                context=request.context,
                model=request.model,
                max_output_bytes=request.max_output_bytes,
            ).model_dump_json()
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._worker_command,
                    cwd=request.repository_root,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix",
                )
            except OSError as exc:
                raise AgentRuntimeError("The configured Codex worker could not start.") from exc
            try:
                stdout = await _communicate_bounded(
                    process,
                    payload.encode("utf-8"),
                    request.max_output_bytes,
                )
            except _WorkerOutputLimitError as exc:
                await _terminate_process(process)
                raise InvalidAgentOutputError(
                    "The verifier process output exceeded its byte limit."
                ) from exc
            except BaseException:
                await _terminate_process(process)
                raise
            if process.returncode != 0:
                raise AgentRuntimeError
            try:
                response = _WORKER_RESPONSE.validate_json(stdout, strict=True)
            except ValidationError as exc:
                raise InvalidAgentOutputError from exc
            if isinstance(response, CodexWorkerFailure):
                if response.category == "invalid_output":
                    raise InvalidAgentOutputError
                raise AgentRuntimeError
            return response.result
        finally:
            await asyncio.to_thread(shutil.rmtree, temporary_root, True)

    def _prepare_environment(self, temporary_root: Path) -> dict[str, str]:
        codex_home = temporary_root / "codex-home"
        worker_home = temporary_root / "home"
        worker_tmp = temporary_root / "tmp"
        for directory in (codex_home, worker_home, worker_tmp):
            directory.mkdir(mode=0o700)
        if self._settings.codex_auth_file is not None:
            _copy_auth_file(self._settings.codex_auth_file, codex_home / "auth.json")
        environment = {
            "CODEX_HOME": str(codex_home),
            "HOME": str(worker_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "MAESTRO_VERIFIER_DEPTH": "1",
            "NO_COLOR": "1",
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "RUST_LOG": "warn",
            "TMPDIR": str(worker_tmp),
        }
        if self._settings.codex_api_key is not None:
            environment["MAESTRO_CODEX_API_KEY"] = self._settings.codex_api_key.get_secret_value()
        return environment


class _WorkerOutputLimitError(Exception):
    """Private signal used to terminate a worker whose pipes exceed their caps."""


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
    payload: bytes,
    limit: int,
) -> bytes:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise AgentRuntimeError("The verifier process pipes are unavailable.")
    process.stdin.write(payload)
    with suppress(BrokenPipeError, ConnectionResetError):
        await process.stdin.drain()
    process.stdin.close()

    stdout_task = asyncio.create_task(_read_bounded(process.stdout, limit))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, limit))
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        stdout, _stderr, _return_code = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return stdout


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(min(65_536, limit + 1)):
        size += len(chunk)
        if size > limit:
            raise _WorkerOutputLimitError
        chunks.append(chunk)
    return b"".join(chunks)


def _copy_auth_file(source: Path, destination: Path) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise AgentRuntimeError(
            "The configured Codex authentication source is unavailable."
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise AgentRuntimeError("The configured Codex authentication source is not regular.")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            while data := os.read(source_fd, 65_536):
                remaining = memoryview(data)
                while remaining:
                    remaining = remaining[os.write(destination_fd, remaining) :]
        finally:
            os.close(destination_fd)
    except OSError as exc:
        raise AgentRuntimeError(
            "The configured Codex authentication source is unavailable."
        ) from exc
    finally:
        os.close(source_fd)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        async with asyncio.timeout(2):
            await process.wait()
    except (ProcessLookupError, TimeoutError):
        if process.returncode is None:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
