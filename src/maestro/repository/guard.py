"""Repository authorization, bounded fingerprinting, and evidence validation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from maestro.capabilities.resolve_codebase_fact.contracts import Evidence
from maestro.config import Settings
from maestro.errors import (
    EvidenceValidationError,
    InvalidInputError,
    RepositoryInspectionError,
    RepositoryNotAllowedError,
    RepositoryNotFoundError,
)
from maestro.repository.fingerprint_protocol import (
    FINGERPRINT_PROTOCOL_VERSION,
    MAX_CANONICAL_PATH_RESULT_BYTES,
    MAX_FINGERPRINT_RESULT_BYTES,
    CanonicalPathRequestV1,
    CanonicalPathResultV1,
    FingerprintScanRequestV1,
    FingerprintScanResultV1,
    validate_canonical_path_result,
    validate_fingerprint_result,
)
from maestro.repository.fingerprint_scan import FileState, file_state
from maestro.repository.subprocess import ProcessOutputLimitError, run_owned_process

_GIT_OUTPUT_LIMIT_BYTES = 16_777_216


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
        if _is_filesystem_anchor(canonical):
            raise RepositoryNotAllowedError
        if not any(_is_within(canonical, root) for root in self._settings.allowed_roots):
            raise RepositoryNotAllowedError
        return AuthorizedRepository(root=canonical, repository_id=_private_path_id(canonical))

    async def fingerprint(self, repository: AuthorizedRepository) -> RepositoryFingerprint:
        """Capture Git identity plus a bounded content-aware working-tree snapshot."""

        files, truncated = await self._scan_files(repository.root)
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

    async def _scan_files(self, root: Path) -> tuple[dict[str, FileState], bool]:
        try:
            request = FingerprintScanRequestV1(
                protocol_version=FINGERPRINT_PROTOCOL_VERSION,
                root=str(root),
                max_repository_files=self._settings.max_repository_files,
                max_repository_bytes=self._settings.max_repository_bytes,
                max_file_bytes=self._settings.max_file_bytes,
            )
            completed = await run_owned_process(
                _fingerprint_worker_command(),
                cwd=_trusted_worker_cwd(root),
                environment=_fingerprint_environment(),
                input_data=request.model_dump_json().encode("utf-8"),
                max_stdout_bytes=MAX_FINGERPRINT_RESULT_BYTES,
            )
            if completed.returncode != 0:
                raise RepositoryInspectionError
            result = FingerprintScanResultV1.model_validate_json(
                completed.stdout,
                strict=True,
            )
            validate_fingerprint_result(result, request)
        except (OSError, ProcessOutputLimitError, ValidationError, ValueError):
            raise RepositoryInspectionError from None

        files = {
            item.relative_path: FileState(
                token=item.token,
                content_digest=item.content_digest,
                line_count=item.line_count,
                size=item.size,
            )
            for item in result.files
        }
        return files, result.truncated

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
        current, _ = file_state(
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


def _is_filesystem_anchor(path: Path) -> bool:
    return bool(path.anchor) and path == Path(path.anchor)


async def _git_state(root: Path) -> tuple[Path | None, str | None, str | None]:
    top_level_output = await _run_git(root, "rev-parse", "--show-toplevel")
    if top_level_output is None:
        return None, None, None
    top_level = await _canonicalize_git_top_level(root, top_level_output)
    if top_level is None:
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
        completed = await run_owned_process(
            (
                *_git_command(),
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
            ),
            cwd=root,
            environment=environment,
            input_data=b"",
            max_stdout_bytes=_GIT_OUTPUT_LIMIT_BYTES,
        )
    except (OSError, ProcessOutputLimitError):
        return None
    return completed.stdout if completed.returncode == 0 else None


async def _canonicalize_git_top_level(root: Path, output: bytes) -> Path | None:
    try:
        decoded = output.decode("utf-8", errors="strict")
        if not decoded.endswith("\n"):
            return None
        request = CanonicalPathRequestV1(
            protocol_version=FINGERPRINT_PROTOCOL_VERSION,
            path=decoded.removesuffix("\n"),
        )
        completed = await run_owned_process(
            _canonical_path_worker_command(),
            cwd=_trusted_worker_cwd(root),
            environment=_fingerprint_environment(),
            input_data=request.model_dump_json().encode("utf-8"),
            max_stdout_bytes=MAX_CANONICAL_PATH_RESULT_BYTES,
        )
        if completed.returncode != 0:
            return None
        result = CanonicalPathResultV1.model_validate_json(completed.stdout, strict=True)
        canonical = validate_canonical_path_result(result)
    except (OSError, ProcessOutputLimitError, UnicodeDecodeError, ValidationError, ValueError):
        return None
    return canonical if _is_within(root, canonical) else None


def _fingerprint_worker_command() -> tuple[str, ...]:
    return (sys.executable, "-I", "-m", "maestro.repository.fingerprint_worker")


def _canonical_path_worker_command() -> tuple[str, ...]:
    return (sys.executable, "-I", "-m", "maestro.repository.canonical_path_worker")


def _trusted_worker_cwd(root: Path) -> Path:
    cwd = Path(root.anchor)
    if not cwd.is_absolute() or _is_within(cwd, root):
        raise RepositoryInspectionError
    return cwd


def _fingerprint_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }


def _git_command() -> tuple[str, ...]:
    return ("git",)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _private_path_id(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
