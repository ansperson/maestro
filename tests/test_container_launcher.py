from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_LAUNCHER = _PROJECT_ROOT / "scripts" / "maestro_container.py"


def _run_launcher(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_LAUNCHER), "--dry-run", *arguments],
        cwd=_PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_launcher_builds_safe_hardened_argument_vector_for_paths_with_spaces(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "allowed repository"
    repository.mkdir()
    auth_file = tmp_path / "auth file.json"
    auth_file.write_text("{}", encoding="utf-8")
    forwarded_value = "synthetic-" + "api-key-not-for-use"
    environment = {
        "MAESTRO_ALLOWED_ROOTS": str(repository),
        "MAESTRO_CODEX_API_KEY": forwarded_value,
        "MAESTRO_CODEX_AUTH_FILE": str(auth_file),
        "MAESTRO_DOCKER_GID": "43210",
        "MAESTRO_DOCKER_UID": "12345",
        "MAESTRO_LOG_LEVEL": "WARNING",
        "MAESTRO_UNRECOGNIZED": "must-not-be-forwarded",
        "PATH": os.environ["PATH"],
    }

    completed = _run_launcher(environment, "--image", "maestro-verifier:test", "--name", "safe")

    assert completed.returncode == 0, completed.stderr
    raw_command: object = json.loads(completed.stdout)
    assert isinstance(raw_command, list)
    command: list[str] = []
    for item in cast(list[object], raw_command):
        assert isinstance(item, str)
        command.append(item)
    assert command[:2] == ["docker", "run"]
    assert command[-1] == "maestro-verifier:test"
    assert "--read-only" in command
    assert command[command.index("--user") + 1] == "12345:43210"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges=true" in command
    assert command[command.index("--network") + 1] == "bridge"
    assert command[command.index("--memory") + 1] == "2g"
    assert command[command.index("--cpus") + 1] == "2"
    assert command[command.index("--pids-limit") + 1] == "256"
    assert any(
        value
        == (
            f"type=bind,src={repository},dst={repository},readonly,"
            "bind-recursive=readonly,bind-propagation=rprivate"
        )
        for value in command
    )
    assert any(str(auth_file) in value and value.endswith(",readonly") for value in command)
    assert forwarded_value not in completed.stdout
    assert "MAESTRO_CODEX_API_KEY" in command
    assert "MAESTRO_UNRECOGNIZED" not in completed.stdout


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MAESTRO_DOCKER_UID", "0", "MAESTRO_DOCKER_UID is invalid"),
        ("MAESTRO_DOCKER_MEMORY", "unlimited", "MAESTRO_DOCKER_MEMORY is invalid"),
        ("MAESTRO_DOCKER_PIDS_LIMIT", "8", "MAESTRO_DOCKER_PIDS_LIMIT is invalid"),
    ],
)
def test_launcher_rejects_unsafe_deployment_values(
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    environment = {
        "MAESTRO_ALLOWED_ROOTS": str(tmp_path),
        "MAESTRO_DOCKER_GID": "12345",
        "MAESTRO_DOCKER_UID": "12345",
        "PATH": os.environ["PATH"],
        name: value,
    }

    completed = _run_launcher(environment)

    assert completed.returncode == 2
    assert message in completed.stderr
    assert completed.stdout == ""


def test_launcher_rejects_mount_grammar_and_auth_symlinks(tmp_path: Path) -> None:
    comma_root = tmp_path / "unsafe,root"
    comma_root.mkdir()
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_link = tmp_path / "auth-link.json"
    auth_link.symlink_to(auth_file)
    base_environment = {
        "MAESTRO_DOCKER_GID": "12345",
        "MAESTRO_DOCKER_UID": "12345",
        "PATH": os.environ["PATH"],
    }

    comma = _run_launcher({**base_environment, "MAESTRO_ALLOWED_ROOTS": str(comma_root)})
    symlink = _run_launcher(
        {
            **base_environment,
            "MAESTRO_ALLOWED_ROOTS": str(tmp_path),
            "MAESTRO_CODEX_AUTH_FILE": str(auth_link),
        }
    )

    assert comma.returncode == 2
    assert "paths containing commas are unsupported" in comma.stderr
    assert symlink.returncode == 2
    assert "must not be a symlink" in symlink.stderr
