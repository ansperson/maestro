"""Synchronous trusted filesystem primitives used only by the scan worker."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "vendor",
    }
)


@dataclass(frozen=True, slots=True)
class FileState:
    """Bounded immutable state for one discovered repository path."""

    token: str
    content_digest: str | None
    line_count: int | None
    size: int


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """One file state plus bytes charged to the aggregate scan budget."""

    state: FileState
    consumed_bytes: int


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """Complete bounded output of one synchronous repository scan."""

    files: dict[str, ScannedFile]
    consumed_bytes: int
    truncated: bool


def scan_repository(
    root: Path,
    *,
    max_repository_files: int,
    max_repository_bytes: int,
    max_file_bytes: int,
) -> ScanOutcome:
    """Scan without following directories, executing code, or escaping the root."""

    files: dict[str, ScannedFile] = {}
    aggregate_bytes = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError:
            continue
        for index, entry in enumerate(entries):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                if entry.is_symlink():
                    state = symlink_state(path, root)
                    consumed = 0
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name not in _SKIPPED_DIRECTORIES:
                        pending.append(path)
                    continue
                elif entry.is_file(follow_symlinks=False):
                    state, consumed = file_state(
                        path,
                        max_file_bytes=max_file_bytes,
                        remaining_bytes=max_repository_bytes - aggregate_bytes,
                    )
                else:
                    continue
            except OSError:
                continue
            files[relative] = ScannedFile(state=state, consumed_bytes=consumed)
            aggregate_bytes += consumed
            if len(files) >= max_repository_files:
                truncated = bool(pending) or index < len(entries) - 1
                return ScanOutcome(files, aggregate_bytes, truncated)
            if aggregate_bytes >= max_repository_bytes:
                return ScanOutcome(files, aggregate_bytes, True)
    return ScanOutcome(files, aggregate_bytes, False)


def file_state(
    path: Path,
    *,
    max_file_bytes: int,
    remaining_bytes: int,
) -> tuple[FileState, int]:
    """Read stable regular-file state through a non-following descriptor."""

    limit = min(max_file_bytes, remaining_bytes)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise OSError("path is not a regular file")
        metadata = _file_metadata(initial)
        if initial.st_size > limit:
            return FileState(metadata + ":skipped-size", None, None, initial.st_size), 0
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(65_536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                break
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_identity(initial) != _file_identity(final) or size > limit:
        return FileState(metadata + ":skipped-changing", None, None, final.st_size), 0
    data = b"".join(chunks)
    content_digest = hashlib.sha256(data).hexdigest()
    token = f"{metadata}:{content_digest}"
    if b"\x00" in data[:8_192]:
        return FileState(token + ":binary", None, None, final.st_size), len(data)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return FileState(token + ":non-utf8", None, None, final.st_size), len(data)
    line_count = len(text.splitlines())
    return FileState(token, content_digest, line_count, final.st_size), len(data)


def _file_metadata(value: os.stat_result) -> str:
    return f"file:{value.st_mode}:{value.st_dev}:{value.st_ino}:{value.st_size}:{value.st_mtime_ns}"


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_mode, value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def symlink_state(path: Path, root: Path) -> FileState:
    """Fingerprint a link without following it during traversal."""

    try:
        target = path.resolve(strict=True)
        target_value = target.relative_to(root).as_posix()
        state_token = f"symlink:inside:{target_value}"
    except (OSError, RuntimeError, ValueError):
        state_token = "symlink:unresolved-or-outside"  # noqa: S105 - not a credential
    return FileState(token=state_token, content_digest=None, line_count=None, size=0)
