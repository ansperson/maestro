from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from shutil import which

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import maestro.repository.guard as repository_module
from maestro.capabilities.resolve_codebase_fact.contracts import Evidence
from maestro.config import Settings
from maestro.errors import (
    EvidenceValidationError,
    InvalidInputError,
    RepositoryNotAllowedError,
    RepositoryNotFoundError,
)
from maestro.repository import RepositoryGuard

SettingsFactory = Callable[..., Settings]


def _remove_top_level_files(repository: Path) -> None:
    for path in repository.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink()


def test_authorize_preserves_requested_subdirectory(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository.parent,)))
    authorized = guard.authorize(str(repository / "src"))
    assert authorized.root == (repository / "src").resolve()


def test_authorize_rejects_missing_file_outside_and_prefix_sibling(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    with pytest.raises(RepositoryNotFoundError):
        guard.authorize(str(repository / "missing"))
    with pytest.raises(RepositoryNotFoundError):
        guard.authorize(str(repository / "src" / "models.py"))
    sibling = repository.parent / f"{repository.name}-sibling"
    sibling.mkdir()
    with pytest.raises(RepositoryNotAllowedError):
        guard.authorize(str(sibling))


def test_authorize_rejects_nul(repository: Path, settings_factory: SettingsFactory) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    with pytest.raises(InvalidInputError, match="NUL"):
        guard.authorize(f"{repository}\x00suffix")


@given(traversals=st.lists(st.sampled_from(["..", ".", "src"]), min_size=1, max_size=6))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_authorize_rejects_every_parent_traversal(
    repository: Path, settings_factory: SettingsFactory, traversals: list[str]
) -> None:
    if ".." not in traversals:
        return
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    with pytest.raises(InvalidInputError):
        guard.authorize(str(repository.joinpath(*traversals)))


def test_authorize_rejects_symlink_escape(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    outside = repository.parent / "outside"
    outside.mkdir()
    link = repository / "escape"
    link.symlink_to(outside, target_is_directory=True)
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    with pytest.raises(RepositoryNotAllowedError):
        guard.authorize(str(link))


@pytest.mark.asyncio
async def test_fingerprint_is_content_aware_and_bounds_special_files(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    authorized = guard.authorize(str(repository))
    before = await guard.fingerprint(authorized)
    assert before.files["binary.dat"].line_count is None
    assert before.files["non-utf8.txt"].line_count is None
    assert before.files["oversized.txt"].content_digest is None
    (repository / "src" / "models.py").write_text("changed\n", encoding="utf-8")
    after = await guard.fingerprint(authorized)
    assert before.digest != after.digest


@pytest.mark.asyncio
async def test_fingerprint_handles_symlink_loop_and_file_cap(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    (repository / "loop").symlink_to(repository / "loop")
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,), max_repository_files=2))
    fingerprint = await guard.fingerprint(guard.authorize(str(repository)))
    assert fingerprint.truncated is True
    assert len(fingerprint.files) == 2


@pytest.mark.asyncio
async def test_fingerprint_records_internal_and_external_symlinks_without_following(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    (repository / "inside-link").symlink_to(repository / "src" / "models.py")
    (repository / "outside-link").symlink_to(repository.parent)
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    fingerprint = await guard.fingerprint(guard.authorize(str(repository)))
    assert fingerprint.files["inside-link"].token == "symlink:inside:src/models.py"  # noqa: S105
    assert fingerprint.files["outside-link"].token == "symlink:unresolved-or-outside"  # noqa: S105
    assert fingerprint.files["inside-link"].content_digest is None


@pytest.mark.asyncio
async def test_fingerprint_enforces_aggregate_byte_cap_and_skips_special_file(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    _remove_top_level_files(repository)
    (repository / "z.txt").write_text("z" * 1_024, encoding="utf-8")
    fifo = repository / "zz-named-pipe"
    os.mkfifo(fifo)
    guard = RepositoryGuard(
        settings_factory(
            allowed_roots=(repository,),
            max_file_bytes=1_024,
            max_repository_bytes=1_024,
        )
    )
    fingerprint = await guard.fingerprint(guard.authorize(str(repository)))
    assert fingerprint.truncated is True
    assert "zz-named-pipe" not in fingerprint.files
    assert (
        sum(state.size for state in fingerprint.files.values() if state.content_digest is not None)
        <= 1_024
    )


@pytest.mark.asyncio
async def test_git_fingerprint_includes_head_dirty_state_without_widening(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    _initialize_git_repository(repository)
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    authorized = guard.authorize(str(repository / "src"))
    clean = await guard.fingerprint(authorized)
    assert clean.head is not None
    assert clean.git_top_level_id is not None
    assert clean.git_top_level_id != clean.repository_id
    (repository / "src" / "models.py").write_text("dirty\n", encoding="utf-8")
    dirty = await guard.fingerprint(authorized)
    assert clean.dirty_digest != dirty.dirty_digest
    assert authorized.root == (repository / "src").resolve()


def _initialize_git_repository(repository: Path) -> None:
    git = which("git")
    if git is None:
        pytest.skip("git is required for repository fingerprint coverage")
    subprocess.run([git, "init", "-q"], cwd=repository, check=True)
    subprocess.run([git, "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Maestro Test",
            "-c",
            "user.email=maestro@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repository,
        check=True,
    )


@pytest.mark.asyncio
async def test_validate_good_evidence(repository: Path, settings_factory: SettingsFactory) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    authorized = guard.authorize(str(repository))
    fingerprint = await guard.fingerprint(authorized)
    evidence = Evidence(
        path="src/models.py",
        line_start=1,
        line_end=3,
        symbol="Order",
        finding="Order stores a list.",
    )
    await guard.validate_evidence(authorized, fingerprint, [evidence])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        Evidence(path="missing.py", finding="hallucinated"),
        Evidence(path="src/models.py", line_start=999, finding="bad line"),
        Evidence(path="binary.dat", finding="binary"),
        Evidence(path="non-utf8.txt", finding="encoding"),
        Evidence(path="oversized.txt", finding="oversized"),
    ],
)
async def test_validate_rejects_unusable_evidence(
    repository: Path, settings_factory: SettingsFactory, evidence: Evidence
) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    authorized = guard.authorize(str(repository))
    fingerprint = await guard.fingerprint(authorized)
    with pytest.raises(EvidenceValidationError):
        await guard.validate_evidence(authorized, fingerprint, [evidence])


@pytest.mark.asyncio
async def test_validate_rejects_changed_deleted_and_symlink_evidence(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    authorized = guard.authorize(str(repository))
    fingerprint = await guard.fingerprint(authorized)
    source = repository / "src" / "models.py"
    source.write_text("mutated\n", encoding="utf-8")
    with pytest.raises(EvidenceValidationError, match="changed"):
        await guard.validate_evidence(
            authorized, fingerprint, [Evidence(path="src/models.py", finding="changed")]
        )
    source.unlink()
    with pytest.raises(EvidenceValidationError):
        await guard.validate_evidence(
            authorized, fingerprint, [Evidence(path="src/models.py", finding="deleted")]
        )

    target = repository / "migrations" / "001_payments.sql"
    source.symlink_to(target)
    with pytest.raises(EvidenceValidationError):
        await guard.validate_evidence(
            authorized, fingerprint, [Evidence(path="src/models.py", finding="symlink")]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../outside", "src\\models.py", "/absolute"])
async def test_validate_defends_against_bypassed_contract_path_validation(
    repository: Path,
    settings_factory: SettingsFactory,
    path: str,
) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    authorized = guard.authorize(str(repository))
    fingerprint = await guard.fingerprint(authorized)
    evidence = Evidence(path="src/models.py", finding="placeholder").model_copy(
        update={"path": path}
    )
    with pytest.raises(EvidenceValidationError, match="normalized"):
        await guard.validate_evidence(authorized, fingerprint, [evidence])


@pytest.mark.asyncio
async def test_git_state_fails_closed_on_invalid_path_and_missing_git(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run_git = repository_module._run_git  # pyright: ignore[reportPrivateUsage]

    async def invalid_top_level(_root: Path, *arguments: str) -> bytes | None:
        if "--show-toplevel" in arguments:
            return b"\xff"
        return b""

    monkeypatch.setattr(repository_module, "_run_git", invalid_top_level)
    assert await repository_module._git_state(repository) == (  # pyright: ignore[reportPrivateUsage]
        None,
        None,
        None,
    )
    monkeypatch.setattr(repository_module, "_run_git", original_run_git)

    async def missing_process(*_args: object, **_kwargs: object) -> object:
        raise OSError("git missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_process)
    assert (
        await repository_module._run_git(  # pyright: ignore[reportPrivateUsage]
            repository, "status"
        )
        is None
    )
