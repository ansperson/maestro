#!/usr/bin/env python3
"""Fail closed unless Compose made every authorized repository mount read-only."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_MAESTRO_EXECUTABLE = "/opt/maestro/.venv/bin/maestro"
_PYTHON_EXECUTABLE = "/opt/maestro/.venv/bin/python"
_ADMIN_MODULE = "maestro.audit.postgres.admin"
_ADMIN_ACTIONS = ("bootstrap", "migrate", "read")
_MOUNTINFO = Path("/proc/self/mountinfo")
# Credentials arrive on a read-only mount whose reported ownership is not authoritative on
# every supported engine: Docker Desktop's shared filesystem reports host-owned files as
# root-owned, and does so inconsistently. Maestro requires an owner-only credential, so each
# value is re-materialized on real tmpfs, where ownership and mode are enforced by the kernel.
_CREDENTIAL_ROOT = Path("/run/maestro-credentials")
_STAGED_ROOT = Path("/run/maestro-secrets")
_CREDENTIAL_VARIABLES = (
    "MAESTRO_AUDIT_BOOTSTRAP_PASSWORD_FILE",
    "MAESTRO_AUDIT_MIGRATION_PASSWORD_FILE",
    "MAESTRO_AUDIT_READER_PASSWORD_FILE",
    "MAESTRO_AUDIT_WRITER_PASSWORD_FILE",
)
_MAX_CREDENTIAL_BYTES = 4_096
_MAX_CREDENTIAL_NAME_CHARS = 128
_MOUNT_ESCAPE = re.compile(r"\\(040|011|012|134)")
_MOUNT_ESCAPE_VALUES = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}
_MINIMUM_MOUNTINFO_FIELDS = 10


class MountGuardError(RuntimeError):
    """The effective container mount state is not safe for Maestro startup."""


@dataclass(frozen=True, slots=True)
class MountState:
    """One VFS mountpoint and its effective per-mount options."""

    path: Path
    options: frozenset[str]
    propagation: frozenset[str]


def parse_mountinfo(raw: str) -> tuple[MountState, ...]:
    """Parse only the mountpoint and VFS option fields from Linux mountinfo."""

    mounts: list[MountState] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < _MINIMUM_MOUNTINFO_FIELDS:
            raise MountGuardError("container mount information is invalid")
        try:
            separator = fields.index("-", 6)
        except ValueError:
            raise MountGuardError("container mount information is invalid") from None
        mounts.append(
            MountState(
                path=Path(_unescape_mount_field(fields[4])),
                options=frozenset(fields[5].split(",")),
                propagation=frozenset(fields[6:separator]),
            )
        )
    if not mounts:
        raise MountGuardError("container mount information is unavailable")
    return tuple(mounts)


def validate_allowed_root_mounts(
    allowed_roots: tuple[Path, ...],
    mounts: tuple[MountState, ...],
) -> None:
    """Require every root and descendant mount to be effectively VFS read-only."""

    for root in allowed_roots:
        root_mounts = tuple(mount for mount in mounts if mount.path == root)
        if not root_mounts:
            raise MountGuardError("an allowed root is not a dedicated container mount")
        descendants = tuple(mount for mount in mounts if _is_within(mount.path, root))
        if any("ro" not in mount.options for mount in descendants):
            raise MountGuardError("an allowed repository mount is not recursively read-only")
        if any(_has_nonprivate_propagation(mount) for mount in descendants):
            raise MountGuardError("an allowed repository mount does not use private propagation")


def load_allowed_roots(environment: dict[str, str]) -> tuple[Path, ...]:
    """Load the canonical absolute roots emitted by the trusted host launcher."""

    raw = environment.get("MAESTRO_ALLOWED_ROOTS")
    if raw is None:
        raise MountGuardError("allowed roots are missing")
    roots = tuple(Path(part) for part in raw.split(os.pathsep) if part)
    if not roots or any(not root.is_absolute() or root == Path(root.anchor) for root in roots):
        raise MountGuardError("allowed roots are invalid")
    return roots


def stage_credentials(environment: dict[str, str]) -> None:
    """Re-materialize each mounted credential on tmpfs and repoint its configured path."""

    for variable in _CREDENTIAL_VARIABLES:
        raw = environment.get(variable)
        if raw is None:
            continue
        source = Path(raw)
        if source.parent != _CREDENTIAL_ROOT or not _valid_credential_name(source.name):
            raise MountGuardError("a configured credential path is outside the mounted root")
        target = _STAGED_ROOT / source.name
        _write_owner_only(target, _read_bounded(source))
        environment[variable] = str(target)


def _valid_credential_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and len(name) <= _MAX_CREDENTIAL_NAME_CHARS
    )


def _read_bounded(source: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(source, flags)
    try:
        value = bytearray()
        while chunk := os.read(descriptor, _MAX_CREDENTIAL_BYTES + 1 - len(value)):
            value.extend(chunk)
            if len(value) > _MAX_CREDENTIAL_BYTES:
                raise MountGuardError("a configured credential is oversized")
    finally:
        os.close(descriptor)
    if not value:
        raise MountGuardError("a configured credential is empty")
    return bytes(value)


def _write_owner_only(target: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o400)
    try:
        os.fchmod(descriptor, 0o400)
        view = memoryview(value)
        while view:
            view = view[os.write(descriptor, view) :]
    finally:
        os.close(descriptor)


def _target_command(arguments: list[str]) -> tuple[str, ...]:
    """Resolve one fixed startup target; arbitrary executables are never run.

    Administration actions forward their remaining arguments to the fixed admin module, which
    performs its own validation. Only the leading action selects the target.
    """

    if not arguments:
        return (_MAESTRO_EXECUTABLE,)
    if arguments[0] in _ADMIN_ACTIONS:
        return (_PYTHON_EXECUTABLE, "-m", _ADMIN_MODULE, *arguments)
    raise MountGuardError("the requested startup target is not supported")


def main() -> int:
    """Stage credentials and verify the namespace before replacing this process."""

    environment = dict(os.environ)
    try:
        command = _target_command(sys.argv[1:])
        stage_credentials(environment)
        if command[0] == _MAESTRO_EXECUTABLE:
            roots = load_allowed_roots(environment)
            mountinfo = _MOUNTINFO.read_text(encoding="utf-8")
            validate_allowed_root_mounts(roots, parse_mountinfo(mountinfo))
    except (MountGuardError, OSError, UnicodeError):
        print("Maestro container startup validation failed", file=sys.stderr)
        return 78
    os.environ.update(environment)
    os.execv(command[0], command)  # noqa: S606 - fixed executable and validated target
    return 127


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: _MOUNT_ESCAPE_VALUES[match.group(1)], value)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _has_nonprivate_propagation(mount: MountState) -> bool:
    return any(
        option.startswith(("shared:", "master:", "propagate_from:")) for option in mount.propagation
    )


if __name__ == "__main__":
    raise SystemExit(main())
