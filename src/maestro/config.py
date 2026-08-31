"""Central, fail-fast application configuration."""

from __future__ import annotations

import os
import re
import stat
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Self

from psycopg import pq
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from maestro.capabilities.resolve_codebase_fact.contracts import (
    MAX_CONTEXT_CHARS,
    MAX_EVIDENCE_ITEMS,
    MAX_QUESTION_CHARS,
)
from maestro.model_identity import ModelIdentifier

MAX_SECRET_FILE_BYTES = 4_096
_MAX_POSTGRES_HOST_CHARS = 253
_POSTGRES_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,62}\Z")
_DNS_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_LEGACY_AUDIT_DATABASE_URL = "MAESTRO_AUDIT_DATABASE_URL"
_GITHUB_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z"
)
MAX_WORK_ITEM_RESPONSE_BYTES = 1_048_576


def _libpq_environment_names() -> frozenset[str]:
    advertised = {
        option.envvar.decode("ascii")
        for option in pq.Conninfo.get_defaults()
        if option.envvar is not None
    }
    return frozenset((*advertised, "PGSERVICEFILE", "PGSYSCONFDIR"))


_LIBPQ_ENVIRONMENT_NAMES = _libpq_environment_names()


class CodexRuntimeConfiguration(BaseModel):
    """The complete configuration projection permitted to reach the Codex adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    auth_file: Path | None = None
    api_key: SecretStr | None = None


class AgentRuntimeName(StrEnum):
    """Supported worker adapters. Selection is explicit; there is no silent fallback."""

    CODEX = "codex"
    CLAUDE = "claude"


class ClaudeEffort(StrEnum):
    """Reasoning depth for one Claude investigation.

    Measured on the fixture corpus: `low` collected the same evidence but returned
    `uncertain` where `medium` resolved, while `high` did not change the conclusion.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ClaudeRuntimeConfiguration(BaseModel):
    """The complete configuration projection permitted to reach the Claude adapter.

    It carries no credential: the binary resolves the operator's own authentication.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    executable: str
    effort: ClaudeEffort
    max_budget_usd: Annotated[float, Field(gt=0, le=100)]


class _AuditConnectionConfiguration(BaseModel):
    """Validated connection values for one explicitly scoped PostgreSQL role."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    host: str = Field(repr=False)
    port: Annotated[int, Field(ge=1, le=65_535, repr=False)]
    database: str = Field(repr=False)
    user: str = Field(repr=False)
    password: SecretStr = Field(repr=False)

    @field_validator("host")
    @classmethod
    def validate_host(_cls, value: str) -> str:  # noqa: N804
        return _validate_audit_postgres_host(value)

    @field_validator("database", "user")
    @classmethod
    def validate_identifier(_cls, value: str) -> str:  # noqa: N804
        return _validate_audit_postgres_identifier(value)

    @model_validator(mode="after")
    def reject_ambient_libpq_configuration(self) -> Self:
        validate_audit_libpq_environment()
        return self


class AuditBootstrapConfiguration(_AuditConnectionConfiguration):
    """Administrative bootstrap-role connection projection."""


class AuditMigrationConfiguration(_AuditConnectionConfiguration):
    """Schema-owner migration-role connection projection."""


class AuditWriterConfiguration(_AuditConnectionConfiguration):
    """Minimal append-writer connection projection used by Maestro runtime."""


class AuditReaderConfiguration(_AuditConnectionConfiguration):
    """SELECT-only human/query reader connection projection."""


class GitHubWorkItemConfiguration(BaseModel):
    """The complete configuration projection permitted to reach the GitHub adapter.

    Nothing else in the codebase carries a tracker credential or a tracker address, which is
    what keeps the adapter the only place that knows GitHub.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repository: str = Field(repr=False)
    api_url: str = Field(repr=False)
    token: SecretStr = Field(repr=False)
    request_timeout_seconds: Annotated[float, Field(gt=0, le=120)]


class GitHubWorkItemSettings(BaseSettings):
    """Environment-backed work-management credentials for the GitHub adapter."""

    model_config = SettingsConfigDict(
        env_prefix="MAESTRO_WORKITEM_GITHUB_",
        extra="forbid",
        hide_input_in_errors=True,
        case_sensitive=False,
    )

    repository: Annotated[str, Field(min_length=3, max_length=200, repr=False)]
    api_url: Annotated[str, Field(min_length=1, max_length=2_048, repr=False)] = (
        "https://api.github.com"
    )
    token_file: Path = Field(exclude=True, repr=False)
    request_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 15.0

    @field_validator("repository")
    @classmethod
    def validate_repository(_cls, value: str) -> str:  # noqa: N804
        """Require an owner/name pair, so a reference can never widen the path it builds."""

        if _GITHUB_REPOSITORY_PATTERN.fullmatch(value) is None:
            raise ValueError("MAESTRO_WORKITEM_GITHUB_REPOSITORY must read owner/name")
        return value

    @field_validator("api_url")
    @classmethod
    def validate_api_url(_cls, value: str) -> str:  # noqa: N804
        """Require HTTPS, so a token is never offered over a plaintext connection."""

        trimmed = value.rstrip("/")
        if not trimmed.startswith("https://") or " " in trimmed:
            raise ValueError("MAESTRO_WORKITEM_GITHUB_API_URL must be an https URL")
        return trimmed

    @field_validator("token_file")
    @classmethod
    def validate_token_file(_cls, value: Path) -> Path:  # noqa: N804
        """Apply the same owner-only controls an Audit role password receives."""

        canonical = canonical_secret_file(value)
        read_owner_only_secret(canonical)
        return canonical

    def work_item_configuration(self) -> GitHubWorkItemConfiguration:
        """Read the token afresh, so a rotated file is picked up without a restart."""

        return GitHubWorkItemConfiguration(
            repository=self.repository,
            api_url=self.api_url,
            token=read_owner_only_secret(self.token_file),
            request_timeout_seconds=self.request_timeout_seconds,
        )


class _AuditRoleSettings(BaseSettings):
    """Shared validation for one role-specific Audit credential source."""

    model_config = SettingsConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        case_sensitive=False,
    )

    host: Annotated[str, Field(min_length=1, max_length=253, repr=False)] = "localhost"
    port: Annotated[int, Field(ge=1, le=65_535, repr=False)] = 5432
    database: Annotated[str, Field(min_length=1, max_length=63, repr=False)] = "maestro"
    user: Annotated[str, Field(min_length=1, max_length=63, repr=False)]
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

        canonical = canonical_secret_file(value)
        read_owner_only_secret(canonical)
        return canonical

    @model_validator(mode="after")
    def reject_ambient_libpq_configuration(self) -> Self:
        validate_audit_libpq_environment()
        return self

    def _connection_values(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": read_owner_only_secret(self.password_file),
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
    # Deliberately has no default: a deployment states which worker it runs, so a Trail
    # never attributes a result to a provider nobody selected (ADR-0010).
    agent_runtime: AgentRuntimeName
    claude_model: ModelIdentifier = Field(default_factory=lambda: ModelIdentifier("claude-opus-5"))
    claude_executable: Annotated[str, Field(min_length=1, max_length=4_096)] = "claude"
    claude_effort: ClaudeEffort = ClaudeEffort.MEDIUM
    claude_max_budget_usd: Annotated[float, Field(gt=0, le=100)] = 1.0
    audit_writer: AuditWriterSettings = Field(
        default_factory=_load_audit_writer_settings, exclude=True, repr=False
    )

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_audit_database_url(_cls, value: object) -> object:  # noqa: N804
        _reject_legacy_audit_database_url()
        return value

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

    def claude_runtime_configuration(self) -> ClaudeRuntimeConfiguration:
        """Project only values the Claude adapter is permitted to receive."""

        return ClaudeRuntimeConfiguration(
            executable=self.claude_executable,
            effort=self.claude_effort,
            max_budget_usd=self.claude_max_budget_usd,
        )

    def agent_model(self) -> ModelIdentifier:
        """Return the model identity the selected worker runs under."""

        if self.agent_runtime is AgentRuntimeName.CLAUDE:
            return self.claude_model
        return self.codex_model

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


def validate_audit_libpq_environment() -> None:
    """Reject ambient libpq inputs so typed Audit projections are connection-complete."""

    if any(name.upper() in _LIBPQ_ENVIRONMENT_NAMES for name in os.environ):
        raise ValueError("Ambient libpq configuration is not permitted for Audit")


def _reject_legacy_audit_database_url() -> None:
    if any(name.upper() == _LEGACY_AUDIT_DATABASE_URL for name in os.environ):
        raise ValueError(f"{_LEGACY_AUDIT_DATABASE_URL} is no longer supported")


def canonical_secret_file(path: Path) -> Path:
    if os.name != "posix":
        raise ValueError("Secret file controls require a POSIX platform")
    try:
        candidate = path.parent.resolve(strict=True) / path.name
        metadata = os.lstat(candidate)
    except (OSError, RuntimeError):
        raise ValueError("The secret file is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("A secret file must be a regular non-symlink file")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("A secret file must be regular")
    return candidate


def read_owner_only_secret(path: Path) -> SecretStr:
    """Read one bounded owner-only secret without following symlinks or leaking details.

    Every credential Maestro holds comes through this one path: an Audit role password and a
    work-management token are the same kind of thing, and a second reader would be a second
    place for the controls to drift.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        expected = os.lstat(path)
        if stat.S_ISLNK(expected.st_mode):
            raise ValueError("A secret file must be a regular non-symlink file")
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError("A secret file must be regular")
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("The secret file is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (expected.st_dev, expected.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("The secret file changed while being opened")
        _validate_secret_file_metadata(metadata)
        raw = _read_bounded_secret(descriptor)
    except OSError:
        raise ValueError("The secret file is unreadable") from None
    finally:
        os.close(descriptor)
    return SecretStr(_decode_secret(raw))


def _validate_secret_file_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("A secret file must be regular")
    if metadata.st_uid != os.geteuid():
        raise ValueError("A secret file must be owned by the Maestro user")
    if stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
        raise ValueError("Secret file permissions must be owner-only")
    if metadata.st_size > MAX_SECRET_FILE_BYTES:
        raise ValueError("The secret file is oversized")


def _read_bounded_secret(descriptor: int) -> bytearray:
    raw = bytearray()
    while chunk := os.read(descriptor, min(1_024, MAX_SECRET_FILE_BYTES + 1 - len(raw))):
        raw.extend(chunk)
        if len(raw) > MAX_SECRET_FILE_BYTES:
            raise ValueError("The secret file is oversized")
    return raw


def _decode_secret(raw: bytearray) -> str:
    if raw.endswith(b"\r\n"):
        del raw[-2:]
    elif raw.endswith(b"\n"):
        del raw[-1:]
    if not raw:
        raise ValueError("A secret file must not be empty")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("A secret file must contain UTF-8 text") from None
    if "\x00" in value:
        raise ValueError("A secret file contains an invalid value")
    return value
