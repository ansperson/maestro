"""Deterministic stand-in for the Claude Code binary.

Behaves like the real binary's `--print --output-format json` contract so adapter tests
need no provider credential, no network, and no subscription usage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_RESULT: dict[str, Any] = {
    "status": "resolved",
    "answer": "An Order can have many Payments.",
    "confidence": "high",
    "evidence": [{"path": "src/models.py", "line_start": 1, "finding": "The field is a list."}],
    "conflicts": [],
    "reason": "Validated repository evidence establishes the fact.",
}


def main() -> int:  # noqa: PLR0911 - process-mode fixture exits explicitly
    """Emit one envelope for the requested mode, recording argv when asked."""

    mode = sys.argv[1] if len(sys.argv) > 1 else "success"
    if mode == "version":
        sys.stdout.write("2.1.251 (Claude Code)\n")
        return 0
    if mode == "old-version":
        sys.stdout.write("2.0.1 (Claude Code)\n")
        return 0
    if mode == "unparseable-version":
        sys.stdout.write("unknown build\n")
        return 0
    if mode == "version-failure":
        return 1

    sys.stdin.buffer.read()
    if mode == "record-argv":
        record = sys.argv[2]
        with Path(record).open("w", encoding="utf-8") as handle:
            json.dump({"argv": sys.argv[3:], "env": dict(_environment())}, handle)
        sys.stdout.write(_envelope(json.dumps(_RESULT)))
        return 0
    if mode == "runtime-error":
        sys.stdout.write(json.dumps({"is_error": True, "subtype": "error", "result": "boom"}))
        return 0
    if mode == "nonzero":
        return 3
    if mode == "malformed-envelope":
        sys.stdout.write("{")
        return 0
    if mode == "invalid-result":
        sys.stdout.write(_envelope('{"status": "resolved"}'))
        return 0
    if mode == "semantic-violation":
        payload = dict(_RESULT, status="human_decision_required")
        sys.stdout.write(_envelope(json.dumps(payload)))
        return 0
    if mode == "oversized":
        sys.stdout.write("x" * int(sys.argv[2]))
        return 0
    sys.stdout.write(_envelope(json.dumps(_RESULT)))
    return 0


def _environment() -> dict[str, str]:
    import os  # noqa: PLC0415 - keeps the fixture importable without side effects

    return os.environ.copy()


def _envelope(result: str) -> str:
    return json.dumps(
        {
            "is_error": False,
            "subtype": "success",
            "result": result,
            "session_id": "fixture",
            "total_cost_usd": 0.0,
            "usage": {"output_tokens": 1},
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
