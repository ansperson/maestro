#!/usr/bin/env python3
"""Launch Maestro's stdio server with the approved hardened Docker profile."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_IMAGE = "maestro-verifier:local"
_DEFAULT_MEMORY = "2g"
_DEFAULT_CPUS = "2"
_DEFAULT_PIDS_LIMIT = 256
_DEFAULT_TMPFS_SIZE = "512m"
_MAX_CPUS = 64
_CONTAINER_AUTH_FILE = "/run/maestro-auth/auth.json"
# Audit is mandatory configuration, so the direct launcher delivers the append-writer
# credential the same way the Compose adapter does: a read-only mount that the image-owned
# guard re-materializes on tmpfs before starting Maestro.
_MOUNT_GUARD = "/opt/maestro/maestro_mount_guard.py"
_CONTAINER_PYTHON = "/opt/maestro/.venv/bin/python"
_CONTAINER_CREDENTIAL_ROOT = "/run/maestro-credentials"
_CONTAINER_WRITER_CREDENTIAL = f"{_CONTAINER_CREDENTIAL_ROOT}/audit-writer-password"
_CONTAINER_STAGED_ROOT = "/run/maestro-secrets"
_IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,254}\Z")
_SIZE_PATTERN = re.compile(r"[1-9][0-9]*(?:[bkmgBKMG])?\Z")
_CPU_PATTERN = re.compile(r"(?:[1-9][0-9]*|0\.[0-9]*[1-9][0-9]*)\Z")
_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_FORWARDED_SETTINGS = (
    "MAESTRO_AUDIT_WRITER_DATABASE",
    "MAESTRO_AUDIT_WRITER_HOST",
    "MAESTRO_AUDIT_WRITER_PORT",
    "MAESTRO_AUDIT_WRITER_USER",
    "MAESTRO_AGENT_RUNTIME",
    "MAESTRO_CLAUDE_EFFORT",
    "MAESTRO_CLAUDE_EXECUTABLE",
    "MAESTRO_CLAUDE_MAX_BUDGET_USD",
    "MAESTRO_CLAUDE_MODEL",
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


class LauncherConfigurationError(ValueError):
    """A safe container command cannot be derived from the environment."""


@dataclass(frozen=True, slots=True)
class ContainerConfiguration:
    """Validated deployment-only inputs for one hardened container process."""

    image: str
    allowed_roots: tuple[Path, ...]
    auth_file: Path | None
    audit_password_file: Path | None
    memory: str
    cpus: str
    pids_limit: int
    tmpfs_size: str
    uid: int
    gid: int
    name: str | None


def load_configuration(
    environment: Mapping[str, str],
    *,
    image_override: str | None = None,
    name: str | None = None,
) -> ContainerConfiguration:
    """Validate host inputs without invoking Docker or resolving shell text."""

    image = image_override or environment.get("MAESTRO_DOCKER_IMAGE", _DEFAULT_IMAGE)
    if _IMAGE_PATTERN.fullmatch(image) is None:
        raise LauncherConfigurationError("MAESTRO_DOCKER_IMAGE is invalid")
    if name is not None and _NAME_PATTERN.fullmatch(name) is None:
        raise LauncherConfigurationError("container name is invalid")

    roots = _allowed_roots(environment)
    auth_file = _auth_file(environment)
    audit_password_file = _audit_password_file(environment)
    memory = environment.get("MAESTRO_DOCKER_MEMORY", _DEFAULT_MEMORY)
    cpus = environment.get("MAESTRO_DOCKER_CPUS", _DEFAULT_CPUS)
    tmpfs_size = environment.get("MAESTRO_DOCKER_TMPFS_SIZE", _DEFAULT_TMPFS_SIZE)
    if _SIZE_PATTERN.fullmatch(memory) is None:
        raise LauncherConfigurationError("MAESTRO_DOCKER_MEMORY is invalid")
    if _SIZE_PATTERN.fullmatch(tmpfs_size) is None:
        raise LauncherConfigurationError("MAESTRO_DOCKER_TMPFS_SIZE is invalid")
    if _CPU_PATTERN.fullmatch(cpus) is None or float(cpus) > _MAX_CPUS:
        raise LauncherConfigurationError("MAESTRO_DOCKER_CPUS is invalid")

    pids_limit = _bounded_integer(
        environment,
        "MAESTRO_DOCKER_PIDS_LIMIT",
        _DEFAULT_PIDS_LIMIT,
        minimum=16,
        maximum=4_096,
    )
    uid = _identity(environment, "MAESTRO_DOCKER_UID", os.getuid())
    gid = _identity(environment, "MAESTRO_DOCKER_GID", os.getgid())
    return ContainerConfiguration(
        image=image,
        allowed_roots=roots,
        auth_file=auth_file,
        audit_password_file=audit_password_file,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        tmpfs_size=tmpfs_size,
        uid=uid,
        gid=gid,
        name=name,
    )


def build_command(
    configuration: ContainerConfiguration,
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    """Build the exact argument vector for the approved runtime profile."""

    command = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--init",
        "--read-only",
        "--user",
        f"{configuration.uid}:{configuration.gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--network",
        "bridge",
        "--memory",
        configuration.memory,
        "--cpus",
        configuration.cpus,
        "--pids-limit",
        str(configuration.pids_limit),
        "--tmpfs",
        (
            "/tmp:rw,nosuid,nodev,noexec,"  # noqa: S108 - intentional ephemeral mount
            f"size={configuration.tmpfs_size},mode=0700,"
            f"uid={configuration.uid},gid={configuration.gid}"
        ),
    ]
    if configuration.name is not None:
        command.extend(("--name", configuration.name))

    allowed_roots = os.pathsep.join(str(root) for root in configuration.allowed_roots)
    command.extend(("--env", f"MAESTRO_ALLOWED_ROOTS={allowed_roots}"))
    for name in _FORWARDED_SETTINGS:
        if value := environment.get(name):
            command.extend(("--env", f"{name}={value}"))

    for root in configuration.allowed_roots:
        command.extend(
            (
                "--mount",
                (
                    f"type=bind,src={root},dst={root},readonly,"
                    "bind-recursive=readonly,bind-propagation=rprivate"
                ),
            )
        )

    if configuration.auth_file is not None:
        command.extend(
            (
                "--mount",
                (f"type=bind,src={configuration.auth_file},dst={_CONTAINER_AUTH_FILE},readonly"),
                "--env",
                f"MAESTRO_CODEX_AUTH_FILE={_CONTAINER_AUTH_FILE}",
            )
        )
    if environment.get("MAESTRO_CODEX_API_KEY"):
        command.extend(("--env", "MAESTRO_CODEX_API_KEY"))

    if configuration.audit_password_file is not None:
        command.extend(
            (
                "--tmpfs",
                (
                    f"{_CONTAINER_STAGED_ROOT}:rw,nosuid,nodev,noexec,size=1m,mode=0700,"
                    f"uid={configuration.uid},gid={configuration.gid}"
                ),
                "--mount",
                (
                    f"type=bind,src={configuration.audit_password_file},"
                    f"dst={_CONTAINER_WRITER_CREDENTIAL},readonly"
                ),
                "--env",
                f"MAESTRO_AUDIT_WRITER_PASSWORD_FILE={_CONTAINER_WRITER_CREDENTIAL}",
                "--entrypoint",
                _CONTAINER_PYTHON,
            )
        )
        command.extend((configuration.image, _MOUNT_GUARD))
        return tuple(command)

    command.append(configuration.image)
    return tuple(command)


def _allowed_roots(environment: Mapping[str, str]) -> tuple[Path, ...]:
    raw = environment.get("MAESTRO_ALLOWED_ROOTS")
    if raw is None:
        raise LauncherConfigurationError("MAESTRO_ALLOWED_ROOTS is required")
    roots: list[Path] = []
    for value in raw.split(os.pathsep):
        if not value.strip():
            continue
        root = _canonical_directory(Path(value), "allowed root")
        _validate_mount_path(root)
        if root not in roots:
            roots.append(root)
    if not roots:
        raise LauncherConfigurationError("MAESTRO_ALLOWED_ROOTS must contain a directory")
    return tuple(roots)


def _auth_file(environment: Mapping[str, str]) -> Path | None:
    return _credential_file(environment, "MAESTRO_CODEX_AUTH_FILE")


def _audit_password_file(environment: Mapping[str, str]) -> Path | None:
    return _credential_file(environment, "MAESTRO_AUDIT_WRITER_PASSWORD_FILE")


def _credential_file(environment: Mapping[str, str], variable: str) -> Path | None:
    raw = environment.get(variable)
    if raw is None:
        return None
    path = Path(raw)
    if path.is_symlink():
        raise LauncherConfigurationError(f"{variable} must not be a symlink")
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LauncherConfigurationError(f"{variable} does not exist") from exc
    if not canonical.is_file():
        raise LauncherConfigurationError(f"{variable} must be a regular file")
    _validate_mount_path(canonical)
    return canonical


def _canonical_directory(path: Path, description: str) -> Path:
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LauncherConfigurationError(f"{description} does not exist") from exc
    if not canonical.is_dir():
        raise LauncherConfigurationError(f"{description} must be a directory")
    return canonical


def _validate_mount_path(path: Path) -> None:
    if "," in str(path):
        raise LauncherConfigurationError("Docker --mount paths containing commas are unsupported")


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
        raise LauncherConfigurationError(f"{name} is invalid") from exc
    if not minimum <= value <= maximum:
        raise LauncherConfigurationError(f"{name} is invalid")
    return value


def _identity(environment: Mapping[str, str], name: str, default: int) -> int:
    return _bounded_integer(environment, name, default, minimum=1, maximum=2_147_483_647)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the argument vector as JSON")
    parser.add_argument("--image", help="override MAESTRO_DOCKER_IMAGE")
    parser.add_argument("--name", help="assign a validated Docker container name")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    """Validate configuration, then replace this process with Docker."""

    options = _parser().parse_args(arguments)
    try:
        configuration = load_configuration(
            os.environ,
            image_override=options.image,
            name=options.name,
        )
        command = build_command(configuration, os.environ)
    except LauncherConfigurationError as exc:
        _parser().error(str(exc))
    if options.dry_run:
        print(json.dumps(command, separators=(",", ":")))
        return 0
    os.execvp(command[0], command)  # noqa: S606 - validated argument vector, no shell
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
