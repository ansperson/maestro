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


def _write_report(path: Path) -> None:
    codex_home = Path(os.environ["CODEX_HOME"])
    auth_file = codex_home / "auth.json"
    report = {
        "pid": os.getpid(),
        "temporary_root": str(codex_home.parent),
        "unrelated_secret_inherited": "MAESTRO_TEST_UNRELATED_SECRET" in os.environ,
        "api_key_present": "MAESTRO_CODEX_API_KEY" in os.environ,
        "auth_present": auth_file.is_file(),
        "auth_contents": auth_file.read_text(encoding="utf-8") if auth_file.is_file() else None,
        "audit_database_url_present": "MAESTRO_AUDIT_DATABASE_URL" in os.environ,
        "depth": os.environ.get("MAESTRO_VERIFIER_DEPTH"),
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def main() -> None:
    """Read one private request and emit the selected bounded response."""

    mode = sys.argv[1]
    report_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    json.loads(sys.stdin.buffer.read())
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if report_path is not None:
        _write_report(report_path)
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
