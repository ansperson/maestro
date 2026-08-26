from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from maestro.agents import InvestigationRequest
from maestro.agents.codex.runtime import CodexAgentRuntime
from maestro.capabilities.resolve_codebase_fact.contracts import VerificationStatus
from maestro.config import CodexRuntimeConfiguration, Settings
from maestro.errors import AgentRuntimeError, InvalidAgentOutputError

SettingsFactory = Callable[..., Settings]


def _path_exists(path: Path) -> bool:
    return path.exists()


def _request(repository: Path, max_output_bytes: int = 8_192) -> InvestigationRequest:
    return InvestigationRequest(
        repository_root=repository,
        question="Is Order.payments a list?",
        context=None,
        repository_fingerprint="fixture-fingerprint",
        model="gpt-5.4",  # pyright: ignore[reportArgumentType]
        max_output_bytes=max_output_bytes,
    )


def _command(mode: str, report: Path | None = None) -> tuple[str, ...]:
    helper = Path(__file__).parent / "helpers" / "fake_codex_worker.py"
    values = [sys.executable, str(helper), mode]
    if report is not None:
        values.append(str(report))
    return tuple(values)


@pytest.mark.asyncio
async def test_runtime_uses_minimal_environment_and_cleans_temporary_state(
    repository: Path,
    settings_factory: SettingsFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "worker-report.json"
    monkeypatch.setenv("MAESTRO_TEST_UNRELATED_SECRET", "must-not-cross-boundary")
    runtime = CodexAgentRuntime(
        _runtime_config(settings_factory, repository),
        worker_command=_command("success", report_path),
    )
    result = await runtime.investigate(_request(repository))
    report = cast(dict[str, object], json.loads(report_path.read_text(encoding="utf-8")))

    assert result.status is VerificationStatus.RESOLVED
    assert report["unrelated_secret_inherited"] is False
    assert report["api_key_present"] is False
    assert (report["auth_present"], report["audit_environment_names"]) == (False, [])
    assert report["depth"] == "1"
    assert not _path_exists(Path(cast(str, report["temporary_root"])))


@pytest.mark.asyncio
async def test_runtime_copies_only_explicit_authentication_source(
    repository: Path,
    settings_factory: SettingsFactory,
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("opaque-test-auth", encoding="utf-8")
    report_path = tmp_path / "auth-report.json"
    runtime = CodexAgentRuntime(
        _runtime_config(settings_factory, repository, codex_auth_file=auth),
        worker_command=_command("success", report_path),
    )
    await runtime.investigate(_request(repository))
    report = cast(dict[str, object], json.loads(report_path.read_text(encoding="utf-8")))
    assert report["auth_present"] is True
    assert report["auth_contents"] == "opaque-test-auth"
    assert report["api_key_present"] is False

    api_report_path = tmp_path / "api-report.json"
    api_runtime = CodexAgentRuntime(
        settings_factory(
            allowed_roots=(repository,),
            codex_api_key="opaque-api-key",
        ).codex_runtime_configuration(),
        worker_command=_command("success", api_report_path),
    )
    await api_runtime.investigate(_request(repository))
    api_report = cast(dict[str, object], json.loads(api_report_path.read_text(encoding="utf-8")))
    assert api_report["api_key_present"] is True
    assert api_report["auth_present"] is False


@pytest.mark.asyncio
async def test_runtime_excludes_audit_values_and_inheritable_descriptors(
    repository: Path,
    settings_factory: SettingsFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path = tmp_path / "audit-boundary-report.json"
    monkeypatch.setenv("PGPASSWORD", "must-not-cross-boundary")
    monkeypatch.setenv("PGPASSFILE", "/must/not/cross")
    settings = settings_factory(allowed_roots=(repository,))
    writer = settings.audit_writer_configuration()
    inherited_descriptor, peer_descriptor = os.pipe()
    os.set_inheritable(inherited_descriptor, True)
    runtime = CodexAgentRuntime(
        settings.codex_runtime_configuration(),
        worker_command=_command("success", report_path),
    )
    try:
        await runtime.investigate(_request(repository))
    finally:
        os.close(inherited_descriptor)
        os.close(peer_descriptor)
    report = cast(dict[str, object], json.loads(report_path.read_text(encoding="utf-8")))

    assert report["audit_environment_names"] == []
    assert inherited_descriptor not in cast(list[int], report["open_fds"])
    worker_boundary = json.dumps(
        {
            "argv": report["argv"],
            "request": report["request"],
            "config": report["config"],
            "environment_names": report["environment_names"],
        },
        sort_keys=True,
    )
    assert settings.audit_writer.password_file.as_posix() not in worker_boundary
    assert writer.password.get_secret_value() not in worker_boundary
    assert "PGPASSWORD" not in worker_boundary
    assert "PGPASSFILE" not in worker_boundary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "error_type"),
    [
        ("malformed", InvalidAgentOutputError),
        ("oversized", InvalidAgentOutputError),
        ("stderr-oversized", InvalidAgentOutputError),
        ("runtime-failure", AgentRuntimeError),
        ("invalid-output", InvalidAgentOutputError),
        ("exit-nonzero", AgentRuntimeError),
    ],
)
async def test_runtime_maps_worker_failures(
    repository: Path,
    settings_factory: SettingsFactory,
    mode: str,
    error_type: type[Exception],
) -> None:
    runtime = CodexAgentRuntime(
        settings_factory(allowed_roots=(repository,)).codex_runtime_configuration(),
        worker_command=_command(mode),
    )
    with pytest.raises(error_type):
        await runtime.investigate(_request(repository, max_output_bytes=1_024))


@pytest.mark.asyncio
async def test_runtime_missing_worker_is_typed_failure(
    repository: Path, settings_factory: SettingsFactory, tmp_path: Path
) -> None:
    runtime = CodexAgentRuntime(
        settings_factory(allowed_roots=(repository,)).codex_runtime_configuration(),
        worker_command=(str(tmp_path / "missing-worker"),),
    )
    with pytest.raises(AgentRuntimeError, match="could not start"):
        await runtime.investigate(_request(repository))


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["missing", "directory"])
async def test_runtime_fails_if_validated_auth_source_changes_before_copy(
    repository: Path,
    settings_factory: SettingsFactory,
    tmp_path: Path,
    replacement: str,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("opaque-test-auth", encoding="utf-8")
    settings = settings_factory(allowed_roots=(repository,), codex_auth_file=auth)
    auth.unlink()
    if replacement == "directory":
        auth.mkdir()
    runtime = CodexAgentRuntime(
        settings.codex_runtime_configuration(), worker_command=_command("success")
    )
    with pytest.raises(AgentRuntimeError, match="authentication source"):
        await runtime.investigate(_request(repository))


@pytest.mark.asyncio
async def test_runtime_cancellation_kills_process_group_and_cleans_temp(
    repository: Path, settings_factory: SettingsFactory, tmp_path: Path
) -> None:
    report_path = tmp_path / "sleep-report.json"
    runtime = CodexAgentRuntime(
        settings_factory(allowed_roots=(repository,)).codex_runtime_configuration(),
        worker_command=_command("sleep", report_path),
    )
    task = asyncio.create_task(runtime.investigate(_request(repository)))
    for _ in range(200):
        if report_path.exists():
            break
        await asyncio.sleep(0.01)
    assert report_path.exists()
    report = cast(dict[str, object], json.loads(report_path.read_text(encoding="utf-8")))
    pid = cast(int, report["pid"])
    temporary_root = Path(cast(str, report["temporary_root"]))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _path_exists(temporary_root)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_runtime_cancellation_escalates_when_worker_ignores_termination(
    repository: Path, settings_factory: SettingsFactory, tmp_path: Path
) -> None:
    report_path = tmp_path / "ignore-term-report.json"
    runtime = CodexAgentRuntime(
        settings_factory(allowed_roots=(repository,)).codex_runtime_configuration(),
        worker_command=_command("ignore-term", report_path),
    )
    task = asyncio.create_task(runtime.investigate(_request(repository)))
    for _ in range(200):
        if report_path.exists():
            break
        await asyncio.sleep(0.01)
    assert report_path.exists()
    report = cast(dict[str, object], json.loads(report_path.read_text(encoding="utf-8")))
    pid = cast(int, report["pid"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _runtime_config(
    settings_factory: SettingsFactory, repository: Path, **overrides: object
) -> CodexRuntimeConfiguration:
    return settings_factory(allowed_roots=(repository,), **overrides).codex_runtime_configuration()
