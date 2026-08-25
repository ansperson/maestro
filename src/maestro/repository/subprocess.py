"""Owned bounded subprocess lifecycle for repository inspection."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_READ_CHUNK_BYTES = 65_536
_TERMINATE_GRACE_SECONDS = 0.25


class ProcessOutputLimitError(Exception):
    """The child exceeded its protocol stdout allowance."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded output and status from a fully reaped process."""

    stdout: bytes
    returncode: int


async def run_owned_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    input_data: bytes,
    max_stdout_bytes: int,
) -> ProcessResult:
    """Run one child and terminate/kill/reap it on failures after handle acquisition."""

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        close_fds=True,
        start_new_session=os.name == "posix",
    )
    try:
        stdin, stdout_stream = _require_pipes(process)
        stdin.write(input_data)
        await stdin.drain()
        stdin.close()
        stdout = await _read_bounded(stdout_stream, max_stdout_bytes)
        returncode = await process.wait()
    except BaseException:
        await _cleanup_uninterruptibly(process)
        raise
    return ProcessResult(stdout=stdout, returncode=returncode)


def _require_pipes(
    process: asyncio.subprocess.Process,
) -> tuple[asyncio.StreamWriter, asyncio.StreamReader]:
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("repository child pipes were not created")
    return process.stdin, process.stdout


async def _read_bounded(stream: asyncio.StreamReader, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        size += len(chunk)
        if size > maximum:
            raise ProcessOutputLimitError
        chunks.append(chunk)
    return b"".join(chunks)


async def _cleanup_uninterruptibly(process: asyncio.subprocess.Process) -> None:
    cleanup = asyncio.create_task(_terminate_and_reap(process))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    await cleanup


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    _signal_process(process, signal.SIGTERM)
    try:
        async with asyncio.timeout(_TERMINATE_GRACE_SECONDS):
            await process.wait()
            return
    except TimeoutError:
        _signal_process(process, signal.SIGKILL)
    await process.wait()


def _signal_process(process: asyncio.subprocess.Process, selected: signal.Signals) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, selected)
        elif selected is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        pass
