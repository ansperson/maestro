from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from maestro.config import AuditWriterSettings, Settings

_TEST_AUDIT_WRITER_HOST = "127.0.0.1"
_TEST_AUDIT_WRITER_USER = "audit_writer"


@pytest.fixture(autouse=True)
def configured_test_audit_writer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Supply the mandatory Audit and worker-selection settings to deterministic tests."""

    password_file = tmp_path.parent / f".{tmp_path.name}-audit-writer-password"
    password_file.write_text("synthetic-audit-password", encoding="utf-8")
    password_file.chmod(0o600)
    monkeypatch.setenv("MAESTRO_AUDIT_WRITER_HOST", _TEST_AUDIT_WRITER_HOST)
    monkeypatch.setenv("MAESTRO_AUDIT_WRITER_USER", _TEST_AUDIT_WRITER_USER)
    monkeypatch.setenv("MAESTRO_AUDIT_WRITER_PASSWORD_FILE", str(password_file))
    # Worker selection has no default, so a deployment always states which worker it runs.
    # Deterministic tests use a typed fake and select Codex only to satisfy the setting.
    monkeypatch.setenv("MAESTRO_AGENT_RUNTIME", "codex")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    destination = tmp_path / "repository"
    source = Path(__file__).parent / "fixtures" / "codebase"
    shutil.copytree(source, destination)
    (destination / "binary.dat").write_bytes(b"\x00\x01\x02")
    (destination / "non-utf8.txt").write_bytes(b"\xff\xfe")
    (destination / "oversized.txt").write_text("x" * 2_048, encoding="utf-8")
    return destination


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "allowed_roots": (Path.cwd(),),
            "audit_writer": AuditWriterSettings(),  # pyright: ignore[reportCallIssue]
            "max_file_bytes": 1_024,
            "max_repository_bytes": 1_048_576,
        }
        values.update(overrides)
        return Settings.model_validate(values)

    return factory
