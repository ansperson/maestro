"""Strict versioned IPC contracts for the repository fingerprint worker."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

FINGERPRINT_PROTOCOL_VERSION = 1
MAX_FINGERPRINT_REQUEST_BYTES = 16_384
MAX_FINGERPRINT_RESULT_BYTES = 16_777_216
MAX_RELATIVE_PATH_CHARS = 4_096
MAX_STATE_TOKEN_CHARS = 8_192

_MAX_REPOSITORY_FILES = 100_000
_MAX_REPOSITORY_BYTES = 1_073_741_824
_MAX_FILE_BYTES = 67_108_864
_MAX_FILE_SIZE = (1 << 63) - 1
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FIRST_CONTROL_FREE_CODEPOINT = 32
_DELETE_CODEPOINT = 127

RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_RELATIVE_PATH_CHARS),
]
StateToken = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_STATE_TOKEN_CHARS),
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FingerprintScanRequestV1(_StrictFrozenModel):
    """Canonical root and numeric bounds sent to the trusted helper."""

    protocol_version: Literal[1]
    root: Annotated[str, StringConstraints(min_length=1, max_length=MAX_RELATIVE_PATH_CHARS)]
    max_repository_files: Annotated[int, Field(ge=1, le=_MAX_REPOSITORY_FILES)]
    max_repository_bytes: Annotated[int, Field(ge=1_024, le=_MAX_REPOSITORY_BYTES)]
    max_file_bytes: Annotated[int, Field(ge=1, le=_MAX_FILE_BYTES)]

    @model_validator(mode="after")
    def validate_byte_bounds(self) -> Self:
        if self.max_file_bytes > self.max_repository_bytes:
            raise ValueError("file byte bound exceeds repository byte bound")
        return self


class FingerprintFileV1(_StrictFrozenModel):
    """Bounded state for one discovered path."""

    relative_path: RelativePath
    token: StateToken
    content_digest: Annotated[str, StringConstraints(pattern=_SHA256_PATTERN)] | None
    line_count: Annotated[int, Field(ge=0, le=_MAX_FILE_BYTES + 1)] | None
    size: Annotated[int, Field(ge=0, le=_MAX_FILE_SIZE)]
    consumed_bytes: Annotated[int, Field(ge=0, le=_MAX_FILE_BYTES)]


class FingerprintScanResultV1(_StrictFrozenModel):
    """Protocol-only bounded result returned by the helper."""

    protocol_version: Literal[1]
    files: Annotated[tuple[FingerprintFileV1, ...], Field(max_length=_MAX_REPOSITORY_FILES)]
    file_count: Annotated[int, Field(ge=0, le=_MAX_REPOSITORY_FILES)]
    consumed_bytes: Annotated[int, Field(ge=0, le=_MAX_REPOSITORY_BYTES)]
    truncated: bool


def validate_fingerprint_result(
    result: FingerprintScanResultV1,
    request: FingerprintScanRequestV1,
) -> None:
    """Validate untrusted helper output against the exact parent request."""

    if result.file_count != len(result.files):
        raise ValueError("fingerprint result cardinality mismatch")
    if result.file_count > request.max_repository_files:
        raise ValueError("fingerprint result exceeds requested file bound")

    paths: set[str] = set()
    consumed_bytes = 0
    for item in result.files:
        if item.relative_path in paths:
            raise ValueError("fingerprint result contains duplicate paths")
        paths.add(item.relative_path)
        _validate_file(item, request)
        consumed_bytes += item.consumed_bytes

    if consumed_bytes != result.consumed_bytes:
        raise ValueError("fingerprint aggregate byte count mismatch")
    if consumed_bytes > request.max_repository_bytes:
        raise ValueError("fingerprint result exceeds requested repository byte bound")


def _validate_file(item: FingerprintFileV1, request: FingerprintScanRequestV1) -> None:
    _validate_relative_path(item.relative_path)
    if _contains_control(item.token):
        raise ValueError("fingerprint state token contains control characters")
    if item.consumed_bytes > request.max_file_bytes:
        raise ValueError("fingerprint file exceeds requested byte bound")
    if item.consumed_bytes > item.size:
        raise ValueError("fingerprint consumed byte count exceeds file size")
    if item.content_digest is None and item.line_count is not None:
        raise ValueError("fingerprint line count requires a content digest")
    if item.content_digest is not None and (
        item.line_count is None or item.consumed_bytes != item.size
    ):
        raise ValueError("fingerprint text state is inconsistent")


def _validate_relative_path(value: str) -> None:
    if "\x00" in value or "\\" in value or _contains_control(value):
        raise ValueError("fingerprint path is not a normalized relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("//")
        or ".." in path.parts
        or path.as_posix() != value
        or value == "."
    ):
        raise ValueError("fingerprint path is not a normalized relative path")


def _contains_control(value: str) -> bool:
    return any(
        ord(character) < _FIRST_CONTROL_FREE_CODEPOINT or ord(character) == _DELETE_CODEPOINT
        for character in value
    )
