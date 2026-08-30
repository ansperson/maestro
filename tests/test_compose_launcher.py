from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from scripts import maestro_compose as compose_module  # noqa: E402

sys.path.pop(0)

_CREDENTIAL_ROOT = "/run/maestro-credentials"


def _secret(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _environment(tmp_path: Path) -> dict[str, str]:
    repository = tmp_path / "repository with spaces"
    repository.mkdir()
    auth = _secret(tmp_path / "codex-auth.json", "{}")
    environment = {
        "MAESTRO_ALLOWED_ROOTS": str(repository),
        "MAESTRO_CODEX_AUTH_FILE": str(auth),
        "MAESTRO_DOCKER_GID": str(os.getgid() or 65532),
        "MAESTRO_DOCKER_UID": str(os.getuid() or 65532),
        "MAESTRO_LOG_LEVEL": "WARNING",
        "MAESTRO_UNRECOGNIZED": "not-forwarded",
    }
    for role in ("bootstrap", "migration", "writer", "reader"):
        path = _secret(tmp_path / f"{role}-password", f"distinct-{role}-value")
        environment[f"MAESTRO_AUDIT_{role.upper()}_PASSWORD_FILE"] = str(path)
    return environment


def _configuration(
    environment: dict[str, str],
    action: compose_module.DeploymentAction,
    *,
    reader_arguments: tuple[str, ...] = (),
    published_port: int | None = None,
) -> compose_module.ComposeConfiguration:
    return compose_module.load_configuration(
        environment,
        compose_module.ComposeOptions(
            action=action,
            reader_arguments=reader_arguments,
            published_port=published_port,
        ),
    )


def _projected_credentials(override: dict[str, object]) -> set[str]:
    """Collect every credential an action projects, by file-secret name or mount target.

    PostgreSQL consumes its bootstrap credential as root through a Compose file secret, while
    non-root services receive owner-preserving read-only bind mounts.
    """

    raw_secrets = override.get("secrets", {})
    assert isinstance(raw_secrets, dict)
    names = set(cast(dict[str, object], raw_secrets))
    services = cast(dict[str, object], override.get("services", {}))
    for raw_service in services.values():
        service = cast(dict[str, object], raw_service)
        for mount in cast(list[dict[str, object]], service.get("volumes", [])):
            target = cast(str, mount["target"])
            if target.startswith(f"{_CREDENTIAL_ROOT}/"):
                names.add(target.rsplit("/", 1)[-1])
    return names


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (compose_module.DeploymentAction.SERVER, {"audit-writer-password", "codex-auth.json"}),
        (compose_module.DeploymentAction.DATABASE_UP, {"audit-bootstrap-password"}),
        (
            compose_module.DeploymentAction.BOOTSTRAP,
            {
                "audit-bootstrap-password",
                "audit-migration-password",
                "audit-writer-password",
                "audit-reader-password",
            },
        ),
        (compose_module.DeploymentAction.MIGRATE, {"audit-migration-password"}),
        (compose_module.DeploymentAction.READ, {"audit-reader-password"}),
        (compose_module.DeploymentAction.DOWN, set[str]()),
    ],
)
def test_compose_override_mounts_only_action_specific_secrets(
    tmp_path: Path,
    action: compose_module.DeploymentAction,
    expected: set[str],
) -> None:
    override = compose_module.build_compose_override(_configuration(_environment(tmp_path), action))

    assert _projected_credentials(override) == expected
    serialized = str(override)
    for role in ("bootstrap", "migration", "writer", "reader"):
        present = f"audit-{role}-password" in expected
        assert (f"audit-{role}-password" in serialized) is present


def test_server_override_preserves_validated_multi_root_and_settings_boundary(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    second_root = tmp_path / "second repository"
    second_root.mkdir()
    environment["MAESTRO_ALLOWED_ROOTS"] += os.pathsep + str(second_root)
    configuration = _configuration(environment, compose_module.DeploymentAction.SERVER)

    override = compose_module.build_compose_override(configuration)
    services = cast(dict[str, object], override["services"])
    service = cast(dict[str, object], services["maestro"])
    all_mounts = cast(list[dict[str, object]], service["volumes"])
    mounts = [
        mount
        for mount in all_mounts
        if not cast(str, mount["target"]).startswith(f"{_CREDENTIAL_ROOT}/")
    ]
    assert [mount["source"] for mount in mounts] == [
        str(Path(environment["MAESTRO_ALLOWED_ROOTS"].split(os.pathsep)[0]).resolve()),
        str(second_root.resolve()),
    ]
    assert all(mount["read_only"] is True for mount in mounts)
    assert all(
        mount["bind"] == {"create_host_path": False, "propagation": "rprivate"} for mount in mounts
    )
    service_environment = cast(dict[str, str], service["environment"])
    assert service_environment["MAESTRO_AUDIT_WRITER_HOST"] == "audit-postgres"
    assert service_environment["MAESTRO_LOG_LEVEL"] == "WARNING"
    assert "MAESTRO_UNRECOGNIZED" not in service_environment
    assert "MAESTRO_AUDIT_BOOTSTRAP_PASSWORD_FILE" not in service_environment


def test_compose_commands_are_shell_free_and_keep_database_volume_on_down(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    server = _configuration(environment, compose_module.DeploymentAction.SERVER)
    database = _configuration(environment, compose_module.DeploymentAction.DATABASE_UP)
    down = _configuration(environment, compose_module.DeploymentAction.DOWN)

    server_command = compose_module.build_command(server, "/dev/fd/7")
    database_command = compose_module.build_command(database, "/dev/fd/7")
    down_command = compose_module.build_command(down, "/dev/fd/7")

    assert server_command[:2] == ("docker", "compose")
    assert server_command[-5:] == ("run", "--rm", "--no-deps", "--no-TTY", "maestro")
    assert "--wait" in database_command
    assert database_command[-1] == "audit-postgres"
    assert down_command[-2:] == ("down", "--remove-orphans")
    assert "--volumes" not in down_command
    assert not {"sh", "bash", "-c"} & set(server_command)
    assert not any("password" in item.lower() for item in server_command)


def test_database_publication_is_loopback_only(tmp_path: Path) -> None:
    configuration = _configuration(
        _environment(tmp_path),
        compose_module.DeploymentAction.DATABASE_UP,
        published_port=55432,
    )

    override = compose_module.build_compose_override(configuration)
    services = override["services"]
    assert isinstance(services, dict)
    service = cast(dict[str, object], services["audit-postgres"])
    assert service["ports"] == [
        {
            "target": 5432,
            "published": "55432",
            "host_ip": "127.0.0.1",
            "protocol": "tcp",
            "mode": "host",
        }
    ]
    # Docker silently ignores a published port on a container attached only to an internal
    # network, so the opt-in exposure must also add a non-internal attachment.
    assert service["networks"] == ["audit-internal", "database-loopback"]
    assert override["networks"] == {"database-loopback": {"internal": False}}


def test_hardened_database_stays_internal_with_no_published_port(tmp_path: Path) -> None:
    """Without the development opt-in, PostgreSQL keeps the internal network alone."""

    configuration = _configuration(
        _environment(tmp_path), compose_module.DeploymentAction.DATABASE_UP
    )

    override = compose_module.build_compose_override(configuration)
    services = cast(dict[str, object], override["services"])
    service = cast(dict[str, object], services["audit-postgres"])
    assert "ports" not in service
    assert "networks" not in service
    assert "networks" not in override


def test_compose_launcher_rejects_secret_aliases_and_repository_exposure(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["MAESTRO_AUDIT_READER_PASSWORD_FILE"] = environment[
        "MAESTRO_AUDIT_WRITER_PASSWORD_FILE"
    ]
    with pytest.raises(compose_module.ComposeConfigurationError, match="must be distinct"):
        _configuration(environment, compose_module.DeploymentAction.BOOTSTRAP)

    second = tmp_path / "second"
    second.mkdir()
    exposed = _environment(second)
    repository = Path(exposed["MAESTRO_ALLOWED_ROOTS"])
    writer = repository / "writer-password"
    _secret(writer, "repository-visible")
    exposed["MAESTRO_AUDIT_WRITER_PASSWORD_FILE"] = str(writer)
    with pytest.raises(compose_module.ComposeConfigurationError, match="outside every allowed"):
        _configuration(exposed, compose_module.DeploymentAction.SERVER)


@pytest.mark.parametrize("mode", [0o644, 0o200])
def test_compose_launcher_rejects_insecure_audit_secret_modes(tmp_path: Path, mode: int) -> None:
    environment = _environment(tmp_path)
    Path(environment["MAESTRO_AUDIT_WRITER_PASSWORD_FILE"]).chmod(mode)

    with pytest.raises(compose_module.ComposeConfigurationError, match="owner-only"):
        _configuration(environment, compose_module.DeploymentAction.SERVER)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--audit-id", "not-a-uuid"),
        ("--repository-id", "../../private"),
        ("--view", "timeline", "--outcome", "resolved"),
        ("--outcome", "not-an-outcome"),
    ],
)
def test_reader_filters_fail_before_container_argv(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    with pytest.raises(compose_module.ComposeConfigurationError):
        _configuration(
            _environment(tmp_path),
            compose_module.DeploymentAction.READ,
            reader_arguments=arguments,
        )


def test_reader_filters_are_normalized_before_container_argv(tmp_path: Path) -> None:
    configuration = _configuration(
        _environment(tmp_path),
        compose_module.DeploymentAction.READ,
        reader_arguments=("--audit-id", "00000000000000000000000000000001"),
    )

    assert configuration.reader_arguments == (
        "--audit-id",
        "00000000-0000-0000-0000-000000000001",
    )
    assert compose_module.build_command(configuration, "/dev/fd/8")[-3:] == (
        "read",
        "--audit-id",
        "00000000-0000-0000-0000-000000000001",
    )


def test_compose_cli_environment_does_not_inherit_application_or_libpq_values(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment.update(
        {
            "PATH": os.environ["PATH"],
            "DOCKER_CONTEXT": "synthetic-context",
            "PGSERVICE": "must-not-reach-compose-child",
            "MAESTRO_AUDIT_READER_PASSWORD_FILE": environment["MAESTRO_AUDIT_READER_PASSWORD_FILE"],
        }
    )
    configuration = _configuration(environment, compose_module.DeploymentAction.SERVER)

    projected = compose_module.compose_environment(configuration, environment)

    assert projected["PATH"] == os.environ["PATH"]
    assert projected["DOCKER_CONTEXT"] == "synthetic-context"
    assert projected["MAESTRO_DOCKER_UID"] == str(os.getuid() or 65532)
    assert "PGSERVICE" not in projected
    assert "MAESTRO_AUDIT_READER_PASSWORD_FILE" not in projected
