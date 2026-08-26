"""Central, fail-fast application configuration."""

from __future__ import annotations

import os
import re
import stat
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from maestro.capabilities.resolve_codebase_fact.contracts import (
    MAX_CONTEXT_CHARS,
    MAX_EVIDENCE_ITEMS,
    MAX_QUESTION_CHARS,
)
from maestro.model_identity import ModelIdentifier

_MAX_AUDIT_PASSWORD_BYTES = 4_096
_MAX_POSTGRES_HOST_CHARS = 253
_POSTGRES_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,62}\Z")
_DNS_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class CodexRuntimeConfiguration(BaseModel):
    """The complete configuration projection permitted to reach the Codex adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    auth_file: Path | None = None
    api_key: SecretStr | None = None


class _AuditConnectionConfiguration(BaseModel):
    """Validated connection values for one explicitly scoped PostgreSQL role."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    host: str
    port: Annotated[int, Field(ge=1, le=65_535)]
    database: str
    user: str
    password: SecretStr = Field(repr=False)

    @field_validator("host")
    @classmethod
    def validate_host(_cls, value: str) -> str:  # noqa: N804
        return _validate_audit_postgres_host(value)

    @field_validator("database", "user")
    @classmethod
    def validate_identifier(_cls, value: str) -> str:  # noqa: N804
        return _validate_audit_postgres_identifier(value)


class AuditBootstrapConfiguration(_AuditConnectionConfiguration):
    """Administrative bootstrap-role connection projection."""


class AuditMigrationConfiguration(_AuditConnectionConfiguration):
    """Schema-owner migration-role connection projection."""


class AuditWriterConfiguration(_AuditConnectionConfiguration):
    """Minimal append-writer connection projection used by Maestro runtime."""


class AuditReaderConfiguration(_AuditConnectionConfiguration):
    """SELECT-only human/query reader connection projection."""


class _AuditRoleSettings(BaseSettings):
    """Shared validation for one role-specific Audit credential source."""

    model_config = SettingsConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        case_sensitive=False,
    )

    host: Annotated[str, Field(min_length=1, max_length=253)] = "localhost"
    port: Annotated[int, Field(ge=1, le=65_535)] = 5432
    database: Annotated[str, Field(min_length=1, max_length=63)] = "maestro"
    user: Annotated[str, Field(min_length=1, max_length=63)]
    password_file: Path = Field(exclude=True, repr=False)

    @field_validator("host")
    @classmethod
    def validate_host(_cls, value: str) -> str:  # noqa: N804
        """Accept bounded DNS names and IP literals without URI or connection-string syntax."""

        return _validate_audit_postgres_host(value)

    @field_validator("database", "user")
    @classmethod
    def validate_identifier(_cls, value: str) -> str:  # noqa: N804
        """Keep role/database values bounded and free of connection-string grammar."""

        return _validate_audit_postgres_identifier(value)

    @field_validator("password_file")
    @classmethod
    def validate_password_file(_cls, value: Path) -> Path:  # noqa: N804
        """Validate and read the owner-only regular file once during settings construction."""

        canonical = _canonical_secret_file(value)
        _read_audit_password(canonical)
        return canonical

    def _connection_values(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": _read_audit_password(self.password_file),
        }


class AuditBootstrapSettings(_AuditRoleSettings):
    """Environment-backed bootstrap credentials, unavailable to normal runtime."""

    model_config = SettingsConfigDict(
        env_prefix="MAESTRO_AUDIT_BOOTSTRAP_",
        extra="forbid",
        hide_input_in_errors=True,
        case_sensitive=False,
    )

    def connection_configuration(self) -> AuditBootstrapConfiguration:
        return AuditBootstrapConfiguration.model_validate(self._connection_values())


class AuditMigrationSettings(_AuditRoleSettings):
    """Environment-backed migration-owner credentials, unavailable to normal runtime."""

    model_config = SettingsConfigDict(
        env_prefix="MAESTRO_AUDIT_MIGRATION_",
        extra="forbid",
        hide_input_in_errors=True,
        case_sensitive=False,
    )

    def connection_configuration(self) -> AuditMigrationConfiguration:
        return AuditMigrationConfiguration.model_validate(self._connection_values())


class AuditWriterSettings(_AuditRoleSettings):
    """Environment-backed append-writer credentials used by normal Maestro runtime."""

    model_config = SettingsConfigDict(
        env_prefix="MAESTRO_AUDIT_WRITER_",
        extra="forbid",
        hide_input_in_errors=True,
        case_sensitive=False,
    )

    def connection_configuration(self) -> AuditWriterConfiguration:
        return AuditWriterConfiguration.model_validate(self._connection_values())


class AuditReaderSettings(_AuditRoleSettings):
    """Environment-backed SELECT-only query credentials, unavailable to normal runtime."""

    model_config = SettingsConfigDict(
        env_prefix="MAESTRO_AUDIT_READER_",
        extra="forbid",
        hide_input_in_errors=True,
        case_sensitive=False,
    )

    def connection_configuration(self) -> AuditReaderConfiguration:
        return AuditReaderConfiguration.model_validate(self._connection_values())


def _load_audit_writer_settings() -> AuditWriterSettings:
    return AuditWriterSettings()  # pyright: ignore[reportCallIssue] - values come from environment


class Settings(BaseSettings):
    """Environment-backed settings used by Maestro v1."""

    model_config = SettingsConfigDict(
        env_prefix="MAESTRO_",
        extra="ignore",
        enable_decoding=False,
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    allowed_roots: tuple[Path, ...]
    verifier_timeout_seconds: Annotated[float, Field(gt=0, le=3_600)] = 300.0
    max_concurrency: Annotated[int, Field(ge=1, le=64)] = 2
    max_queue_size: Annotated[int, Field(ge=0, le=1_024)] = 4
    max_question_chars: Annotated[int, Field(ge=1, le=MAX_QUESTION_CHARS)] = MAX_QUESTION_CHARS
    max_context_chars: Annotated[int, Field(ge=1, le=MAX_CONTEXT_CHARS)] = MAX_CONTEXT_CHARS
    max_result_bytes: Annotated[int, Field(ge=1_024, le=1_048_576)] = 65_536
    max_agent_output_bytes: Annotated[int, Field(ge=1_024, le=1_048_576)] = 131_072
    max_evidence_items: Annotated[int, Field(ge=1, le=MAX_EVIDENCE_ITEMS)] = 20
    max_conflicts: Annotated[int, Field(ge=0, le=10)] = 10
    max_repository_files: Annotated[int, Field(ge=1, le=100_000)] = 10_000
    max_repository_bytes: Annotated[int, Field(ge=1_024, le=1_073_741_824)] = 67_108_864
    max_file_bytes: Annotated[int, Field(ge=1, le=67_108_864)] = 1_048_576
    log_level: str = "INFO"
    codex_model: ModelIdentifier = Field(default_factory=lambda: ModelIdentifier("gpt-5.4"))
    codex_auth_file: Path | None = None
    codex_api_key: SecretStr | None = None
    audit_writer: AuditWriterSettings = Field(
        default_factory=_load_audit_writer_settings, exclude=True, repr=False
    )

    @field_validator("allowed_roots", mode="before")
    @classmethod
    def parse_allowed_roots(_cls, value: object) -> object:  # noqa: N804
        """Accept path-separator-delimited roots or programmatic path tuples."""

        if isinstance(value, str):
            roots = tuple(Path(part) for part in value.split(os.pathsep) if part.strip())
            if not roots:
                raise ValueError("MAESTRO_ALLOWED_ROOTS must contain at least one path")
            return roots
        return value

    @field_validator("allowed_roots")
    @classmethod
    def canonicalize_allowed_roots(
        _cls,  # noqa: N804
        value: tuple[Path, ...],
    ) -> tuple[Path, ...]:
        """Resolve and validate authorization roots once at startup."""

        if not value:
            raise ValueError("MAESTRO_ALLOWED_ROOTS must contain at least one path")
        resolved: list[Path] = []
        for root in value:
            try:
                canonical = root.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError("an allowed root does not exist") from exc
            if not canonical.is_dir():
                raise ValueError("every allowed root must be a directory")
            if _is_filesystem_anchor(canonical):
                raise ValueError("filesystem anchors cannot be allowed roots")
            if canonical not in resolved:
                resolved.append(canonical)
        return tuple(resolved)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(_cls, value: str) -> str:  # noqa: N804
        """Restrict log configuration to standard non-custom levels."""

        normalized = value.upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("MAESTRO_LOG_LEVEL is invalid")
        return normalized

    @field_validator("codex_auth_file")
    @classmethod
    def validate_auth_file(_cls, value: Path | None) -> Path | None:  # noqa: N804
        """Canonicalize an explicitly configured opaque Codex auth source."""

        if value is None:
            return None
        if value.is_symlink():
            raise ValueError("MAESTRO_CODEX_AUTH_FILE must be a regular non-symlink file")
        try:
            canonical = value.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("MAESTRO_CODEX_AUTH_FILE does not exist") from exc
        if not canonical.is_file():
            raise ValueError("MAESTRO_CODEX_AUTH_FILE must be a regular non-symlink file")
        return canonical

    @model_validator(mode="after")
    def validate_cross_field_limits(self) -> Self:
        """Keep aggregate and per-item repository limits coherent."""

        if self.max_file_bytes > self.max_repository_bytes:
            raise ValueError("MAESTRO_MAX_FILE_BYTES cannot exceed MAESTRO_MAX_REPOSITORY_BYTES")
        if self.codex_auth_file is not None and self.codex_api_key is not None:
            raise ValueError("configure only one Codex authentication source")
        if any(
            self.audit_writer.password_file == root
            or self.audit_writer.password_file.is_relative_to(root)
            for root in self.allowed_roots
        ):
            raise ValueError("Audit writer password file must be outside every allowed root")
        return self

    def codex_runtime_configuration(self) -> CodexRuntimeConfiguration:
        """Project only values the disposable Codex adapter is permitted to receive."""

        return CodexRuntimeConfiguration(
            auth_file=self.codex_auth_file,
            api_key=self.codex_api_key,
        )

    def audit_writer_configuration(self) -> AuditWriterConfiguration:
        """Project the normal runtime's single append-writer credential."""

        return self.audit_writer.connection_configuration()


def _is_filesystem_anchor(path: Path) -> bool:
    return bool(path.anchor) and path == Path(path.anchor)


def _is_ip_literal(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def _validate_audit_postgres_host(value: str) -> str:
    if _is_ip_literal(value):
        return value
    if len(value) > _MAX_POSTGRES_HOST_CHARS or any(
        _DNS_LABEL_PATTERN.fullmatch(label) is None for label in value.split(".")
    ):
        raise ValueError("Audit PostgreSQL host is invalid")
    return value


def _validate_audit_postgres_identifier(value: str) -> str:
    if _POSTGRES_IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError("Audit PostgreSQL identifier is invalid")
    return value


def _canonical_secret_file(path: Path) -> Path:
    try:
        candidate = path.parent.resolve(strict=True) / path.name
        metadata = os.lstat(candidate)
    except (OSError, RuntimeError):
        raise ValueError("Audit password file is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("Audit password file must be a regular non-symlink file")
    return candidate


def _read_audit_password(path: Path) -> SecretStr:
    """Read one bounded owner-only password without following symlinks or leaking details."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        expected = os.lstat(path)
        if stat.S_ISLNK(expected.st_mode):
            raise ValueError("Audit password file must be a regular non-symlink file")
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("Audit password file is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (expected.st_dev, expected.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("Audit password file changed while being opened")
        _validate_audit_password_metadata(metadata)
        raw = _read_bounded_password(descriptor)
    except OSError:
        raise ValueError("Audit password file is unreadable") from None
    finally:
        os.close(descriptor)
    return SecretStr(_decode_audit_password(raw))


def _validate_audit_password_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Audit password file must be regular")
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise ValueError("Audit password file must be owned by the Maestro user")
        if stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
            raise ValueError("Audit password file permissions must be owner-only")
    if metadata.st_size > _MAX_AUDIT_PASSWORD_BYTES:
        raise ValueError("Audit password file is oversized")


def _read_bounded_password(descriptor: int) -> bytearray:
    raw = bytearray()
    while chunk := os.read(descriptor, min(1_024, _MAX_AUDIT_PASSWORD_BYTES + 1 - len(raw))):
        raw.extend(chunk)
        if len(raw) > _MAX_AUDIT_PASSWORD_BYTES:
            raise ValueError("Audit password file is oversized")
    return raw


def _decode_audit_password(raw: bytearray) -> str:
    if raw.endswith(b"\r\n"):
        del raw[-2:]
    elif raw.endswith(b"\n"):
        del raw[-1:]
    if not raw:
        raise ValueError("Audit password file must not be empty")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Audit password file must contain UTF-8 text") from None
    if "\x00" in value:
        raise ValueError("Audit password file contains an invalid value")
    return value
