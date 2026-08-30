#!/usr/bin/env python3
"""Launch Maestro's approved local PostgreSQL deployment through Docker Compose."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

_PROJECT_ROOT = Path(__file__).parent.parent
_COMPOSE_FILE = _PROJECT_ROOT / "compose.yaml"
_DEFAULT_IMAGE = "maestro-verifier:local"
_DEFAULT_PROJECT = "maestro-audit"
_DEFAULT_MEMORY = "2g"
_DEFAULT_CPUS = "2"
_DEFAULT_PIDS_LIMIT = 256
_DEFAULT_TMPFS_SIZE = "512m"
_MAX_CPUS = 64
_MAX_PASSWORD_BYTES = 4_096
_MAX_PORT = 65_535
# Docker Desktop materializes Compose file secrets as root-owned copies inside the VM, so a
# non-root role credential delivered that way fails Maestro's owner-only validation. Role
# credentials are therefore read-only bind mounts, which preserve host ownership on every
# supported platform. PostgreSQL keeps the official root-consumed file-secret path because its
# entrypoint reads the bootstrap credential as root before dropping to the postgres user.
_CREDENTIAL_ROOT = "/run/maestro-credentials"
_CONTAINER_AUTH_FILE = f"{_CREDENTIAL_ROOT}/codex-auth.json"
_CREDENTIAL_TARGETS = {
    "bootstrap": f"{_CREDENTIAL_ROOT}/audit-bootstrap-password",
    "migration": f"{_CREDENTIAL_ROOT}/audit-migration-password",
    "writer": f"{_CREDENTIAL_ROOT}/audit-writer-password",
    "reader": f"{_CREDENTIAL_ROOT}/audit-reader-password",
}
_POSTGRES_BOOTSTRAP_TARGET = "/run/secrets/audit-bootstrap-password"
_CONTAINER_WAIT_SECONDS = 30.0
_ROLE_USERS = {
    "bootstrap": "postgres",
    "migration": "maestro_audit_migrator",
    "writer": "maestro_audit_writer",
    "reader": "maestro_audit_reader",
}
_IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,254}\Z")
_SIZE_PATTERN = re.compile(r"[1-9][0-9]*(?:[bkmgBKMG])?\Z")
_CPU_PATTERN = re.compile(r"(?:[1-9][0-9]*|0\.[0-9]*[1-9][0-9]*)\Z")
_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
_FORWARDED_SETTINGS = (
    "MAESTRO_CODEX_MODEL",
    "MAESTRO_LOG_LEVEL",
    "MAESTRO_MAX_AGENT_OUTPUT_BYTES",
    "MAESTRO_MAX_CONCURRENCY",
    "MAESTRO_MAX_CONFLICTS",
    "MAESTRO_MAX_CONTEXT_CHARS",
    "MAESTRO_MAX_EVIDENCE_ITEMS",
    "MAESTRO_MAX_FILE_BYTES",
    "MAESTRO_MAX_QUEUE_SIZE",
    "MAESTRO_MAX_QUESTION_CHARS",
    "MAESTRO_MAX_REPOSITORY_BYTES",
    "MAESTRO_MAX_REPOSITORY_FILES",
    "MAESTRO_MAX_RESULT_BYTES",
    "MAESTRO_VERIFIER_TIMEOUT_SECONDS",
)
_DOCKER_CLIENT_ENVIRONMENT = (
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
)


class DeploymentAction(StrEnum):
    SERVER = "server"
    DATABASE_UP = "database-up"
    BOOTSTRAP = "bootstrap"
    MIGRATE = "migrate"
    READ = "read"
    DOWN = "down"


class ComposeConfigurationError(ValueError):
    """A safe Compose operation cannot be derived from the environment."""


@dataclass(frozen=True, slots=True)
class ComposeConfiguration:
    """Validated host inputs for one action-specific Compose invocation."""

    action: DeploymentAction
    image: str
    project: str
    uid: int
    gid: int
    memory: str
    cpus: str
    pids_limit: int
    tmpfs_size: str
    name: str | None
    allowed_roots: tuple[Path, ...]
    auth_file: Path | None
    api_key: str | None
    forwarded_settings: tuple[tuple[str, str], ...]
    role_secrets: tuple[tuple[str, Path], ...]
    published_port: int | None
    reader_arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComposeOptions:
    """Already parsed command options kept separate from the environment boundary."""

    action: DeploymentAction
    image_override: str | None = None
    name: str | None = None
    published_port: int | None = None
    reader_arguments: tuple[str, ...] = ()


def load_configuration(
    environment: Mapping[str, str],
    options: ComposeOptions,
) -> ComposeConfiguration:
    """Validate action-specific inputs without starting Docker or resolving shell text."""

    _validate_action_options(options)
    action = options.action
    image = options.image_override or environment.get("MAESTRO_DOCKER_IMAGE", _DEFAULT_IMAGE)
    project = environment.get("MAESTRO_DOCKER_PROJECT", _DEFAULT_PROJECT)
    if _IMAGE_PATTERN.fullmatch(image) is None:
        raise ComposeConfigurationError("MAESTRO_DOCKER_IMAGE is invalid")
    if _PROJECT_PATTERN.fullmatch(project) is None:
        raise ComposeConfigurationError("MAESTRO_DOCKER_PROJECT is invalid")
    if options.name is not None and _NAME_PATTERN.fullmatch(options.name) is None:
        raise ComposeConfigurationError("container name is invalid")

    uid = _identity(environment, "MAESTRO_DOCKER_UID", os.getuid())
    gid = _identity(environment, "MAESTRO_DOCKER_GID", os.getgid())
    memory = environment.get("MAESTRO_DOCKER_MEMORY", _DEFAULT_MEMORY)
    cpus = environment.get("MAESTRO_DOCKER_CPUS", _DEFAULT_CPUS)
    tmpfs_size = environment.get("MAESTRO_DOCKER_TMPFS_SIZE", _DEFAULT_TMPFS_SIZE)
    if _SIZE_PATTERN.fullmatch(memory) is None:
        raise ComposeConfigurationError("MAESTRO_DOCKER_MEMORY is invalid")
    if _SIZE_PATTERN.fullmatch(tmpfs_size) is None:
        raise ComposeConfigurationError("MAESTRO_DOCKER_TMPFS_SIZE is invalid")
    if _CPU_PATTERN.fullmatch(cpus) is None or float(cpus) > _MAX_CPUS:
        raise ComposeConfigurationError("MAESTRO_DOCKER_CPUS is invalid")
    pids_limit = _bounded_integer(
        environment,
        "MAESTRO_DOCKER_PIDS_LIMIT",
        _DEFAULT_PIDS_LIMIT,
        minimum=16,
        maximum=4_096,
    )

    roots = _allowed_roots(environment) if action is DeploymentAction.SERVER else ()
    auth_file = _auth_file(environment) if action is DeploymentAction.SERVER else None
    api_key = (
        environment.get("MAESTRO_CODEX_API_KEY") if action is DeploymentAction.SERVER else None
    )
    if auth_file is not None and api_key is not None:
        raise ComposeConfigurationError("configure only one Codex authentication source")
    role_secrets = _role_secrets(environment, action, uid)
    _validate_distinct_role_secrets(role_secrets)
    sensitive_files = tuple(path for _, path in role_secrets)
    if auth_file is not None:
        sensitive_files = (*sensitive_files, auth_file)
    if any(_is_within(secret, root) for secret in sensitive_files for root in roots):
        raise ComposeConfigurationError("credential files must be outside every allowed root")

    return ComposeConfiguration(
        action=action,
        image=image,
        project=project,
        uid=uid,
        gid=gid,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        tmpfs_size=tmpfs_size,
        name=options.name,
        allowed_roots=roots,
        auth_file=auth_file,
        api_key=api_key,
        forwarded_settings=tuple(
            (setting, value)
            for setting in _FORWARDED_SETTINGS
            if (value := environment.get(setting)) is not None and value != ""
        ),
        role_secrets=role_secrets,
        published_port=options.published_port,
        reader_arguments=_validated_reader_arguments(options.reader_arguments),
    )


def _validate_action_options(options: ComposeOptions) -> None:
    if options.name is not None and options.action is not DeploymentAction.SERVER:
        raise ComposeConfigurationError("a container name is supported only for the server action")
    if options.published_port is not None and options.action is not DeploymentAction.DATABASE_UP:
        raise ComposeConfigurationError("loopback publication is supported only for database-up")
    if options.published_port is not None and not 1 <= options.published_port <= _MAX_PORT:
        raise ComposeConfigurationError("loopback PostgreSQL port is invalid")


def build_compose_override(configuration: ComposeConfiguration) -> dict[str, object]:
    """Build the secret-minimal override for exactly one deployment action."""

    services: dict[str, object] = {}
    secrets: dict[str, object] = {}
    if configuration.action is DeploymentAction.SERVER:
        services["maestro"] = _server_override(configuration)
    elif configuration.action is DeploymentAction.DATABASE_UP:
        services["audit-postgres"] = _database_override(configuration, secrets)
    elif configuration.action is DeploymentAction.BOOTSTRAP:
        services["audit-bootstrap"] = _role_service_override(configuration)
    elif configuration.action is DeploymentAction.MIGRATE:
        services["audit-migrate"] = _role_service_override(configuration)
    elif configuration.action is DeploymentAction.READ:
        services["audit-reader"] = _role_service_override(configuration)
    override: dict[str, object] = {"services": services}
    if secrets:
        override["secrets"] = secrets
    return override


def build_command(configuration: ComposeConfiguration, override_path: str) -> tuple[str, ...]:
    """Build one shell-free Compose argument vector."""

    command = [
        "docker",
        "compose",
        "--ansi",
        "never",
        "--progress",
        "quiet",
        "--project-name",
        configuration.project,
        "--file",
        str(_COMPOSE_FILE),
        "--file",
        override_path,
    ]
    if configuration.action is DeploymentAction.SERVER:
        command.extend(("run", "--rm", "--no-deps", "--no-TTY"))
        if configuration.name is not None:
            command.extend(("--name", configuration.name))
        command.append("maestro")
    elif configuration.action is DeploymentAction.DATABASE_UP:
        command.extend(("up", "--detach", "--wait", "--wait-timeout", "60", "audit-postgres"))
    elif configuration.action is DeploymentAction.DOWN:
        command.extend(("down", "--remove-orphans"))
    else:
        service = {
            DeploymentAction.BOOTSTRAP: "audit-bootstrap",
            DeploymentAction.MIGRATE: "audit-migrate",
            DeploymentAction.READ: "audit-reader",
        }[configuration.action]
        command.extend(("run", "--rm", "--no-deps", "--no-TTY", service))
        if configuration.action is DeploymentAction.READ:
            command.extend(("read", *configuration.reader_arguments))
    return tuple(command)


def compose_environment(
    configuration: ComposeConfiguration,
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Preserve Docker client context while pinning safe Compose interpolation inputs."""

    result = {
        name: value
        for name in _DOCKER_CLIENT_ENVIRONMENT
        if (value := environment.get(name)) is not None
    }
    result.update(
        {
            "MAESTRO_DOCKER_CPUS": configuration.cpus,
            "MAESTRO_DOCKER_GID": str(configuration.gid),
            "MAESTRO_DOCKER_IMAGE": configuration.image,
            "MAESTRO_DOCKER_MEMORY": configuration.memory,
            "MAESTRO_DOCKER_PIDS_LIMIT": str(configuration.pids_limit),
            "MAESTRO_DOCKER_TMPFS_SIZE": configuration.tmpfs_size,
            "MAESTRO_DOCKER_UID": str(configuration.uid),
        }
    )
    return result


def _server_override(configuration: ComposeConfiguration) -> dict[str, object]:
    allowed_roots = os.pathsep.join(str(root) for root in configuration.allowed_roots)
    environment = {
        "MAESTRO_ALLOWED_ROOTS": allowed_roots,
        "MAESTRO_AUDIT_WRITER_DATABASE": "maestro",
        "MAESTRO_AUDIT_WRITER_HOST": "audit-postgres",
        "MAESTRO_AUDIT_WRITER_PASSWORD_FILE": _CREDENTIAL_TARGETS["writer"],
        "MAESTRO_AUDIT_WRITER_PORT": "5432",
        "MAESTRO_AUDIT_WRITER_USER": _ROLE_USERS["writer"],
    }
    environment.update(configuration.forwarded_settings)
    mounts: list[dict[str, object]] = [
        _credential_mount(root, str(root)) for root in configuration.allowed_roots
    ]
    mounts.extend(_credential_mounts(configuration))
    if configuration.auth_file is not None:
        mounts.append(_credential_mount(configuration.auth_file, _CONTAINER_AUTH_FILE))
        environment["MAESTRO_CODEX_AUTH_FILE"] = _CONTAINER_AUTH_FILE
    if configuration.api_key is not None:
        environment["MAESTRO_CODEX_API_KEY"] = configuration.api_key
    return {
        "environment": environment,
        "volumes": mounts,
    }


def _database_override(
    configuration: ComposeConfiguration,
    secrets: dict[str, object],
) -> dict[str, object]:
    service: dict[str, object] = {
        "secrets": _postgres_bootstrap_secret(configuration, secrets),
    }
    if configuration.published_port is not None:
        service["ports"] = [
            {
                "target": 5432,
                "published": str(configuration.published_port),
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
                "mode": "host",
            }
        ]
    return service


def _role_service_override(configuration: ComposeConfiguration) -> dict[str, object]:
    environment: dict[str, str] = {}
    for role, _path in configuration.role_secrets:
        prefix = f"MAESTRO_AUDIT_{role.upper()}_"
        environment.update(
            {
                f"{prefix}HOST": "audit-postgres",
                f"{prefix}PORT": "5432",
                f"{prefix}DATABASE": "maestro",
                f"{prefix}USER": _ROLE_USERS[role],
                f"{prefix}PASSWORD_FILE": _CREDENTIAL_TARGETS[role],
            }
        )
    return {
        "environment": environment,
        "volumes": _credential_mounts(configuration),
    }


def _postgres_bootstrap_secret(
    configuration: ComposeConfiguration,
    secrets: dict[str, object],
) -> list[dict[str, object]]:
    """Project only the root-consumed bootstrap credential onto PostgreSQL's official path."""

    for role, path in configuration.role_secrets:
        if role != "bootstrap":
            continue
        secrets["audit-bootstrap-password"] = {"file": str(path)}
        return [{"source": "audit-bootstrap-password", "target": _POSTGRES_BOOTSTRAP_TARGET}]
    raise ComposeConfigurationError("the database action requires the bootstrap credential")


def _credential_mounts(configuration: ComposeConfiguration) -> list[dict[str, object]]:
    """Deliver each role credential as an owner-preserving read-only bind mount."""

    return [
        _credential_mount(path, _CREDENTIAL_TARGETS[role])
        for role, path in configuration.role_secrets
    ]


def _credential_mount(source: Path, target: str) -> dict[str, object]:
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": True,
        "bind": {"create_host_path": False, "propagation": "rprivate"},
    }


def _role_secrets(
    environment: Mapping[str, str],
    action: DeploymentAction,
    uid: int,
) -> tuple[tuple[str, Path], ...]:
    roles = {
        DeploymentAction.SERVER: ("writer",),
        DeploymentAction.DATABASE_UP: ("bootstrap",),
        DeploymentAction.BOOTSTRAP: ("bootstrap", "migration", "writer", "reader"),
        DeploymentAction.MIGRATE: ("migration",),
        DeploymentAction.READ: ("reader",),
        DeploymentAction.DOWN: (),
    }[action]
    return tuple(
        (
            role,
            _audit_secret_file(
                environment,
                f"MAESTRO_AUDIT_{role.upper()}_PASSWORD_FILE",
                uid,
            ),
        )
        for role in roles
    )


def _validate_distinct_role_secrets(role_secrets: tuple[tuple[str, Path], ...]) -> None:
    identities: set[tuple[int, int]] = set()
    for _role, path in role_secrets:
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            raise ComposeConfigurationError("Audit password file is unavailable") from None
        identities.add((metadata.st_dev, metadata.st_ino))
    if len(identities) != len(role_secrets):
        raise ComposeConfigurationError("Audit role password files must be distinct")


def _validated_reader_arguments(arguments: tuple[str, ...]) -> tuple[str, ...]:
    if len(arguments) % 2 != 0:
        raise ComposeConfigurationError("reader arguments are invalid")
    allowed = {
        "--view": frozenset(("summary", "timeline", "evidence")),
        "--outcome": frozenset(
            ("resolved", "uncertain", "human_decision_required", "failed", "incomplete")
        ),
    }
    normalized: list[str] = []
    seen: set[str] = set()
    view = "summary"
    outcome: str | None = None
    for index in range(0, len(arguments), 2):
        name, value = arguments[index : index + 2]
        if name in seen or name not in {
            "--view",
            "--audit-id",
            "--execution-id",
            "--repository-id",
            "--outcome",
        }:
            raise ComposeConfigurationError("reader arguments are invalid")
        seen.add(name)
        if name in {"--audit-id", "--execution-id"}:
            try:
                value = str(UUID(value))
            except ValueError:
                raise ComposeConfigurationError("reader UUID filter is invalid") from None
        elif name == "--repository-id" and re.fullmatch(r"[0-9a-f]{16}", value) is None:
            raise ComposeConfigurationError("reader repository filter is invalid")
        elif name in allowed and value not in allowed[name]:
            raise ComposeConfigurationError("reader filter is invalid")
        if name == "--view":
            view = value
        elif name == "--outcome":
            outcome = value
        normalized.extend((name, value))
    if outcome is not None and view != "summary":
        raise ComposeConfigurationError("outcome filtering is available only for summary reads")
    return tuple(normalized)


def _allowed_roots(environment: Mapping[str, str]) -> tuple[Path, ...]:
    raw = environment.get("MAESTRO_ALLOWED_ROOTS")
    if raw is None:
        raise ComposeConfigurationError("MAESTRO_ALLOWED_ROOTS is required")
    roots: list[Path] = []
    for value in raw.split(os.pathsep):
        if not value.strip():
            continue
        root = _canonical_directory(Path(value), "allowed root")
        _validate_mount_path(root)
        if root == Path(root.anchor):
            raise ComposeConfigurationError("filesystem anchors cannot be allowed roots")
        if root not in roots:
            roots.append(root)
    if not roots:
        raise ComposeConfigurationError("MAESTRO_ALLOWED_ROOTS must contain a directory")
    return tuple(roots)


def _auth_file(environment: Mapping[str, str]) -> Path | None:
    raw = environment.get("MAESTRO_CODEX_AUTH_FILE")
    if raw is None:
        return None
    return _canonical_regular_file(Path(raw), "MAESTRO_CODEX_AUTH_FILE")


def _audit_secret_file(environment: Mapping[str, str], name: str, uid: int) -> Path:
    raw = environment.get(name)
    if raw is None:
        raise ComposeConfigurationError(f"{name} is required for this action")
    path = _canonical_regular_file(Path(raw), "Audit password file")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise ComposeConfigurationError("Audit password file is unavailable") from None
    if metadata.st_uid != uid:
        raise ComposeConfigurationError("Audit password file must be owned by the container user")
    if stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
        raise ComposeConfigurationError("Audit password file permissions must be owner-only")
    if not 0 < metadata.st_size <= _MAX_PASSWORD_BYTES:
        raise ComposeConfigurationError("Audit password file size is invalid")
    return path


def _canonical_regular_file(path: Path, description: str) -> Path:
    try:
        candidate = path.parent.resolve(strict=True) / path.name
        metadata = os.lstat(candidate)
    except (OSError, RuntimeError):
        raise ComposeConfigurationError(f"{description} is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ComposeConfigurationError(f"{description} must be a regular non-symlink file")
    _validate_mount_path(candidate)
    return candidate


def _canonical_directory(path: Path, description: str) -> Path:
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ComposeConfigurationError(f"{description} does not exist") from None
    if not canonical.is_dir():
        raise ComposeConfigurationError(f"{description} must be a directory")
    return canonical


def _validate_mount_path(path: Path) -> None:
    if "," in str(path):
        raise ComposeConfigurationError("Docker mount paths containing commas are unsupported")


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except ValueError as exc:
        raise ComposeConfigurationError(f"{name} is invalid") from exc
    if not minimum <= value <= maximum:
        raise ComposeConfigurationError(f"{name} is invalid")
    return value


def _identity(environment: Mapping[str, str], name: str, default: int) -> int:
    return _bounded_integer(environment, name, default, minimum=1, maximum=2_147_483_647)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=tuple(action.value for action in DeploymentAction),
        default=DeploymentAction.SERVER.value,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print only the safe argument vector",
    )
    parser.add_argument("--image", help="override MAESTRO_DOCKER_IMAGE")
    parser.add_argument("--name", help="assign a validated server container name")
    parser.add_argument("--publish-loopback", type=int, metavar="PORT")
    parser.add_argument("--audit-id")
    parser.add_argument("--execution-id")
    parser.add_argument("--repository-id")
    parser.add_argument("--outcome")
    parser.add_argument("--view", choices=("summary", "timeline", "evidence"), default="summary")
    return parser


def _reader_arguments(options: argparse.Namespace) -> tuple[str, ...]:
    arguments: list[str] = ["--view", options.view]
    for option in ("audit_id", "execution_id", "repository_id", "outcome"):
        if value := getattr(options, option):
            arguments.extend((f"--{option.replace('_', '-')}", value))
    return tuple(arguments)


def _write_override(stream: BinaryIO, override: dict[str, object]) -> None:
    stream.write(json.dumps(override, separators=(",", ":")).encode("utf-8"))
    stream.flush()
    stream.seek(0)
    os.set_inheritable(stream.fileno(), True)


def main(arguments: Sequence[str] | None = None) -> int:
    """Validate one action, then replace this process with the attached Compose CLI."""

    parser = _parser()
    options = parser.parse_args(arguments)
    action = DeploymentAction(options.action)
    try:
        configuration = load_configuration(
            os.environ,
            ComposeOptions(
                action=action,
                image_override=options.image,
                name=options.name,
                published_port=options.publish_loopback,
                reader_arguments=_reader_arguments(options),
            ),
        )
        override = build_compose_override(configuration)
    except ComposeConfigurationError as exc:
        parser.error(str(exc))
    with tempfile.TemporaryFile(mode="w+b") as stream:
        _write_override(stream, override)
        command = build_command(configuration, f"/dev/fd/{stream.fileno()}")
        if options.dry_run:
            print(json.dumps(command, separators=(",", ":")))
            return 0
        os.execvpe(  # noqa: S606 - validated argument vector, no shell
            command[0],
            command,
            compose_environment(configuration, os.environ),
        )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
