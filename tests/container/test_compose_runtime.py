from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest
from mcp import Client, StdioServerParameters

from maestro.capabilities.resolve_codebase_fact.contracts import (
    VerificationResult,
    VerificationStatus,
)

_PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
from scripts import maestro_compose  # noqa: E402

sys.path.pop(0)
_COMPOSE_FILE = _PROJECT_ROOT / "compose.yaml"
_COMPOSE_LAUNCHER = _PROJECT_ROOT / "scripts" / "maestro_compose.py"
_POSTGRES_IMAGE = (
    "postgres:18.6-trixie@sha256:1957b2ff3137e4ef7f3bc813e74fff50b1e1ffddc85c8b9d6f14ade972be8687"
)
# Role credentials reach non-root services as owner-preserving read-only bind mounts, because
# Docker Desktop materializes Compose file secrets as root-owned copies. PostgreSQL keeps the
# official file-secret path, which its entrypoint consumes as root.
_CREDENTIAL_ROOT = "/run/maestro-credentials"
_POSTGRES_BOOTSTRAP_TARGET = "/run/secrets/audit-bootstrap-password"
_EXPECTED_ROLE_TARGETS = {
    role: f"{_CREDENTIAL_ROOT}/audit-{role}-password"
    for role in ("bootstrap", "migration", "writer", "reader")
}

pytestmark = [pytest.mark.container, pytest.mark.timeout(600)]


def _run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=_PROJECT_ROOT,
        env=None if environment is None else dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed with status {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed


def _docker_inspect(name: str) -> dict[str, object]:
    raw: object = json.loads(_run(("docker", "inspect", name)).stdout)
    assert isinstance(raw, list)
    items = cast(list[object], raw)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, dict)
    return cast(dict[str, object], item)


def _network_inspect(name: str) -> dict[str, object]:
    raw: object = json.loads(_run(("docker", "network", "inspect", name)).stdout)
    assert isinstance(raw, list)
    items = cast(list[object], raw)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, dict)
    return cast(dict[str, object], item)


def _assert_common_host_policy(host: dict[str, object]) -> None:
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["Init"] is True
    assert host["PidMode"] == ""
    assert host["IpcMode"] in {"", "private"}
    assert host["UsernsMode"] == ""
    assert host["Devices"] is None or host["Devices"] == []
    assert "no-new-privileges=true" in cast(list[str], host["SecurityOpt"])
    assert "seccomp=unconfined" not in cast(list[str], host["SecurityOpt"])
    assert "apparmor=unconfined" not in cast(list[str], host["SecurityOpt"])


def _secret(path: Path) -> str:
    value = f"maestro-compose-test-{secrets.token_urlsafe(32)}"
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


def _environment(
    *,
    image: str,
    project: str,
    repository: Path,
    secret_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    environment = {
        "HOME": os.environ.get("HOME", str(secret_root)),
        "MAESTRO_ALLOWED_ROOTS": str(repository),
        "MAESTRO_DOCKER_GID": str(os.getgid() or 65532),
        "MAESTRO_DOCKER_IMAGE": image,
        "MAESTRO_DOCKER_PROJECT": project,
        "MAESTRO_DOCKER_UID": str(os.getuid() or 65532),
        "MAESTRO_LOG_LEVEL": "WARNING",
        "PATH": os.environ["PATH"],
    }
    values: dict[str, str] = {}
    for role in _EXPECTED_ROLE_TARGETS:
        path = secret_root / f"{role}-password"
        values[role] = _secret(path)
        environment[f"MAESTRO_AUDIT_{role.upper()}_PASSWORD_FILE"] = str(path)
    return environment, values


def _launcher(
    environment: Mapping[str, str],
    action: str,
    *arguments: str,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return _run(
        (sys.executable, str(_COMPOSE_LAUNCHER), action, *arguments),
        environment=environment,
        check=check,
        timeout=timeout,
    )


def _psql(container: str, statement: str) -> str:
    return _run(
        (
            "docker",
            "exec",
            "--user",
            "999:999",
            container,
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--username",
            "postgres",
            "--dbname",
            "maestro",
            "--command",
            statement,
        ),
        timeout=30,
    ).stdout.strip()


def _wait_healthy(container: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        inspected = _docker_inspect(container)
        state = cast(dict[str, object], inspected["State"])
        health = cast(dict[str, object], state.get("Health", {}))
        if health.get("Status") == "healthy":
            return
        time.sleep(0.25)
    raise AssertionError("PostgreSQL did not become healthy")


def _assert_no_secret_values(text: str, values: Mapping[str, str]) -> None:
    assert all(value not in text for value in values.values())


def _assert_service_secret_projection(
    environment: Mapping[str, str],
    *,
    action: str,
    service: str,
    expected_roles: set[str],
    tmp_path: Path,
) -> None:
    dry_run = _launcher(environment, action, "--dry-run")
    raw_command: object = json.loads(dry_run.stdout)
    assert isinstance(raw_command, list)
    command = [cast(str, item) for item in cast(list[object], raw_command)]
    override_index = command.index("--file", command.index("--file") + 2) + 1
    assert command[override_index].startswith("/dev/fd/")

    # The production launcher keeps its override unlinked. Reconstruct the reviewed
    # action configuration in a private test file solely to inspect effective mounts.
    configuration = maestro_compose.load_configuration(
        environment,
        maestro_compose.ComposeOptions(action=maestro_compose.DeploymentAction(action)),
    )
    override = tmp_path / f"{action}-override.json"
    override.write_text(
        json.dumps(maestro_compose.build_compose_override(configuration)), encoding="utf-8"
    )
    override.chmod(0o600)
    # Assert the credential state Maestro actually consumes: the guard re-materializes each
    # mounted credential on tmpfs, where ownership and mode are kernel-enforced.
    probe = (
        "import json,os,pathlib,stat,sys;"
        "sys.path.insert(0,'/opt/maestro');"
        "import maestro_mount_guard as guard;"
        "env=dict(os.environ);"
        "guard.stage_credentials(env);"
        "print(json.dumps({pathlib.Path(v).name:"
        "[stat.S_IMODE(os.stat(v).st_mode),os.stat(v).st_uid] "
        "for k,v in env.items() if k.endswith('_PASSWORD_FILE')}))"
    )
    compose_environment = maestro_compose.compose_environment(configuration, environment)
    completed = _run(
        (
            "docker",
            "compose",
            "--ansi",
            "never",
            "--project-name",
            configuration.project,
            "--file",
            str(_COMPOSE_FILE),
            "--file",
            str(override),
            "run",
            "--rm",
            "--no-deps",
            "--no-TTY",
            "--entrypoint",
            "/opt/maestro/.venv/bin/python",
            service,
            "-c",
            probe,
        ),
        environment=compose_environment,
        timeout=60,
    )
    effective: object = json.loads(completed.stdout)
    assert isinstance(effective, dict)
    expected_names = {f"audit-{role}-password" for role in expected_roles}
    assert set(cast(dict[str, object], effective)) == expected_names
    for name, metadata in cast(dict[str, list[int]], effective).items():
        assert name in expected_names
        assert metadata == [0o400, configuration.uid]


@pytest.fixture
def compose_deployment(
    container_image: str,
    mounted_repository: Path,
    container_test_root: Path,
) -> Generator[tuple[dict[str, str], dict[str, str], str]]:
    project = f"maestro-i13-{uuid.uuid4().hex[:12]}"
    secret_root = container_test_root / f"secrets-{uuid.uuid4().hex}"
    secret_root.mkdir(mode=0o700)
    environment, values = _environment(
        image=container_image,
        project=project,
        repository=mounted_repository,
        secret_root=secret_root,
    )
    try:
        yield environment, values, project
    finally:
        _launcher(environment, "down", check=False, timeout=60)
        _run(("docker", "volume", "rm", f"{project}_audit-postgres-data"), check=False)


@pytest.mark.asyncio
async def test_compose_audit_deployment_is_hardened_private_durable_and_fail_closed(  # noqa: PLR0915
    compose_deployment: tuple[dict[str, str], dict[str, str], str],
    mounted_repository: Path,
    tmp_path: Path,
) -> None:
    environment, values, project = compose_deployment
    postgres_name = f"{project}-audit-postgres-1"

    _launcher(environment, "database-up", timeout=120)
    _wait_healthy(postgres_name)
    postgres = _docker_inspect(postgres_name)
    postgres_host = cast(dict[str, object], postgres["HostConfig"])
    postgres_config = cast(dict[str, object], postgres["Config"])
    postgres_network = cast(dict[str, object], postgres["NetworkSettings"])
    postgres_mounts = cast(list[dict[str, object]], postgres["Mounts"])
    assert postgres_config["Image"] == _POSTGRES_IMAGE
    assert postgres["AppArmorProfile"] in {"", "docker-default"}
    _assert_common_host_policy(postgres_host)
    assert postgres_host["CapDrop"] == ["ALL"]
    assert {value.removeprefix("CAP_") for value in cast(list[str], postgres_host["CapAdd"])} == {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "SETGID",
        "SETUID",
    }
    assert not postgres_host.get("PortBindings")
    assert postgres_host["Memory"] == 1024**3
    assert postgres_host["NanoCpus"] == 1_000_000_000
    assert postgres_host["PidsLimit"] == 128
    assert "/run/postgresql" in cast(dict[str, object], postgres_host["Tmpfs"])
    assert "/tmp" in cast(dict[str, object], postgres_host["Tmpfs"])  # noqa: S108
    assert all(
        bindings is None for bindings in cast(dict[str, object], postgres_network["Ports"]).values()
    )
    assert set(cast(dict[str, object], postgres_network["Networks"])) == {
        f"{project}_audit-internal"
    }
    assert any(
        mount["Type"] == "volume"
        and mount["Destination"] == "/var/lib/postgresql"
        and mount["RW"] is True
        for mount in postgres_mounts
    )
    postgres_secret_mounts = {
        cast(str, mount["Destination"])
        for mount in postgres_mounts
        if cast(str, mount["Destination"]).startswith("/run/secrets/")
    }
    assert postgres_secret_mounts == {_POSTGRES_BOOTSTRAP_TARGET}
    bootstrap_projection = _run(
        (
            "docker",
            "exec",
            postgres_name,
            "stat",
            "--format=%a:%u",
            _POSTGRES_BOOTSTRAP_TARGET,
        )
    ).stdout.strip()
    # Owner-only is the security property to enforce. The owning identity is an engine
    # detail that legitimately differs — native Linux Docker preserves the host owner while
    # Docker Desktop projects a root-owned copy into its virtual machine — which is exactly
    # why role credentials for non-root services use bind mounts instead of file secrets.
    # PostgreSQL reaching a healthy state above already proves it could read this file.
    bootstrap_mode, _, bootstrap_owner = bootstrap_projection.partition(":")
    assert bootstrap_mode == "600", bootstrap_projection
    assert bootstrap_owner in {"0", str(os.getuid())}, bootstrap_projection
    # The daemon parses ps output positionally and requires an explicit PID column.
    postgres_processes = _run(("docker", "top", postgres_name, "-eo", "pid,uid,comm")).stdout
    assert "999 postgres" in " ".join(postgres_processes.split())
    assert not any(mount["Destination"] == "/var/run/docker.sock" for mount in postgres_mounts)

    audit_network = _network_inspect(f"{project}_audit-internal")
    assert audit_network["Internal"] is True
    assert audit_network["Driver"] == "bridge"
    assert _psql(postgres_name, "SHOW server_version") == "18.6 (Debian 18.6-1.pgdg13+2)"
    assert _psql(postgres_name, "SHOW data_directory") == "/var/lib/postgresql/18/docker"
    host_methods = _psql(
        postgres_name,
        "SELECT DISTINCT auth_method FROM pg_hba_file_rules "
        "WHERE type IN ('host','hostssl','hostnossl') ORDER BY 1",
    ).splitlines()
    assert host_methods == ["scram-sha-256"]

    for action, service, expected in (
        ("bootstrap", "audit-bootstrap", set(_EXPECTED_ROLE_TARGETS)),
        ("migrate", "audit-migrate", {"migration"}),
        ("read", "audit-reader", {"reader"}),
    ):
        _assert_service_secret_projection(
            environment,
            action=action,
            service=service,
            expected_roles=expected,
            tmp_path=tmp_path,
        )

    bootstrap = _launcher(environment, "bootstrap", timeout=120)
    migration = _launcher(environment, "migrate", timeout=120)
    _assert_no_secret_values(bootstrap.stdout + bootstrap.stderr, values)
    _assert_no_secret_values(migration.stdout + migration.stderr, values)
    assert _psql(postgres_name, "SELECT version FROM audit.schema_version") == "3"
    assert (
        _psql(
            postgres_name,
            "SELECT count(*) FROM pg_authid WHERE rolname LIKE 'maestro_audit_%' "
            "AND rolpassword LIKE 'SCRAM-SHA-256$%'",
        )
        == "3"
    )
    activity = _psql(
        postgres_name,
        "SELECT coalesce(string_agg(query, ''), '') FROM pg_stat_activity",
    )
    logs = _run(("docker", "logs", postgres_name)).stdout
    _assert_no_secret_values(activity + logs, values)

    server_name = f"{project}-maestro"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(_COMPOSE_LAUNCHER), "server", "--name", server_name],
        env=environment,
    )
    async with Client(parameters, read_timeout_seconds=30) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == ["resolve_codebase_fact"]
        # Compose creates the egress network with the server, not with the database.
        provider_network = _network_inspect(f"{project}_provider-egress")
        assert provider_network["Internal"] is False
        assert provider_network["Driver"] == "bridge"
        inspected = _docker_inspect(server_name)
        host = cast(dict[str, object], inspected["HostConfig"])
        config = cast(dict[str, object], inspected["Config"])
        network = cast(dict[str, object], inspected["NetworkSettings"])
        mounts = cast(list[dict[str, object]], inspected["Mounts"])
        _assert_common_host_policy(host)
        assert host["CapDrop"] == ["ALL"]
        assert host["Memory"] == 2 * 1024**3
        assert host["NanoCpus"] == 2_000_000_000
        assert host["PidsLimit"] == 256
        assert host["Init"] is True
        assert "/tmp" in cast(dict[str, object], host["Tmpfs"])  # noqa: S108
        assert not host.get("PortBindings")
        assert config["User"] == f"{os.getuid() or 65532}:{os.getgid() or 65532}"
        assert inspected["AppArmorProfile"] in {"", "docker-default"}
        assert set(cast(dict[str, object], network["Networks"])) == {
            f"{project}_audit-internal",
            f"{project}_provider-egress",
        }
        assert all(mount["RW"] is False for mount in mounts)
        assert any(mount["Source"] == str(mounted_repository) for mount in mounts)
        assert all(mount["Destination"] != "/var/run/docker.sock" for mount in mounts)
        secret_targets = {
            cast(str, mount["Destination"])
            for mount in mounts
            if cast(str, mount["Destination"]).startswith(f"{_CREDENTIAL_ROOT}/")
            or cast(str, mount["Destination"]).startswith("/run/secrets/")
        }
        assert secret_targets == {_EXPECTED_ROLE_TARGETS["writer"]}
        serialized = json.dumps(config)
        assert "MAESTRO_AUDIT_WRITER_PASSWORD_FILE" in serialized
        assert "MAESTRO_AUDIT_BOOTSTRAP_" not in serialized
        assert "MAESTRO_AUDIT_MIGRATION_" not in serialized
        assert "MAESTRO_AUDIT_READER_" not in serialized
        _assert_no_secret_values(serialized, values)

        completed = await client.call_tool(
            "resolve_codebase_fact",
            {
                "repository_path": str(mounted_repository),
                "question": "Should this fixture choose a different payment representation?",
            },
        )
        assert completed.is_error is False
        validated = VerificationResult.model_validate_json(
            json.dumps(completed.structured_content), strict=True
        )
        assert validated.status is VerificationStatus.HUMAN_DECISION_REQUIRED

        _run(("docker", "stop", postgres_name), timeout=60)
        assert [tool.name for tool in (await client.list_tools()).tools] == [
            "resolve_codebase_fact"
        ]
        unavailable = await client.call_tool(
            "resolve_codebase_fact",
            {
                "repository_path": str(mounted_repository),
                "question": "What exact payment representation does the fixture use?",
            },
        )
        assert unavailable.is_error is True
        assert "AUDIT_UNAVAILABLE" in str(unavailable.content)

    outage_name = f"{project}-maestro-outage"
    outage_parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(_COMPOSE_LAUNCHER), "server", "--name", outage_name],
        env=environment,
    )
    async with Client(outage_parameters, read_timeout_seconds=30) as outage_client:
        assert [tool.name for tool in (await outage_client.list_tools()).tools] == [
            "resolve_codebase_fact"
        ]

    _run(("docker", "start", postgres_name))
    _wait_healthy(postgres_name)
    assert _psql(postgres_name, "SELECT count(*) FROM audit.executions") == "1"
    assert _psql(postgres_name, "SELECT count(*) FROM audit.events") == "2"
    reader = _launcher(environment, "read", "--view", "summary", timeout=60)
    row: object = json.loads(reader.stdout)
    assert isinstance(row, dict)
    assert cast(dict[str, object], row)["outcome"] == "human_decision_required"
    _assert_no_secret_values(reader.stdout + reader.stderr, values)

    _launcher(environment, "down", timeout=60)
    volume_name = f"{project}_audit-postgres-data"
    assert _docker_inspect(volume_name)["Name"] == volume_name
    assert _run(("docker", "inspect", postgres_name), check=False).returncode != 0
    assert (
        _run(("docker", "network", "inspect", f"{project}_audit-internal"), check=False).returncode
        != 0
    )
    _launcher(environment, "database-up", timeout=120)
    _wait_healthy(postgres_name)
    assert _psql(postgres_name, "SELECT count(*) FROM audit.events") == "2"


@pytest.mark.enable_socket
def test_compose_loopback_publication_and_mount_guard_are_fail_closed(
    compose_deployment: tuple[dict[str, str], dict[str, str], str],
    mounted_repository: Path,
    tmp_path: Path,
) -> None:
    environment, values, project = compose_deployment
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = cast(tuple[str, int], listener.getsockname())[1]
    _launcher(environment, "database-up", "--publish-loopback", str(port), timeout=120)
    postgres_name = f"{project}-audit-postgres-1"
    binding = cast(dict[str, object], _docker_inspect(postgres_name)["HostConfig"])["PortBindings"]
    assert binding == {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}

    nested = mounted_repository / "nested"
    nested.mkdir()
    configuration = maestro_compose.load_configuration(
        environment,
        maestro_compose.ComposeOptions(action=maestro_compose.DeploymentAction.SERVER),
    )
    override_data = maestro_compose.build_compose_override(configuration)
    services = cast(dict[str, object], override_data["services"])
    service = cast(dict[str, object], services["maestro"])
    volumes = cast(list[dict[str, object]], service["volumes"])
    volumes.append(
        {
            "type": "bind",
            "source": str(nested),
            "target": str(nested),
            "read_only": False,
            "bind": {"create_host_path": False, "propagation": "rprivate"},
        }
    )
    override = tmp_path / "nested-write-override.json"
    override.write_text(json.dumps(override_data), encoding="utf-8")
    override.chmod(0o600)
    completed = _run(
        (
            "docker",
            "compose",
            "--ansi",
            "never",
            "--project-name",
            project,
            "--file",
            str(_COMPOSE_FILE),
            "--file",
            str(override),
            "run",
            "--rm",
            "--no-deps",
            "--no-TTY",
            "maestro",
        ),
        environment=maestro_compose.compose_environment(configuration, environment),
        check=False,
        timeout=60,
    )
    assert completed.returncode == 78
    assert completed.stdout == ""
    assert "Maestro container startup validation failed" in completed.stderr
    _assert_no_secret_values(completed.stderr, values)
    with suppress(OSError):
        nested.rmdir()
