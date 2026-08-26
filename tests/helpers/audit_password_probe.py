"""Bounded child probe for Audit password-file nonblocking rejection."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

import maestro.config as config_module


def _construct(path: Path) -> int:
    try:
        config_module.AuditWriterSettings(user="audit_writer", password_file=path)
    except ValidationError as exc:
        rendered = str(exc)
        safe_category = "regular" in rendered or "changed while being opened" in rendered
        if str(path) in rendered or not safe_category:
            return 3
        return 0
    return 2


def _replace_with_fifo_during_open(path: Path) -> int:
    original_open = config_module.os.open

    def replace_on_open(candidate: Path, flags: int) -> int:
        path.unlink()
        os.mkfifo(path, mode=0o600)
        return original_open(candidate, flags)

    with patch.object(config_module.os, "open", replace_on_open):
        return _construct(path)


def main() -> int:
    if len(sys.argv) != 3:
        return 64
    mode = sys.argv[1]
    path = Path(sys.argv[2])
    if mode == "fixed-fifo":
        return _construct(path)
    if mode == "replacement-fifo":
        return _replace_with_fifo_during_open(path)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
