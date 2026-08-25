from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from maestro.config import Settings


def test_settings_parse_and_canonicalize_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setenv("MAESTRO_ALLOWED_ROOTS", f"{tmp_path}{os.pathsep}{second}")
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.allowed_roots == (tmp_path.resolve(), second.resolve())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_concurrency", 0),
        ("max_queue_size", -1),
        ("verifier_timeout_seconds", 0),
        ("log_level", "verbose"),
        ("codex_model", "bad model"),
    ],
)
def test_settings_reject_invalid_values(tmp_path: Path, field: str, value: object) -> None:
    payload: dict[str, object] = {"allowed_roots": (tmp_path,), field: value}
    with pytest.raises(ValidationError):
        Settings.model_validate(payload)


def test_settings_reject_incoherent_file_limits(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        Settings(
            allowed_roots=(tmp_path,),
            max_file_bytes=2_048,
            max_repository_bytes=1_024,
        )


def test_settings_reject_missing_root_and_auth_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Settings(allowed_roots=(tmp_path / "missing",))
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    link = tmp_path / "auth-link.json"
    link.symlink_to(auth)
    with pytest.raises(ValidationError, match="non-symlink"):
        Settings(allowed_roots=(tmp_path,), codex_auth_file=link)


def test_settings_reject_two_auth_sources(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="only one"):
        Settings.model_validate(
            {
                "allowed_roots": (tmp_path,),
                "codex_auth_file": auth,
                "codex_api_key": "secret",
            }
        )


def test_settings_reject_empty_roots_and_non_file_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_ALLOWED_ROOTS", os.pathsep)
    with pytest.raises(ValidationError, match="at least one"):
        Settings()  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError, match="at least one"):
        Settings.model_validate({"allowed_roots": ()})
    with pytest.raises(ValidationError, match="regular"):
        Settings(allowed_roots=(tmp_path,), codex_auth_file=tmp_path)


def test_settings_deduplicates_roots(tmp_path: Path) -> None:
    settings = Settings(allowed_roots=(tmp_path, tmp_path))
    assert settings.allowed_roots == (tmp_path,)
