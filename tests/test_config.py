from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
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
    payload: dict[str, object] = {
        "allowed_roots": (tmp_path,),
        "audit_database_url": "postgresql://audit-writer@localhost/maestro",
        field: value,
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(payload)


def test_settings_reject_incoherent_file_limits(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        Settings(  # pyright: ignore[reportCallIssue] - Audit URL comes from BaseSettings
            allowed_roots=(tmp_path,),
            max_file_bytes=2_048,
            max_repository_bytes=1_024,
        )


def test_settings_reject_missing_root_and_auth_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Settings(  # pyright: ignore[reportCallIssue] - Audit URL comes from BaseSettings
            allowed_roots=(tmp_path / "missing",)
        )
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    link = tmp_path / "auth-link.json"
    link.symlink_to(auth)
    with pytest.raises(ValidationError, match="non-symlink"):
        Settings(  # pyright: ignore[reportCallIssue] - Audit URL comes from BaseSettings
            allowed_roots=(tmp_path,), codex_auth_file=link
        )


def test_settings_reject_two_auth_sources(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="only one"):
        Settings.model_validate(
            {
                "allowed_roots": (tmp_path,),
                "codex_auth_file": auth,
                "codex_api_key": "secret",
                "audit_database_url": "postgresql://audit-writer@localhost/maestro",
            }
        )


def test_settings_reject_empty_roots_and_non_file_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAESTRO_ALLOWED_ROOTS", os.pathsep)
    with pytest.raises(ValidationError, match="at least one"):
        Settings()  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError, match="at least one"):
        Settings.model_validate(
            {
                "allowed_roots": (),
                "audit_database_url": "postgresql://audit-writer@localhost/maestro",
            }
        )
    with pytest.raises(ValidationError, match="regular"):
        Settings(  # pyright: ignore[reportCallIssue] - Audit URL comes from BaseSettings
            allowed_roots=(tmp_path,), codex_auth_file=tmp_path
        )


def test_settings_deduplicates_roots(tmp_path: Path) -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue] - Audit URL comes from BaseSettings
        allowed_roots=(tmp_path, tmp_path)
    )
    assert settings.allowed_roots == (tmp_path,)


def test_settings_rejects_platform_filesystem_anchor() -> None:
    anchor = Path(Path.cwd().anchor)
    with pytest.raises(ValidationError, match="filesystem anchors"):
        Settings.model_validate(
            {
                "allowed_roots": (anchor,),
                "audit_database_url": "postgresql://audit-writer@localhost/maestro",
            }
        )


@given(redundant_segments=st.integers(min_value=0, max_value=8))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_settings_rejects_every_canonical_anchor_alias(redundant_segments: int) -> None:
    anchor = Path(Path.cwd().anchor)
    candidate = f"{anchor}{f'.{os.sep}' * redundant_segments}"
    with pytest.raises(ValidationError, match="filesystem anchors"):
        Settings.model_validate(
            {
                "allowed_roots": (candidate,),
                "audit_database_url": "postgresql://audit-writer@localhost/maestro",
            }
        )


@given(parts=st.lists(st.from_regex(r"[a-z]{1,8}", fullmatch=True), min_size=1, max_size=4))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_settings_accepts_canonical_non_anchor_roots(tmp_path: Path, parts: list[str]) -> None:
    root = tmp_path.joinpath(*parts)
    root.mkdir(parents=True, exist_ok=True)
    configured = Settings.model_validate(
        {
            "allowed_roots": (root,),
            "audit_database_url": "postgresql://audit-writer@localhost/maestro",
        }
    )
    assert configured.allowed_roots == (root.resolve(),)


def test_settings_requires_audit_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAESTRO_AUDIT_DATABASE_URL")
    with pytest.raises(ValidationError, match="audit_database_url"):
        Settings(  # pyright: ignore[reportCallIssue] - intentionally missing Audit URL
            allowed_roots=(tmp_path,)
        )


def test_settings_rejects_malformed_audit_url_without_reflecting_credentials(
    tmp_path: Path,
) -> None:
    private_value = "audit-private-" + "credential"
    malformed = f"postgresql://audit-writer:{private_value}@["

    with pytest.raises(ValidationError, match="MAESTRO_AUDIT_DATABASE_URL is invalid") as error:
        Settings.model_validate(
            {
                "allowed_roots": (tmp_path,),
                "audit_database_url": malformed,
            }
        )

    assert private_value not in str(error.value)
