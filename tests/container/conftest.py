from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest

_PROJECT_ROOT = Path(__file__).parents[2]
_LAUNCHER = _PROJECT_ROOT / "scripts" / "maestro_container.py"
_FIXTURE = _PROJECT_ROOT / "tests" / "fixtures" / "codebase"
_TEST_ROOT = Path(
    os.environ.get("MAESTRO_CONTAINER_TEST_ROOT", _PROJECT_ROOT / ".container-test-tmp")
)
_DEFAULT_IMAGE = "maestro-verifier:container-test"

CommandFactory = Callable[[Path, str | None, Mapping[str, str] | None], list[str]]


def _run(
    command: list[str],
    *,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=_PROJECT_ROOT,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="session")
def container_image() -> str:
    """Build once unless the caller supplied an already built image."""

    image = os.environ.get("MAESTRO_CONTAINER_IMAGE", _DEFAULT_IMAGE)
    _run(["docker", "version"], timeout=30)
    if os.environ.get("MAESTRO_CONTAINER_SKIP_BUILD") != "1":
        _run(["docker", "build", "--tag", image, "."], timeout=600)
    _run(["docker", "image", "inspect", image], timeout=30)
    return image


@pytest.fixture(scope="session")
def container_test_root() -> Generator[Path]:
    """Keep one stable, session-owned parent visible to macOS VM file sharing."""

    _TEST_ROOT.mkdir(exist_ok=True)
    session_root = _TEST_ROOT / f"session-{uuid.uuid4().hex}"
    session_root.mkdir()
    try:
        yield session_root
    finally:
        shutil.rmtree(session_root, ignore_errors=True)
        with suppress(OSError):
            _TEST_ROOT.rmdir()


@pytest.fixture
def mounted_repository(container_test_root: Path) -> Generator[Path]:
    """Create an isolated repository below the stable shared parent."""

    destination = container_test_root / f"repository with spaces-{uuid.uuid4().hex}"
    shutil.copytree(_FIXTURE, destination)
    try:
        yield destination
    finally:
        shutil.rmtree(destination, ignore_errors=True)


@pytest.fixture
def hardened_command(container_image: str) -> CommandFactory:
    """Resolve the authoritative launcher profile without starting Docker."""

    def factory(
        repository: Path,
        name: str | None = None,
        extra_environment: Mapping[str, str] | None = None,
    ) -> list[str]:
        uid = os.getuid() or 65532
        gid = os.getgid() or 65532
        environment = {
            "MAESTRO_ALLOWED_ROOTS": str(repository),
            "MAESTRO_DOCKER_GID": str(gid),
            "MAESTRO_DOCKER_IMAGE": container_image,
            "MAESTRO_DOCKER_UID": str(uid),
            "MAESTRO_LOG_LEVEL": "WARNING",
            "PATH": os.environ["PATH"],
        }
        if extra_environment is not None:
            environment.update(extra_environment)
        launcher = [sys.executable, str(_LAUNCHER), "--dry-run"]
        if name is not None:
            launcher.extend(("--name", name))
        completed = subprocess.run(
            launcher,
            cwd=_PROJECT_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw_command: object = json.loads(completed.stdout)
        if not isinstance(raw_command, list):
            raise TypeError("launcher did not return a string argument vector")
        command: list[str] = []
        for item in cast(list[object], raw_command):
            if not isinstance(item, str):
                raise TypeError("launcher did not return a string argument vector")
            command.append(item)
        return command

    return factory
