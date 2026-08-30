from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import TextContent

pytestmark = [pytest.mark.container, pytest.mark.timeout(600)]

_PROJECT_ROOT = Path(__file__).parents[2]
_PYTHON = "/opt/maestro/.venv/bin/python"
CommandFactory = Callable[[Path, str | None, Mapping[str, str] | None], list[str]]


def _run(command: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=_PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed with status {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed


def _with_entrypoint(command: list[str], entrypoint: str, *arguments: str) -> list[str]:
    return [*command[:-1], "--entrypoint", entrypoint, command[-1], *arguments]


def test_official_dockerfile_checks_pass() -> None:
    _run(["docker", "build", "--check", "."], timeout=300)


def test_runtime_image_metadata_is_minimal_and_non_root(container_image: str) -> None:
    image = json.loads(_run(["docker", "image", "inspect", container_image]).stdout)[0]
    config = image["Config"]

    assert config["User"] == "65532:65532"
    assert config["Entrypoint"] == ["/opt/maestro/.venv/bin/maestro"]
    assert not config.get("ExposedPorts")
    assert not config.get("Volumes")
    serialized = json.dumps(config)
    assert "MAESTRO_CODEX_API_KEY" not in serialized
    assert "MAESTRO_CODEX_AUTH_FILE" not in serialized
    assert "MAESTRO_AUDIT_WRITER_PASSWORD_FILE" not in serialized
    assert "MAESTRO_AUDIT_MIGRATION_PASSWORD_FILE" not in serialized
    assert "MAESTRO_AUDIT_READER_PASSWORD_FILE" not in serialized
    assert "MAESTRO_AUDIT_BOOTSTRAP_PASSWORD_FILE" not in serialized


def test_runtime_kernel_state_root_tmpfs_and_production_dependencies(
    mounted_repository: Path,
    hardened_command: CommandFactory,
) -> None:
    script = """
import importlib.metadata
import json
import os
import pathlib
import shutil
import subprocess

status = {}
for line in pathlib.Path('/proc/self/status').read_text().splitlines():
    if ':' in line:
        key, value = line.split(':', 1)
        status[key] = value.strip()
root_blocked = False
try:
    pathlib.Path('/opt/maestro/write-probe').write_text('blocked')
except OSError:
    root_blocked = True
tmp = pathlib.Path('/tmp/write-probe')
tmp.write_text('ok')
executable = pathlib.Path('/tmp/true')
shutil.copyfile('/bin/true', executable)
executable.chmod(0o700)
noexec_blocked = False
try:
    subprocess.run([str(executable)], check=True)
except OSError:
    noexec_blocked = True
packages = {item.metadata['Name'].lower() for item in importlib.metadata.distributions()}
system_site_packages = pathlib.Path('/usr/local/lib/python3.13/site-packages')
forbidden_paths = [
    pathlib.Path('/opt/maestro/.git'),
    pathlib.Path('/opt/maestro/.env'),
    pathlib.Path('/opt/maestro/.coverage'),
    pathlib.Path('/opt/maestro/.pytest_cache'),
    pathlib.Path('/opt/maestro/tests'),
    pathlib.Path('/root/.codex'),
    pathlib.Path('/root/.ssh'),
    pathlib.Path('/root/.aws'),
    pathlib.Path('/root/.config'),
    pathlib.Path('/var/run/docker.sock'),
]
def accessible(path):
    try:
        return path.exists()
    except PermissionError:
        return False
print(json.dumps({
    'uid': os.geteuid(),
    'cap_eff': status.get('CapEff'),
    'no_new_privs': status.get('NoNewPrivs'),
    'seccomp': status.get('Seccomp'),
    'root_blocked': root_blocked,
    'tmp_writable': tmp.read_text() == 'ok',
    'tmp_noexec': noexec_blocked,
    'dev_packages': sorted(packages & {'pytest', 'ruff', 'pyright', 'twine'}),
    'system_pip_present': any(system_site_packages.glob('pip*')),
    'forbidden_paths': [str(path) for path in forbidden_paths if accessible(path)],
}))
"""
    completed = _run(
        _with_entrypoint(hardened_command(mounted_repository, None, None), _PYTHON, "-c", script)
    )
    result = json.loads(completed.stdout)

    assert result == {
        "uid": os.getuid() or 65532,
        "cap_eff": "0000000000000000",
        "no_new_privs": "1",
        "seccomp": "2",
        "root_blocked": True,
        "tmp_writable": True,
        "tmp_noexec": True,
        "dev_packages": [],
        "system_pip_present": False,
        "forbidden_paths": [],
    }


def test_repository_mount_is_readable_recursively_readonly_and_unchanged(
    mounted_repository: Path,
    hardened_command: CommandFactory,
) -> None:
    existing = mounted_repository / "README.md"
    before = existing.read_bytes()
    script = """
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
existing = root / 'README.md'
results = {'read': bool(existing.read_text())}
operations = {
    'create': lambda: (root / 'created.txt').write_text('created'),
    'modify': lambda: existing.write_text('changed'),
    'delete': existing.unlink,
}
for name, operation in operations.items():
    try:
        operation()
    except OSError:
        results[name] = 'blocked'
    else:
        results[name] = 'allowed'
print(json.dumps(results))
"""
    completed = _run(
        _with_entrypoint(
            hardened_command(mounted_repository, None, None),
            _PYTHON,
            "-c",
            script,
            str(mounted_repository),
        )
    )

    assert json.loads(completed.stdout) == {
        "read": True,
        "create": "blocked",
        "modify": "blocked",
        "delete": "blocked",
    }
    assert existing.read_bytes() == before
    assert not (mounted_repository / "created.txt").exists()


def test_git_fingerprint_control_operates_inside_hardened_container(
    mounted_repository: Path,
    hardened_command: CommandFactory,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.fail("host Git is required to prepare the synthetic container fixture")
    for arguments in (
        ("init", "--quiet"),
        ("add", "."),
        (
            "-c",
            "user.name=Maestro Test",
            "-c",
            "user.email=maestro@example.invalid",
            "commit",
            "--quiet",
            "--message=fixture",
        ),
    ):
        subprocess.run(
            [git, *arguments],
            cwd=mounted_repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    script = """
import asyncio
import json
import os
import pathlib
import sys

# Audit is mandatory configuration. This probe never reaches PostgreSQL, so it stages an
# owner-only credential on the container's own tmpfs, where ownership is kernel-enforced.
_credential = pathlib.Path('/tmp/audit-writer-password')
_credential.write_text('container-probe-password', encoding='utf-8')
_credential.chmod(0o400)
os.environ['MAESTRO_AGENT_RUNTIME'] = 'codex'
os.environ['MAESTRO_AUDIT_WRITER_USER'] = 'maestro_audit_writer'
os.environ['MAESTRO_AUDIT_WRITER_PASSWORD_FILE'] = str(_credential)

from maestro.config import Settings
from maestro.repository.guard import RepositoryGuard

async def inspect(repository):
    guard = RepositoryGuard(Settings())
    fingerprint = await guard.fingerprint(guard.authorize(str(repository)))
    return {
        'git_top_level': fingerprint.git_top_level_id is not None,
        'head': fingerprint.head is not None,
        'dirty': fingerprint.dirty_digest is not None,
    }

print(json.dumps(asyncio.run(inspect(pathlib.Path(sys.argv[1])))))
"""
    completed = _run(
        _with_entrypoint(
            hardened_command(mounted_repository, None, None),
            _PYTHON,
            "-c",
            script,
            str(mounted_repository),
        )
    )

    assert json.loads(completed.stdout) == {
        "git_top_level": True,
        "head": True,
        "dirty": True,
    }


def test_launcher_applies_inspectable_privilege_network_mount_and_resource_policy(
    mounted_repository: Path,
    hardened_command: CommandFactory,
) -> None:
    name = f"maestro-policy-{uuid.uuid4().hex}"
    command = hardened_command(mounted_repository, name, None)
    command.remove("--rm")
    command.remove("--interactive")
    command = [
        *command[:-1],
        "--detach",
        "--entrypoint",
        _PYTHON,
        command[-1],
        "-c",
        "import time; time.sleep(120)",
    ]
    try:
        _run(command)
        inspected = json.loads(_run(["docker", "inspect", name]).stdout)[0]
        host = inspected["HostConfig"]
        config = inspected["Config"]
        mounts = inspected["Mounts"]

        assert host["ReadonlyRootfs"] is True
        assert host["Privileged"] is False
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges=true" in host["SecurityOpt"]
        assert host["NetworkMode"] == "bridge"
        assert host["PidMode"] == ""
        assert host["IpcMode"] == "private"
        assert host["UsernsMode"] == ""
        assert host["Devices"] == []
        assert not host.get("PortBindings")
        assert host["Memory"] == 2 * 1024**3
        assert host["NanoCpus"] == 2_000_000_000
        assert host["PidsLimit"] == 256
        assert host["Init"] is True
        assert config["User"] == f"{os.getuid() or 65532}:{os.getgid() or 65532}"
        assert all(mount["RW"] is False for mount in mounts)
        assert any(mount["Source"] == str(mounted_repository) for mount in mounts)
        assert all(mount["Destination"] != "/var/run/docker.sock" for mount in mounts)
        assert inspected["NetworkSettings"]["Ports"] == {}
        assert inspected["AppArmorProfile"] in {"", "docker-default"}
        assert "seccomp=unconfined" not in host["SecurityOpt"]
        assert "apparmor=unconfined" not in host["SecurityOpt"]
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            cwd=_PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.mark.asyncio
async def test_mcp_stdio_discovery_and_authorization_survive_audit_outage(
    mounted_repository: Path,
    container_test_root: Path,
    hardened_command: CommandFactory,
) -> None:
    """The direct launcher has no database, so an audited call must fail closed.

    ADR-0005 requires tool discovery, protocol negotiation, and authorization to stay
    available while Audit is unreachable; only the audited execution fails.
    """

    credential = container_test_root / f"writer-password-{uuid.uuid4().hex}"
    credential.write_text("direct-launcher-password", encoding="utf-8")
    credential.chmod(0o600)
    command = hardened_command(
        mounted_repository,
        None,
        {
            "MAESTRO_AGENT_RUNTIME": "codex",
            "MAESTRO_AUDIT_WRITER_USER": "maestro_audit_writer",
            "MAESTRO_AUDIT_WRITER_PASSWORD_FILE": str(credential),
        },
    )
    parameters = StdioServerParameters(
        command=command[0],
        args=command[1:],
        env=dict(os.environ),
    )
    async with Client(parameters, read_timeout_seconds=60) as client:
        tools = await client.list_tools()
        audited = await client.call_tool(
            "resolve_codebase_fact",
            {
                "repository_path": str(mounted_repository),
                "question": "Should this fixture choose a different payment representation?",
            },
        )
        unauthorized = await client.call_tool(
            "resolve_codebase_fact",
            {
                "repository_path": str(mounted_repository.parent),
                "question": "Is this path allowed?",
            },
        )

    assert [tool.name for tool in tools.tools] == ["resolve_codebase_fact"]
    assert audited.is_error is True
    audited_content = audited.content[0]
    assert isinstance(audited_content, TextContent)
    assert "AUDIT_UNAVAILABLE" in audited_content.text
    assert unauthorized.is_error is True
    error_content = unauthorized.content[0]
    assert isinstance(error_content, TextContent)
    assert "REPOSITORY_NOT_ALLOWED" in error_content.text


def test_attached_server_terminates_and_named_container_is_removed(
    mounted_repository: Path,
    hardened_command: CommandFactory,
) -> None:
    name = f"maestro-signal-{uuid.uuid4().hex}"
    command = hardened_command(mounted_repository, name, None)
    process = subprocess.Popen(
        command,
        cwd=_PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if inspected.returncode == 0 and inspected.stdout.strip() == "true":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("container did not reach running state")

        process.terminate()
        process.communicate(timeout=15)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (
                subprocess.run(
                    ["docker", "inspect", name],
                    check=False,
                    capture_output=True,
                    timeout=5,
                ).returncode
                != 0
            ):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("container remained after attached process termination")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        else:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        subprocess.run(
            ["docker", "rm", "--force", name],
            cwd=_PROJECT_ROOT,
            check=False,
            capture_output=True,
            timeout=30,
        )
