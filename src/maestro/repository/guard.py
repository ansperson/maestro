"""Repository authorization, bounded fingerprinting, and evidence validation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from maestro.capabilities.resolve_codebase_fact.contracts import Evidence
from maestro.config import Settings
from maestro.errors import (
    EvidenceValidationError,
    InvalidInputError,
    RepositoryNotAllowedError,
    RepositoryNotFoundError,
)

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
class RepositoryFingerprint:
    """Repository identity and bounded working-tree snapshot."""

    digest: str
    repository_id: str
    git_top_level_id: str | None
    head: str | None
    dirty_digest: str | None
    files: dict[str, FileState]
    truncated: bool


@dataclass(frozen=True, slots=True)
class AuthorizedRepository:
    """Canonical investigation root approved by server configuration."""

    root: Path
    repository_id: str


class RepositoryGuard:
    """Enforce path authorization and repository/evidence consistency."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def authorize(self, repository_path: str) -> AuthorizedRepository:
        """Canonicalize one caller path without widening it to a Git parent."""

        if "\x00" in repository_path:
            raise InvalidInputError("repository_path contains an invalid NUL character")
        requested = Path(repository_path)
        if ".." in requested.parts:
            raise InvalidInputError("repository_path must not contain parent traversal")
        try:
            canonical = requested.resolve(strict=True)
        except (
            FileNotFoundError,
            NotADirectoryError,
            PermissionError,
            RuntimeError,
            OSError,
        ) as exc:
            raise RepositoryNotFoundError from exc
        if not canonical.is_dir():
            raise RepositoryNotFoundError
        if not any(_is_within(canonical, root) for root in self._settings.allowed_roots):
            raise RepositoryNotAllowedError
        return AuthorizedRepository(root=canonical, repository_id=_private_path_id(canonical))

    async def fingerprint(self, repository: AuthorizedRepository) -> RepositoryFingerprint:
        """Capture Git identity plus a bounded content-aware working-tree snapshot."""

        files, truncated = await asyncio.to_thread(self._scan_files, repository.root)
        git_top_level, head, dirty = await _git_state(repository.root)
        top_level_id = _private_path_id(git_top_level) if git_top_level is not None else None
        digest = hashlib.sha256()
        for value in (
            repository.repository_id,
            top_level_id or "",
            head or "",
            dirty or "",
            "truncated" if truncated else "complete",
        ):
            digest.update(value.encode())
            digest.update(b"\0")
        for relative, state in sorted(files.items()):
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(state.token.encode())
            digest.update(b"\0")
        return RepositoryFingerprint(
            digest=digest.hexdigest(),
            repository_id=repository.repository_id,
            git_top_level_id=top_level_id,
            head=head,
            dirty_digest=dirty,
            files=files,
            truncated=truncated,
        )

    async def validate_evidence(
        self,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint,
        evidence_items: Iterable[Evidence],
    ) -> None:
        """Verify every AI-produced path, line range, and initial file state."""

        for evidence in evidence_items:
            await asyncio.to_thread(
                self._validate_one_evidence,
                repository.root,
                fingerprint,
                evidence,
            )

    def _scan_files(self, root: Path) -> tuple[dict[str, FileState], bool]:
        files: dict[str, FileState] = {}
        aggregate_bytes = 0
        truncated = False
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                try:
                    if entry.is_symlink():
                        files[relative] = _symlink_state(path, root)
                    elif entry.is_dir(follow_symlinks=False):
                        if entry.name not in _SKIPPED_DIRECTORIES:
                            pending.append(path)
                        continue
                    elif entry.is_file(follow_symlinks=False):
                        state, consumed = _file_state(
                            path,
                            max_file_bytes=self._settings.max_file_bytes,
                            remaining_bytes=self._settings.max_repository_bytes - aggregate_bytes,
                        )
                        files[relative] = state
                        aggregate_bytes += consumed
                    else:
                        continue
                except OSError:
                    continue
                if len(files) >= self._settings.max_repository_files:
                    truncated = bool(pending) or len(entries) > 1
                    return files, truncated
                if aggregate_bytes >= self._settings.max_repository_bytes:
                    truncated = True
                    return files, truncated
        return files, truncated

    def _validate_one_evidence(
        self,
        root: Path,
        fingerprint: RepositoryFingerprint,
        evidence: Evidence,
    ) -> None:
        relative = _validated_relative_evidence_path(evidence.path)
        initial = fingerprint.files.get(relative.as_posix())
        if initial is None or initial.content_digest is None or initial.line_count is None:
            raise EvidenceValidationError(
                "Evidence must reference a discovered UTF-8 text file within repository limits."
            )
        candidate = root.joinpath(*relative.parts)
        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise EvidenceValidationError("An evidence file no longer exists.") from exc
        if not _is_within(canonical, root) or not canonical.is_file() or candidate.is_symlink():
            raise EvidenceValidationError("An evidence path escapes or is not a regular file.")
        current, _ = _file_state(
            canonical,
            max_file_bytes=self._settings.max_file_bytes,
            remaining_bytes=self._settings.max_file_bytes,
        )
        if current.token != initial.token:
            raise EvidenceValidationError("An evidence file changed during investigation.")
        if evidence.line_start is not None:
            line_end = evidence.line_end or evidence.line_start
            if current.line_count is None:
                raise EvidenceValidationError("An evidence file is not valid UTF-8 text.")
            if line_end > current.line_count:
                raise EvidenceValidationError("An evidence line range is outside the file.")


def _validated_relative_evidence_path(raw_path: str) -> PurePosixPath:
    if "\x00" in raw_path or "\\" in raw_path:
        raise EvidenceValidationError(
            "Evidence paths must be normalized repository-relative paths."
        )
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw_path:
        raise EvidenceValidationError(
            "Evidence paths must be normalized repository-relative paths."
        )
    return path


def _file_state(path: Path, *, max_file_bytes: int, remaining_bytes: int) -> tuple[FileState, int]:
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


def _symlink_state(path: Path, root: Path) -> FileState:
    try:
        target = path.resolve(strict=True)
        target_value = target.relative_to(root).as_posix()
        state_token = f"symlink:inside:{target_value}"
    except (OSError, RuntimeError, ValueError):
        state_token = "symlink:unresolved-or-outside"  # noqa: S105 - not a credential
    return FileState(token=state_token, content_digest=None, line_count=None, size=0)


async def _git_state(root: Path) -> tuple[Path | None, str | None, str | None]:
    top_level_output = await _run_git(root, "rev-parse", "--show-toplevel")
    if top_level_output is None:
        return None, None, None
    try:
        top_level = await asyncio.to_thread(
            Path(top_level_output.decode().strip()).resolve,
            strict=True,
        )
    except (OSError, UnicodeDecodeError):
        return None, None, None
    head_output, status_output = await asyncio.gather(
        _run_git(root, "rev-parse", "--verify", "HEAD"),
        _run_git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--", "."),
    )
    try:
        head = head_output.decode().strip() if head_output is not None else None
    except UnicodeDecodeError:
        head = None
    dirty_digest = hashlib.sha256(status_output).hexdigest() if status_output is not None else None
    return top_level, head, dirty_digest


async def _run_git(root: Path, *arguments: str) -> bytes | None:
    environment = {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.devnull,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.pager=cat",
            "-c",
            "pager.status=false",
            *arguments,
            cwd=root,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    stdout, _ = await process.communicate()
    return stdout if process.returncode == 0 else None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _private_path_id(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
