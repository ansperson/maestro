from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from openai_codex import ApprovalMode, Sandbox
from pydantic import ValidationError

from maestro.agents.codex import worker
from maestro.agents.codex.protocol import CodexWorkerRequest
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Evidence,
    VerificationResult,
    VerificationStatus,
)
from maestro.model_identity import ModelIdentifier


def _result() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="The model stores a list.",
        confidence=Confidence.HIGH,
        evidence=[Evidence(path="src/models.py", line_start=1, finding="The field is a list.")],
        conflicts=[],
        reason="The source is direct evidence.",
    )


def _request(repository: Path) -> CodexWorkerRequest:
    return CodexWorkerRequest(
        repository_root=repository,
        question="Is the field a list?",
        context="Consumer background only.",
        model=ModelIdentifier("gpt-5.4"),
        max_output_bytes=8_192,
    )


@pytest.mark.parametrize(
    "model",
    [
        "postgresql://reader:fixture-password@db/maestro",  # pragma: allowlist secret
        "/private/model",
        r"C:\private\model",
        r"\\server\share\model",
        "API_KEY=fixture-secret",
        "gpt-5.4\u200b",
        "production model",
    ],
)
def test_worker_protocol_rejects_unsafe_model_identifier_families(
    repository: Path,
    model: str,
) -> None:
    payload = _request(repository).model_dump()
    payload["model"] = model

    with pytest.raises(ValidationError, match="Audit-safe"):
        CodexWorkerRequest.model_validate(payload, strict=True)


class _FakeHandle:
    def __init__(self, *, block: bool = False, final_response: str | None = "default") -> None:
        self.interrupted = False
        self.started = asyncio.Event()
        self._block = block
        self._final_response = final_response

    async def run(self) -> SimpleNamespace:
        self.started.set()
        if self._block:
            await asyncio.Event().wait()
        response = (
            _result().model_dump_json()
            if self._final_response == "default"
            else self._final_response
        )
        return SimpleNamespace(final_response=response)

    async def interrupt(self) -> None:
        self.interrupted = True


class _FakeThread:
    def __init__(self, handle: _FakeHandle) -> None:
        self.handle = handle
        self.turn_prompt: str | None = None
        self.turn_options: dict[str, object] = {}

    async def turn(self, prompt: str, **options: object) -> _FakeHandle:
        self.turn_prompt = prompt
        self.turn_options = options
        return self.handle


class _FakeCodex:
    def __init__(self, handle: _FakeHandle) -> None:
        self.thread = _FakeThread(handle)
        self.thread_options: dict[str, object] = {}
        self.login_key: str | None = None
        self.config: object | None = None

    def factory(self, config: object) -> _FakeCodex:
        self.config = config
        return self

    async def __aenter__(self) -> _FakeCodex:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _traceback: object | None,
    ) -> None:
        return None

    async def login_api_key(self, key: str) -> None:
        self.login_key = key

    async def thread_start(self, **options: object) -> _FakeThread:
        self.thread_options = options
        return self.thread


@pytest.mark.asyncio
async def test_worker_uses_one_ephemeral_read_only_deny_all_turn(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    temporary = tmp_path / "tmp"
    home = tmp_path / "home"
    for directory in (codex_home, temporary, home):
        directory.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MAESTRO_CODEX_API_KEY", "opaque-api-key")
    fake = _FakeCodex(_FakeHandle())
    monkeypatch.setattr(worker, "AsyncCodex", fake.factory)

    result = await worker.investigate(_request(repository))

    assert result.status is VerificationStatus.RESOLVED
    assert fake.login_key == "opaque-api-key"
    assert fake.thread_options["approval_mode"] is ApprovalMode.deny_all
    assert fake.thread_options["sandbox"] is Sandbox.read_only
    assert fake.thread_options["ephemeral"] is True
    assert fake.thread_options["model"] == "gpt-5.4"
    assert fake.thread_options["cwd"] == str(repository)
    assert fake.thread_options["developer_instructions"] == worker.VERIFIER_INSTRUCTIONS
    assert fake.thread.turn_options["approval_mode"] is ApprovalMode.deny_all
    assert fake.thread.turn_options["sandbox"] is Sandbox.read_only
    assert fake.thread.turn_options["output_schema"] == VerificationResult.model_json_schema(
        mode="validation"
    )
    prompt = cast(str, fake.thread.turn_prompt)
    assert "<untrusted_request_json>" in prompt
    assert "Consumer background only." in prompt

    config = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'sandbox_mode = "read-only"' in config
    assert 'approval_policy = "never"' in config
    assert 'web_search = "disabled"' in config
    assert "project_doc_max_bytes = 0" in config
    assert "apps = false" in config
    assert "multi_agent = false" in config
    assert "shell_tool = true" in config
    assert 'inherit = "none"' in config
    assert 'MAESTRO_VERIFIER_DEPTH = "1"' in config
    assert "mcp_servers" not in config


@pytest.mark.asyncio
async def test_worker_interrupts_turn_when_cancelled(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    temporary = tmp_path / "tmp"
    home = tmp_path / "home"
    for directory in (codex_home, temporary, home):
        directory.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv("HOME", str(home))
    handle = _FakeHandle(block=True)
    fake = _FakeCodex(handle)
    monkeypatch.setattr(worker, "AsyncCodex", fake.factory)

    task = asyncio.create_task(worker.investigate(_request(repository)))
    await handle.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.interrupted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("final_response", [None, "x" * 8_193])
async def test_worker_rejects_missing_or_oversized_final_response(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_response: str | None,
) -> None:
    codex_home = tmp_path / "codex"
    temporary = tmp_path / "tmp"
    home = tmp_path / "home"
    for directory in (codex_home, temporary, home):
        directory.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setenv("HOME", str(home))
    fake = _FakeCodex(_FakeHandle(final_response=final_response))
    monkeypatch.setattr(worker, "AsyncCodex", fake.factory)
    with pytest.raises(ValueError, match="missing or oversized"):
        await worker.investigate(_request(repository))


class _FakeStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "category"),
    [(ValueError("bad output"), "invalid_output"), (RuntimeError("crash"), "runtime")],
)
async def test_worker_private_protocol_maps_safe_failures(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    category: str,
) -> None:
    request = _request(repository)
    output = io.StringIO()

    async def fail(_request_value: CodexWorkerRequest) -> VerificationResult:
        raise failure

    monkeypatch.setattr(sys, "stdin", _FakeStdin(request.model_dump_json().encode()))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(worker, "investigate", fail)
    assert await worker._run() == 0  # pyright: ignore[reportPrivateUsage]
    assert json.loads(output.getvalue()) == {"kind": "failure", "category": category}


@pytest.mark.asyncio
async def test_worker_private_protocol_success(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(repository)
    output = io.StringIO()

    async def succeed(_request_value: CodexWorkerRequest) -> VerificationResult:
        return _result()

    monkeypatch.setattr(sys, "stdin", _FakeStdin(request.model_dump_json().encode()))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(worker, "investigate", succeed)
    assert await worker._run() == 0  # pyright: ignore[reportPrivateUsage]
    payload = json.loads(output.getvalue())
    assert payload["kind"] == "success"
    assert payload["result"]["status"] == "resolved"


def test_worker_main_exits_with_runner_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def run_and_close(coroutine: Coroutine[object, object, int]) -> int:
        coroutine.close()
        return 7

    monkeypatch.setattr(worker.asyncio, "run", run_and_close)
    with pytest.raises(SystemExit) as caught:
        worker.main()
    assert caught.value.code == 7
