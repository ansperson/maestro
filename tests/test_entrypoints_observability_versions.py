from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import maestro.main as main_module
import maestro.versions as versions_module
from maestro.config import Settings
from maestro.errors import AgentRuntimeError, RecursionNotAllowedError
from maestro.observability import JsonFormatter, configure_logging
from maestro.versions import RuntimeVersions, verify_runtime_versions


class _FakeServer:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def run(self, *, transport: str) -> None:
        self._calls.append(transport)


def test_json_formatter_emits_only_approved_primitive_metadata() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="maestro.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="completed",
        args=(),
        exc_info=None,
    )
    record.metadata = {
        "request_id": "abc",
        "duration_ms": 1.5,
        "ok": True,
        "nothing": None,
        "untrusted": {"question": "must not be serialized"},
    }
    payload = json.loads(formatter.format(record))
    assert payload["logger"] == "maestro.test"
    assert payload["message"] == "completed"
    assert payload["request_id"] == "abc"
    assert payload["duration_ms"] == 1.5
    assert payload["ok"] is True
    assert payload["nothing"] is None
    assert "untrusted" not in payload
    assert isinstance(payload["timestamp"], str)


def test_configure_logging_replaces_handlers_and_writes_json_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        root.addHandler(logging.NullHandler())
        configure_logging("WARNING")
        assert len(root.handlers) == 1
        assert root.level == logging.WARNING
        logging.getLogger("maestro.test").warning("safe warning")
        payload = json.loads(capsys.readouterr().err)
        assert payload["message"] == "safe warning"
        assert payload["level"] == "WARNING"
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_verify_runtime_versions_accepts_only_exact_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    installed = {
        "mcp": versions_module.MCP_SDK_VERSION,
        "openai-codex": versions_module.CODEX_SDK_VERSION,
        "openai-codex-cli-bin": versions_module.CODEX_RUNTIME_VERSION,
    }
    monkeypatch.setattr(versions_module.importlib.metadata, "version", installed.__getitem__)
    assert verify_runtime_versions() == RuntimeVersions(
        mcp_sdk="2.1.0",
        codex_sdk="0.147.0",
        codex_runtime="0.147.0",
    )

    installed["mcp"] = "99.0.0"
    with pytest.raises(AgentRuntimeError, match="unsupported"):
        verify_runtime_versions()


def test_verify_runtime_versions_maps_missing_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_distribution: str) -> str:
        raise versions_module.importlib.metadata.PackageNotFoundError("missing")

    monkeypatch.setattr(versions_module.importlib.metadata, "version", missing)
    with pytest.raises(AgentRuntimeError, match="missing"):
        verify_runtime_versions()


def test_main_builds_verified_stdio_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue] - Audit URL comes from BaseSettings
        allowed_roots=(tmp_path,)
    )
    run_calls: list[str] = []
    log_levels: list[str] = []
    server = _FakeServer(run_calls)

    def load_settings() -> Settings:
        return settings

    def fake_build(_settings: Settings) -> object:
        return object()

    def fake_create(_service: object) -> _FakeServer:
        return server

    monkeypatch.delenv("MAESTRO_VERIFIER_DEPTH", raising=False)
    monkeypatch.setattr(main_module, "Settings", load_settings)
    monkeypatch.setattr(
        main_module,
        "verify_runtime_versions",
        lambda: RuntimeVersions("2.1.0", "0.147.0", "0.147.0"),
    )
    monkeypatch.setattr(main_module, "build_service", fake_build)
    monkeypatch.setattr(main_module, "create_server", fake_create)
    monkeypatch.setattr(main_module, "configure_logging", log_levels.append)

    main_module.main()

    assert run_calls == ["stdio"]
    assert log_levels == ["INFO", "INFO"]


@pytest.mark.asyncio
async def test_production_composition_does_not_connect_or_migrate_at_build_time(
    tmp_path: Path,
) -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue] - values come from BaseSettings
        allowed_roots=(tmp_path,)
    )
    service = main_module.build_service(settings)
    await service.shutdown()


def test_main_refuses_recursive_worker_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAESTRO_VERIFIER_DEPTH", "1")
    with pytest.raises(RecursionNotAllowedError):
        main_module.main()
