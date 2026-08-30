"""Controlled subprocess used to exercise the owned-worker boundary."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def _success() -> dict[str, object]:
    return {
        "kind": "success",
        "result": {
            "status": "resolved",
            "answer": "The model stores a list of payments.",
            "confidence": "high",
            "evidence": [
                {
                    "path": "src/models.py",
                    "line_start": 1,
                    "line_end": 3,
                    "symbol": "Order.payments",
                    "finding": "The field is a list.",
                }
            ],
            "conflicts": [],
            "reason": "The source establishes the representation.",
        },
    }


def _write_report(path: Path, raw_request: bytes) -> None:
    codex_home = Path(os.environ["CODEX_HOME"])
    auth_file = codex_home / "auth.json"
    report = {
        "pid": os.getpid(),
        "temporary_root": str(codex_home.parent),
        "unrelated_secret_inherited": "MAESTRO_TEST_UNRELATED_SECRET" in os.environ,
        "api_key_present": "MAESTRO_CODEX_API_KEY" in os.environ,
        "auth_present": auth_file.is_file(),
        "auth_contents": auth_file.read_text(encoding="utf-8") if auth_file.is_file() else None,
        "audit_environment_names": [
            name
            for name in sorted(os.environ)
            if "AUDIT" in name or name.startswith("PG") or name in {"DATABASE_URL", "DB_URL"}
        ],
        "environment_names": sorted(os.environ),
        "argv": sys.argv,
        "request": raw_request.decode("utf-8"),
        "config": (
            (codex_home / "config.toml").read_text(encoding="utf-8")
            if (codex_home / "config.toml").is_file()
            else None
        ),
        "open_fds": _open_nonstandard_descriptors(),
        "depth": os.environ.get("MAESTRO_VERIFIER_DEPTH"),
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def _open_nonstandard_descriptors() -> list[int]:
    descriptors: list[int] = []
    for descriptor in range(3, 256):
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        descriptors.append(descriptor)
    return descriptors


def main() -> None:
    """Read one private request and emit the selected bounded response."""

    mode = sys.argv[1]
    report_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    raw_request = sys.stdin.buffer.read()
    json.loads(raw_request)
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if report_path is not None:
        _write_report(report_path, raw_request)
    if mode in {"ignore-term", "sleep"}:
        time.sleep(30)
    elif mode == "malformed":
        sys.stdout.write("not-json")
    elif mode == "oversized":
        sys.stdout.write("x" * 16_384)
    elif mode == "stderr-oversized":
        sys.stderr.write("x" * 16_384)
    elif mode == "runtime-failure":
        sys.stdout.write('{"kind":"failure","category":"runtime"}')
    elif mode == "invalid-output":
        sys.stdout.write('{"kind":"failure","category":"invalid_output"}')
    elif mode == "exit-nonzero":
        raise SystemExit(7)
    else:
        sys.stdout.write(json.dumps(_success()))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
