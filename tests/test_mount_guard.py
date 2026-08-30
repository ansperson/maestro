from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from scripts import maestro_mount_guard as guard  # noqa: E402

sys.path.pop(0)


def _mountinfo(
    path: str,
    *,
    options: str = "ro,nosuid,nodev",
    propagation: str = "",
    mount_id: int = 10,
) -> str:
    optional = f" {propagation}" if propagation else ""
    return f"{mount_id} 1 0:1 / {path} {options}{optional} - ext4 /dev/test ro\n"


def test_mountinfo_parser_decodes_paths_and_keeps_propagation() -> None:
    mounts = guard.parse_mountinfo(_mountinfo("/repo\\040with\\134slash", propagation="shared:7"))

    assert mounts == (
        guard.MountState(
            path=Path("/repo with\\slash"),
            options=frozenset(("ro", "nosuid", "nodev")),
            propagation=frozenset(("shared:7",)),
        ),
    )


def test_mount_guard_accepts_root_and_descendants_that_are_private_readonly() -> None:
    raw = _mountinfo("/repo") + _mountinfo("/repo/nested", mount_id=11)
    guard.validate_allowed_root_mounts((Path("/repo"),), guard.parse_mountinfo(raw))


@pytest.mark.parametrize(
    "raw",
    [
        _mountinfo("/different"),
        _mountinfo("/repo", options="rw,nosuid,nodev"),
        _mountinfo("/repo") + _mountinfo("/repo/nested", options="rw", mount_id=11),
        _mountinfo("/repo", propagation="shared:7"),
        _mountinfo("/repo") + _mountinfo("/repo/nested", propagation="master:9", mount_id=11),
        _mountinfo("/repo", propagation="propagate_from:4"),
    ],
)
def test_mount_guard_rejects_missing_writable_or_nonprivate_mounts(raw: str) -> None:
    with pytest.raises(guard.MountGuardError):
        guard.validate_allowed_root_mounts((Path("/repo"),), guard.parse_mountinfo(raw))


@pytest.mark.parametrize("raw", ["", "broken", "1 2 3 4 5 6 no-separator"])
def test_mountinfo_parser_rejects_malformed_input(raw: str) -> None:
    with pytest.raises(guard.MountGuardError):
        guard.parse_mountinfo(raw)


def test_mount_guard_root_environment_is_bounded_to_absolute_nonanchors() -> None:
    assert guard.load_allowed_roots({"MAESTRO_ALLOWED_ROOTS": "/repo:/second"}) == (
        Path("/repo"),
        Path("/second"),
    )
    for value in (None, "", "/", "relative"):
        environment = {} if value is None else {"MAESTRO_ALLOWED_ROOTS": value}
        with pytest.raises(guard.MountGuardError):
            guard.load_allowed_roots(environment)
