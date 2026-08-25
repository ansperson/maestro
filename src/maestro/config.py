"""Central, fail-fast application configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Self

from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from maestro.capabilities.resolve_codebase_fact.contracts import (
    MAX_CONTEXT_CHARS,
    MAX_EVIDENCE_ITEMS,
    MAX_QUESTION_CHARS,
)

_MAX_MODEL_IDENTIFIER_CHARS = 128
_FIRST_PRINTABLE_CODEPOINT = 33


class Settings(BaseSettings):
    """Environment-backed settings used by Maestro v1."""

    model_config = SettingsConfigDict(
        env_prefix="MAESTRO_",
        extra="ignore",
        enable_decoding=False,
        case_sensitive=False,
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
    codex_model: str = "gpt-5.4"
    codex_auth_file: Path | None = None
    codex_api_key: SecretStr | None = None
    audit_database_url: Annotated[SecretStr, Field(min_length=1, max_length=4_096)] = Field()

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

    @field_validator("codex_model")
    @classmethod
    def validate_model(_cls, value: str) -> str:  # noqa: N804
        """Reject empty or control-bearing model identifiers."""

        stripped = value.strip()
        if (
            not stripped
            or len(stripped) > _MAX_MODEL_IDENTIFIER_CHARS
            or any(ord(char) < _FIRST_PRINTABLE_CODEPOINT for char in stripped)
        ):
            raise ValueError("MAESTRO_CODEX_MODEL is invalid")
        return stripped

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

    @field_validator("audit_database_url")
    @classmethod
    def validate_audit_database_url(_cls, value: SecretStr) -> SecretStr:  # noqa: N804
        """Reject malformed PostgreSQL connection configuration without connecting."""

        try:
            conninfo_to_dict(value.get_secret_value())
        except Exception:
            raise ValueError("MAESTRO_AUDIT_DATABASE_URL is invalid") from None
        return value

    @model_validator(mode="after")
    def validate_cross_field_limits(self) -> Self:
        """Keep aggregate and per-item repository limits coherent."""

        if self.max_file_bytes > self.max_repository_bytes:
            raise ValueError("MAESTRO_MAX_FILE_BYTES cannot exceed MAESTRO_MAX_REPOSITORY_BYTES")
        if self.codex_auth_file is not None and self.codex_api_key is not None:
            raise ValueError("configure only one Codex authentication source")
        return self


def _is_filesystem_anchor(path: Path) -> bool:
    return bool(path.anchor) and path == Path(path.anchor)
