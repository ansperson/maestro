from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

pytestmark = [pytest.mark.container, pytest.mark.e2e, pytest.mark.timeout(900)]

_PROJECT_ROOT = Path(__file__).parents[2]
_PROBE = Path(__file__).with_name("codex_write_probe.py")
_CONTAINER_PROBE = "/run/maestro-codex-write-probe.py"
_PYTHON = "/opt/maestro/.venv/bin/python"
CommandFactory = Callable[[Path, str | None, Mapping[str, str] | None], list[str]]


def _fingerprint(repository: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(repository.rglob("*")):
        digest.update(str(path.relative_to(repository)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_real_codex_cannot_persist_controlled_write_through_readonly_mount(
    mounted_repository: Path,
    hardened_command: CommandFactory,
) -> None:
    auth_file = os.environ.get("MAESTRO_CODEX_AUTH_FILE")
    api_key = os.environ.get("MAESTRO_CODEX_API_KEY")
    if auth_file is None and api_key is None:
        pytest.skip("set exactly one Codex authentication source for the live container probe")
    extra_environment: dict[str, str] = {}
    if auth_file is not None:
        extra_environment["MAESTRO_CODEX_AUTH_FILE"] = auth_file
    if api_key is not None:
        extra_environment["MAESTRO_CODEX_API_KEY"] = api_key

    target = mounted_repository / "codex-managed-edit-attempt.txt"
    before = _fingerprint(mounted_repository)
    command = hardened_command(mounted_repository, None, extra_environment)
    command = [
        *command[:-1],
        "--mount",
        f"type=bind,src={_PROBE},dst={_CONTAINER_PROBE},readonly",
        "--entrypoint",
        _PYTHON,
        command[-1],
        _CONTAINER_PROBE,
        str(mounted_repository),
        str(target),
    ]

    completed = subprocess.run(
        command,
        cwd=_PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=840,
    )

    result = json.loads(completed.stdout)
    assert result["completed"] is True
    assert result["target_exists_inside"] is False
    assert _fingerprint(mounted_repository) == before
    assert not target.exists()
