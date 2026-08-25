from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from maestro.config import Settings

_TEST_AUDIT_DATABASE_URL = "postgresql://audit-writer@localhost/maestro"


@pytest.fixture(autouse=True)
def configured_test_audit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply non-secret typed Audit configuration to deterministic tests."""

    monkeypatch.setenv("MAESTRO_AUDIT_DATABASE_URL", _TEST_AUDIT_DATABASE_URL)


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
            "audit_database_url": _TEST_AUDIT_DATABASE_URL,
            "max_file_bytes": 1_024,
            "max_repository_bytes": 1_048_576,
        }
        values.update(overrides)
        return Settings.model_validate(values)

    return factory
