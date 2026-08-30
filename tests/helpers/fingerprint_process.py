"""Hostile real-process fixture for repository subprocess boundary tests."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import TypedDict


class _FilePayload(TypedDict):
    relative_path: str
    token: str
    content_digest: str | None
    line_count: int | None
    size: int
    consumed_bytes: int


class _ResultPayload(TypedDict):
    protocol_version: int
    files: list[_FilePayload]
    file_count: int
    consumed_bytes: int
    truncated: bool | int


def _valid_result() -> _ResultPayload:
    return {
        "protocol_version": 1,
        "files": [
            {
                "relative_path": "probe.txt",
                "token": "probe:valid",
                "content_digest": None,
                "line_count": None,
                "size": 0,
                "consumed_bytes": 0,
            }
        ],
        "file_count": 1,
        "consumed_bytes": 0,
        "truncated": False,
    }


def _write_result(result: object) -> None:
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    sys.stdout.flush()


def _block(marker: Path, *, close_stdout: bool = False) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # Publish the identifier atomically. A waiter polling for existence must never observe
    # a created-but-still-empty marker, which parses as an invalid process identifier.
    staged = marker.with_name(marker.name + ".staged")
    staged.write_text(str(os.getpid()), encoding="ascii")
    staged.replace(marker)
    if close_stdout:
        sys.stdout.close()
    while True:
        time.sleep(1)


def _mutated_result(  # noqa: C901,PLR0912 - hostile fixture matrix
    mode: str,
) -> _ResultPayload:
    result = _valid_result()
    files = result["files"]
    item = files[0]
    if mode == "duplicate":
        files.append(item.copy())
        result["file_count"] = 2
    elif mode == "traversal":
        item["relative_path"] = "../escape"
    elif mode == "absolute":
        item["relative_path"] = "/private/file"
    elif mode == "backslash":
        item["relative_path"] = "src\\file.py"
    elif mode == "nul":
        item["relative_path"] = "bad\x00file"
    elif mode == "drive":
        item["relative_path"] = "C:/outside"
    elif mode == "c1-path":
        item["relative_path"] = "bad\u0085file"
    elif mode == "c1-token":
        item["token"] = "bad\u0085token"  # noqa: S105 - intentionally invalid state token
    elif mode == "bidi-path":
        item["relative_path"] = "bad\u202efile"
    elif mode == "format-token":
        item["token"] = "bad\u2066token"  # noqa: S105 - intentionally invalid state token
    elif mode == "wrong-version":
        result["protocol_version"] = 2
    elif mode == "count-mismatch":
        result["file_count"] = 0
    elif mode == "aggregate-mismatch":
        result["consumed_bytes"] = 1
    elif mode == "bad-digest":
        item["content_digest"] = "not-a-digest"
    elif mode == "bad-token":
        item["token"] = "bad\ntoken"  # noqa: S105 - intentionally invalid state token
    elif mode == "bad-file-state":
        item["content_digest"] = "a" * 64
        item["line_count"] = None
    elif mode == "negative-size":
        item["size"] = -1
    elif mode == "consumed-over-size":
        item["consumed_bytes"] = 1
        result["consumed_bytes"] = 1
    elif mode == "token-too-long":
        item["token"] = "x" * 8_193
    elif mode == "path-too-long":
        item["relative_path"] = "x" * 4_097
    elif mode == "bad-truncation":
        result["truncated"] = 1
    return result


def main() -> int:  # noqa: PLR0911 - process-mode fixture exits explicitly
    mode = sys.argv[1]
    if mode == "block-no-read":
        _block(Path(sys.argv[2]))
    raw_request = sys.stdin.buffer.read()
    if mode == "block":
        _block(Path(sys.argv[2]))
    if mode == "wait-block":
        _block(Path(sys.argv[2]), close_stdout=True)
    if mode == "malformed":
        sys.stdout.write("{")
        return 0
    if mode == "missing-version":
        result = _valid_result()
        _write_result({key: value for key, value in result.items() if key != "protocol_version"})
        return 0
    if mode == "stderr-detail":
        sys.stderr.write("internal detail from /private/sensitive/repository")
        sys.stdout.write("{")
        return 0
    if mode == "oversized":
        sys.stdout.write("x" * int(sys.argv[2]))
        return 0
    if mode == "environment-fd":
        expected_environment = set(sys.argv[2].split(","))
        # macOS injects __CF_USER_TEXT_ENCODING into every process. It is outside the
        # launcher's allowlist and outside Maestro's control, so ignore it rather than
        # expecting it — expecting it makes this probe fail on every other platform.
        actual_environment = set(os.environ) - {"__CF_USER_TEXT_ENCODING"}
        descriptor = int(sys.argv[3])
        expected_cwd = Path(sys.argv[4])
        try:
            os.fstat(descriptor)
        except OSError:
            descriptor_closed = True
        else:
            descriptor_closed = False
        result = _valid_result()
        files = result["files"]
        item = files[0]
        clean = (
            actual_environment == expected_environment
            and descriptor_closed
            and Path.cwd() == expected_cwd
        )
        item["token"] = "probe:clean" if clean else "probe:unsafe"
        _write_result(result)
        return 0
    if not raw_request:
        return 2
    _write_result(_mutated_result(mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
