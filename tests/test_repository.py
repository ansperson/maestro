from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import time
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
    RepositoryInspectionError,
    RepositoryNotAllowedError,
    RepositoryNotFoundError,
)
from maestro.repository import RepositoryGuard, fingerprint_worker
from maestro.repository.fingerprint_protocol import (
    MAX_FINGERPRINT_REQUEST_BYTES,
    FingerprintFileV1,
    FingerprintScanRequestV1,
    FingerprintScanResultV1,
    validate_fingerprint_result,
)
from maestro.repository.fingerprint_scan import scan_repository
from maestro.repository.subprocess import run_owned_process

SettingsFactory = Callable[..., Settings]
_PROCESS_FIXTURE = Path(__file__).parent / "helpers" / "fingerprint_process.py"


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


def test_authorize_rejects_platform_anchor_even_if_settings_validation_is_bypassed(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    anchor = Path(repository.anchor)
    settings = settings_factory(allowed_roots=(repository,)).model_copy(
        update={"allowed_roots": (anchor,)}
    )
    guard = RepositoryGuard(settings)
    with pytest.raises(RepositoryNotAllowedError):
        guard.authorize(str(anchor))


@given(redundant_segments=st.integers(min_value=0, max_value=8))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_authorize_rejects_every_canonical_anchor_alias(
    repository: Path,
    settings_factory: SettingsFactory,
    redundant_segments: int,
) -> None:
    anchor = Path(repository.anchor)
    bypassed = settings_factory(allowed_roots=(repository,)).model_copy(
        update={"allowed_roots": (anchor,)}
    )
    candidate = f"{anchor}{f'.{os.sep}' * redundant_segments}"
    with pytest.raises(RepositoryNotAllowedError):
        RepositoryGuard(bypassed).authorize(candidate)


@given(parts=st.lists(st.from_regex(r"[a-z]{1,8}", fullmatch=True), min_size=1, max_size=4))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_authorize_accepts_non_anchor_descendants(
    repository: Path,
    settings_factory: SettingsFactory,
    parts: list[str],
) -> None:
    candidate = repository.joinpath(*parts)
    candidate.mkdir(parents=True, exist_ok=True)
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    assert guard.authorize(str(candidate)).root == candidate.resolve()


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
async def test_isolated_helper_matches_trusted_scan_primitives(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    settings = settings_factory(allowed_roots=(repository,))
    direct = scan_repository(
        repository,
        max_repository_files=settings.max_repository_files,
        max_repository_bytes=settings.max_repository_bytes,
        max_file_bytes=settings.max_file_bytes,
    )
    guard = RepositoryGuard(settings)
    helper = await guard.fingerprint(guard.authorize(str(repository)))

    assert helper.truncated is direct.truncated
    assert helper.files == {relative: scanned.state for relative, scanned in direct.files.items()}


class _BinaryStream:
    def __init__(self, value: bytes = b"") -> None:
        self.buffer = io.BytesIO(value)


@pytest.mark.parametrize("payload", [b"{", b"x" * (MAX_FINGERPRINT_REQUEST_BYTES + 1)])
def test_fingerprint_worker_rejects_invalid_or_oversized_request(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    output = _BinaryStream()
    monkeypatch.setattr(fingerprint_worker.sys, "stdin", _BinaryStream(payload))
    monkeypatch.setattr(fingerprint_worker.sys, "stdout", output)

    assert fingerprint_worker.main() == 1
    assert output.buffer.getvalue() == b""


def test_fingerprint_worker_emits_strict_result_for_canonical_root(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(allowed_roots=(repository,))
    request = FingerprintScanRequestV1(
        protocol_version=1,
        root=str(repository.resolve()),
        max_repository_files=settings.max_repository_files,
        max_repository_bytes=settings.max_repository_bytes,
        max_file_bytes=settings.max_file_bytes,
    )
    output = _BinaryStream()
    monkeypatch.setattr(
        fingerprint_worker.sys,
        "stdin",
        _BinaryStream(request.model_dump_json().encode("utf-8")),
    )
    monkeypatch.setattr(fingerprint_worker.sys, "stdout", output)

    assert fingerprint_worker.main() == 0
    result = FingerprintScanResultV1.model_validate_json(output.buffer.getvalue(), strict=True)
    validate_fingerprint_result(result, request)


def test_fingerprint_worker_is_packaged_and_receives_no_repository_argv(
    repository: Path,
) -> None:
    command = repository_module._fingerprint_worker_command()  # pyright: ignore[reportPrivateUsage]
    assert command == (
        sys.executable,
        "-I",
        "-m",
        "maestro.repository.fingerprint_worker",
    )
    assert str(repository) not in command
    package_worker = Path(repository_module.__file__).parent / "fingerprint_worker.py"
    assert package_worker.is_file()


@given(
    parts=st.lists(
        st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}", fullmatch=True),
        min_size=1,
        max_size=8,
    ),
    size=st.integers(min_value=0, max_value=1_024),
)
def test_fingerprint_protocol_accepts_normalized_paths_at_numeric_boundaries(
    parts: list[str],
    size: int,
) -> None:
    request = FingerprintScanRequestV1(
        protocol_version=1,
        root="/canonical/repository",
        max_repository_files=1,
        max_repository_bytes=1_024,
        max_file_bytes=1_024,
    )
    result = FingerprintScanResultV1(
        protocol_version=1,
        files=(
            FingerprintFileV1(
                relative_path="/".join(parts),
                token="file:bounded",  # noqa: S106 - fingerprint state, not a credential
                content_digest="a" * 64,
                line_count=0,
                size=size,
                consumed_bytes=size,
            ),
        ),
        file_count=1,
        consumed_bytes=size,
        truncated=False,
    )

    validate_fingerprint_result(result, request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "malformed",
        "duplicate",
        "traversal",
        "absolute",
        "backslash",
        "nul",
        "wrong-version",
        "missing-version",
        "count-mismatch",
        "aggregate-mismatch",
        "bad-digest",
        "bad-token",
        "bad-file-state",
        "negative-size",
        "consumed-over-size",
        "token-too-long",
        "path-too-long",
        "bad-truncation",
        "stderr-detail",
    ],
)
async def test_fingerprint_rejects_hostile_real_process_protocol(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(
        repository_module,
        "_fingerprint_worker_command",
        lambda: (sys.executable, "-I", str(_PROCESS_FIXTURE), mode),
    )
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))

    with pytest.raises(RepositoryInspectionError) as error:
        await guard.fingerprint(guard.authorize(str(repository)))

    assert str(repository) not in error.value.public_json()


@pytest.mark.asyncio
async def test_fingerprint_rejects_oversized_real_process_stdout(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_limit = 1_024
    monkeypatch.setattr(repository_module, "MAX_FINGERPRINT_RESULT_BYTES", output_limit)
    monkeypatch.setattr(
        repository_module,
        "_fingerprint_worker_command",
        lambda: (
            sys.executable,
            "-I",
            str(_PROCESS_FIXTURE),
            "oversized",
            str(output_limit + 1),
        ),
    )
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))

    with pytest.raises(RepositoryInspectionError):
        await guard.fingerprint(guard.authorize(str(repository)))


@pytest.mark.asyncio
async def test_fingerprint_real_process_has_minimal_environment_and_closed_fds(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = os.open(repository / "src" / "models.py", os.O_RDONLY)
    os.set_inheritable(descriptor, True)
    expected_environment = ",".join(sorted(repository_module._fingerprint_environment()))  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(
        repository_module,
        "_fingerprint_worker_command",
        lambda: (
            sys.executable,
            "-I",
            str(_PROCESS_FIXTURE),
            "environment-fd",
            expected_environment,
            str(descriptor),
        ),
    )
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    try:
        fingerprint = await guard.fingerprint(guard.authorize(str(repository)))
    finally:
        os.close(descriptor)

    assert fingerprint.files["probe.txt"].token == "probe:clean"  # noqa: S105


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["block", "wait-block"])
async def test_fingerprint_cancellation_forces_kill_and_reaps_real_process(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    marker = tmp_path / f"{mode}.pid"
    monkeypatch.setattr(
        repository_module,
        "_fingerprint_worker_command",
        lambda: (sys.executable, "-I", str(_PROCESS_FIXTURE), mode, str(marker)),
    )
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    task = asyncio.create_task(guard.fingerprint(guard.authorize(str(repository))))
    await _wait_for_marker(marker)
    process_id = int(marker.read_text(encoding="ascii"))

    task.cancel()
    asyncio.get_running_loop().call_later(0.05, task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_process_reaped(process_id)


@pytest.mark.asyncio
async def test_owned_process_cancellation_during_stdin_write_reaps_child(tmp_path: Path) -> None:
    marker = tmp_path / "stdin.pid"
    task = asyncio.create_task(
        run_owned_process(
            (sys.executable, "-I", str(_PROCESS_FIXTURE), "block-no-read", str(marker)),
            cwd=Path(sys.executable).parent,
            environment=repository_module._fingerprint_environment(),  # pyright: ignore[reportPrivateUsage]
            input_data=b"x" * 1_048_576,
            max_stdout_bytes=1_024,
        )
    )
    await _wait_for_marker(marker)
    process_id = int(marker.read_text(encoding="ascii"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_process_reaped(process_id)


@pytest.mark.asyncio
async def test_fingerprint_cancellation_during_process_creation_reaps_child(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "create.pid"
    original_create = asyncio.create_subprocess_exec
    release_creation = asyncio.Event()

    async def delayed_create(*arguments: str, **keywords: object) -> asyncio.subprocess.Process:
        process = await original_create(
            *arguments,
            **keywords,  # pyright: ignore[reportArgumentType]
        )
        await release_creation.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    monkeypatch.setattr(
        repository_module,
        "_fingerprint_worker_command",
        lambda: (
            sys.executable,
            "-I",
            str(_PROCESS_FIXTURE),
            "block-no-read",
            str(marker),
        ),
    )
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    task = asyncio.create_task(guard.fingerprint(guard.authorize(str(repository))))
    await _wait_for_marker(marker)
    process_id = int(marker.read_text(encoding="ascii"))

    task.cancel()
    release_creation.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_process_reaped(process_id)


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


@pytest.mark.asyncio
async def test_git_cancellation_forces_kill_and_reaps_real_process(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "git.pid"
    monkeypatch.setattr(
        repository_module,
        "_git_command",
        lambda: (
            sys.executable,
            "-I",
            str(_PROCESS_FIXTURE),
            "block",
            str(marker),
        ),
    )
    task = asyncio.create_task(
        repository_module._run_git(repository, "status")  # pyright: ignore[reportPrivateUsage]
    )
    await _wait_for_marker(marker)
    process_id = int(marker.read_text(encoding="ascii"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_process_reaped(process_id)


async def _wait_for_marker(marker: Path) -> None:
    deadline = time.monotonic() + 5
    while not await asyncio.to_thread(marker.exists):
        if time.monotonic() >= deadline:
            pytest.fail("real subprocess did not create its lifecycle marker")
        await asyncio.sleep(0.01)


def _assert_process_reaped(process_id: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
