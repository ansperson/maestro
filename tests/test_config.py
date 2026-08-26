from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from pydantic import ValidationError

import maestro.config as config_module
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
        field: value,
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(payload)


def test_settings_reject_incoherent_file_limits(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        Settings(  # pyright: ignore[reportCallIssue] - values come from BaseSettings
            allowed_roots=(tmp_path,),
            max_file_bytes=2_048,
            max_repository_bytes=1_024,
        )


def test_settings_reject_missing_root_and_auth_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        Settings(  # pyright: ignore[reportCallIssue] - values come from BaseSettings
            allowed_roots=(tmp_path / "missing",)
        )
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    link = tmp_path / "auth-link.json"
    link.symlink_to(auth)
    with pytest.raises(ValidationError, match="non-symlink"):
        Settings(  # pyright: ignore[reportCallIssue] - values come from BaseSettings
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
            }
        )
    with pytest.raises(ValidationError, match="regular"):
        Settings(  # pyright: ignore[reportCallIssue] - values come from BaseSettings
            allowed_roots=(tmp_path,), codex_auth_file=tmp_path
        )


def test_settings_deduplicates_roots(tmp_path: Path) -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue] - values come from BaseSettings
        allowed_roots=(tmp_path, tmp_path)
    )
    assert settings.allowed_roots == (tmp_path,)


def test_settings_rejects_platform_filesystem_anchor() -> None:
    anchor = Path(Path.cwd().anchor)
    with pytest.raises(ValidationError, match="filesystem anchors"):
        Settings.model_validate(
            {
                "allowed_roots": (anchor,),
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
        }
    )
    assert configured.allowed_roots == (root.resolve(),)


def test_settings_requires_writer_user_and_password_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAESTRO_AUDIT_WRITER_USER")
    monkeypatch.delenv("MAESTRO_AUDIT_WRITER_PASSWORD_FILE")
    with pytest.raises(ValidationError, match=r"user|password_file"):
        Settings(  # pyright: ignore[reportCallIssue] - intentionally missing writer fields
            allowed_roots=(tmp_path,)
        )


def test_legacy_or_indirect_conninfo_cannot_bypass_writer_password_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAESTRO_AUDIT_WRITER_PASSWORD_FILE")
    monkeypatch.setenv(
        "MAESTRO_AUDIT_DATABASE_URL",
        "service=private-service password=private-value passfile=/private/password",
    )
    with pytest.raises(ValidationError, match="password_file") as error:
        Settings(allowed_roots=(tmp_path,))  # pyright: ignore[reportCallIssue]
    rendered = str(error.value)
    assert "private-service" not in rendered
    assert "private-value" not in rendered
    assert "/private/password" not in rendered


@pytest.mark.parametrize(
    "unsafe_host",
    [
        "postgresql://audit_writer@database/maestro",
        "service=private-service",
        "host=database passfile=/private/password",
        "/var/run/postgresql",
        "database\npassword=private-value",
    ],
)
def test_writer_projection_rejects_conninfo_and_service_indirection(
    tmp_path: Path, unsafe_host: str
) -> None:
    with pytest.raises(ValidationError, match="host is invalid") as error:
        config_module.AuditWriterSettings(
            host=unsafe_host,
            user="audit_writer",
            password_file=_password_file(tmp_path),
        )
    assert unsafe_host not in str(error.value)


@pytest.mark.parametrize("indirect_field", ["password", "passfile", "service", "dsn"])
def test_role_settings_forbid_non_file_credential_inputs(
    tmp_path: Path, indirect_field: str
) -> None:
    payload: dict[str, object] = {
        "user": "audit_writer",
        "password_file": _password_file(tmp_path),
        indirect_field: "private-value",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted") as error:
        config_module.AuditWriterSettings.model_validate(payload)
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "unavailable"),
        ("symlink", "non-symlink"),
        ("directory", "regular"),
        ("empty", "must not be empty"),
        ("oversized", "oversized"),
        ("insecure", "owner-only"),
    ],
)
def test_writer_secret_file_rejects_unsafe_inputs(tmp_path: Path, case: str, message: str) -> None:
    path = tmp_path / "writer-password"
    if case == "symlink":
        target = _password_file(tmp_path)
        path.symlink_to(target)
    elif case == "directory":
        path.mkdir()
    elif case == "missing":
        pass
    else:
        path.write_text("" if case == "empty" else "x" * 4_097, encoding="utf-8")
        path.chmod(0o644 if case == "insecure" else 0o600)

    with pytest.raises(ValidationError, match=message) as error:
        config_module.AuditWriterSettings(user="audit_writer", password_file=path)
    assert str(path) not in str(error.value)


def test_writer_secret_file_rejects_unreadable_input_without_path_or_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _password_file(tmp_path, "private-writer-value")
    original_open = config_module.os.open

    def deny_open(candidate: Path, flags: int) -> int:
        if Path(candidate) == path:
            raise PermissionError("private driver detail")
        return original_open(candidate, flags)

    monkeypatch.setattr(config_module.os, "open", deny_open)
    with pytest.raises(ValidationError, match="unavailable") as error:
        config_module.AuditWriterSettings(user="audit_writer", password_file=path)
    rendered = str(error.value)
    assert str(path) not in rendered
    assert "private-writer-value" not in rendered
    assert "private driver detail" not in rendered


def test_writer_secret_file_rejects_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("ownership is a POSIX credential control")
    path = _password_file(tmp_path)
    real_fstat = config_module.os.fstat

    def wrong_owner(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        values = list(metadata)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(config_module.os, "fstat", wrong_owner)
    with pytest.raises(ValidationError, match="owned by"):
        config_module.AuditWriterSettings(user="audit_writer", password_file=path)


def test_writer_projection_revalidates_secret_file_after_settings_construction(
    tmp_path: Path,
) -> None:
    path = _password_file(tmp_path)
    settings = config_module.AuditWriterSettings(user="audit_writer", password_file=path)
    replacement = tmp_path / "replacement-password"
    replacement.write_text("replacement-value", encoding="utf-8")
    replacement.chmod(0o600)
    path.unlink()
    path.symlink_to(replacement)

    with pytest.raises(ValueError, match="non-symlink"):
        settings.connection_configuration()


def test_writer_projection_suppresses_secret_decoder_diagnostics(tmp_path: Path) -> None:
    path = _password_file(tmp_path)
    settings = config_module.AuditWriterSettings(user="audit_writer", password_file=path)
    path.write_bytes(b"\xffprivate-writer-value")

    with pytest.raises(ValueError, match="UTF-8") as error:
        settings.connection_configuration()

    rendered = str(error.value)
    assert "private-writer-value" not in rendered
    assert str(path) not in rendered
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_writer_secret_file_accepts_owner_only_modes_and_one_trailing_newline(
    tmp_path: Path, mode: int
) -> None:
    path = _password_file(tmp_path, "synthetic-password\n")
    path.chmod(mode)
    configuration = config_module.AuditWriterSettings(
        host="audit-postgres",
        database="maestro_audit",
        user="audit_writer",
        password_file=path,
    ).connection_configuration()
    assert configuration.password.get_secret_value() == "synthetic-password"
    assert str(path) not in repr(configuration)


@pytest.mark.parametrize(
    ("settings_type", "configuration_type"),
    [
        (config_module.AuditBootstrapSettings, config_module.AuditBootstrapConfiguration),
        (config_module.AuditMigrationSettings, config_module.AuditMigrationConfiguration),
        (config_module.AuditWriterSettings, config_module.AuditWriterConfiguration),
        (config_module.AuditReaderSettings, config_module.AuditReaderConfiguration),
    ],
)
def test_database_roles_have_distinct_typed_credential_projections(
    tmp_path: Path,
    settings_type: type[
        config_module.AuditBootstrapSettings
        | config_module.AuditMigrationSettings
        | config_module.AuditWriterSettings
        | config_module.AuditReaderSettings
    ],
    configuration_type: type[
        config_module.AuditBootstrapConfiguration
        | config_module.AuditMigrationConfiguration
        | config_module.AuditWriterConfiguration
        | config_module.AuditReaderConfiguration
    ],
) -> None:
    role = settings_type(user="role_user", password_file=_password_file(tmp_path))
    assert isinstance(role.connection_configuration(), configuration_type)


def test_application_projections_separate_codex_and_audit_values(tmp_path: Path) -> None:
    password_path = _password_file(tmp_path, "writer-only-value")
    allowed_root = tmp_path / "repository"
    allowed_root.mkdir()
    settings = Settings.model_validate(
        {
            "allowed_roots": (allowed_root,),
            "codex_api_key": "codex-only-value",  # pragma: allowlist secret
            "audit_writer": {
                "host": "audit-postgres",
                "database": "maestro_audit",
                "user": "audit_writer",
                "password_file": password_path,
            },
        }
    )

    codex = settings.codex_runtime_configuration()
    writer = settings.audit_writer_configuration()
    assert isinstance(codex, config_module.CodexRuntimeConfiguration)
    assert codex.api_key is not None
    assert codex.api_key.get_secret_value() == "codex-only-value"
    assert not hasattr(codex, "password")
    assert writer.password.get_secret_value() == "writer-only-value"
    assert not hasattr(writer, "api_key")
    assert str(password_path) not in repr(settings)
    assert "audit_writer" not in settings.model_dump()


def test_settings_rejects_writer_password_below_an_allowed_root(tmp_path: Path) -> None:
    password_path = _password_file(tmp_path)
    with pytest.raises(ValidationError, match="outside every allowed root") as error:
        Settings.model_validate(
            {
                "allowed_roots": (tmp_path,),
                "audit_writer": {
                    "user": "audit_writer",
                    "password_file": password_path,
                },
            }
        )
    assert str(password_path) not in str(error.value)


_UNSAFE_MODEL_IDENTIFIERS = (
    "postgresql://reader:fixture-password@db/maestro",  # pragma: allowlist secret
    "/Users/alice/.config/model",
    r"C:\Users\alice\model",
    r"\\server\share\model",
    "gpt-5.4\nAPI_KEY=fixture-secret",
    "gpt-5.4\u200b",
    "API_KEY=fixture-secret",
    "the current production model",
)


@pytest.mark.parametrize("model", _UNSAFE_MODEL_IDENTIFIERS)
def test_settings_rejects_unsafe_model_identifier_families_before_startup(
    tmp_path: Path,
    model: str,
) -> None:
    with pytest.raises(ValidationError, match="Audit-safe") as error:
        Settings.model_validate(
            {
                "allowed_roots": (tmp_path,),
                "codex_model": model,
            }
        )
    assert model not in str(error.value)


@pytest.mark.parametrize(
    "model",
    [
        "gpt-5.4",
        "o3",
        "codex-mini-latest",
        "gpt-5.4-2026-08-01",
        "m" * 128,
    ],
)
def test_settings_retains_supported_safe_model_identifiers(tmp_path: Path, model: str) -> None:
    settings = Settings.model_validate(
        {
            "allowed_roots": (tmp_path,),
            "codex_model": model,
        }
    )

    assert settings.codex_model.value == model


def test_settings_rejects_overlong_model_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Audit-safe"):
        Settings.model_validate(
            {
                "allowed_roots": (tmp_path,),
                "codex_model": "m" * 129,
            }
        )


def _password_file(tmp_path: Path, value: str = "synthetic-password") -> Path:
    path = tmp_path / "audit-password"
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path
