"""Isolated trusted entry point for Git top-level canonicalization."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

from maestro.repository.fingerprint_protocol import (
    FINGERPRINT_PROTOCOL_VERSION,
    MAX_CANONICAL_PATH_RESULT_BYTES,
    MAX_FINGERPRINT_REQUEST_BYTES,
    CanonicalPathRequestV1,
    CanonicalPathResultV1,
)


def main() -> int:
    """Resolve one directory and emit only its strict protocol result."""

    try:
        raw_request = sys.stdin.buffer.read(MAX_FINGERPRINT_REQUEST_BYTES + 1)
        if len(raw_request) > MAX_FINGERPRINT_REQUEST_BYTES:
            return 1
        request = CanonicalPathRequestV1.model_validate_json(raw_request, strict=True)
        canonical = Path(request.path).resolve(strict=True)
        if not canonical.is_dir():
            return 1
        result = CanonicalPathResultV1(
            protocol_version=FINGERPRINT_PROTOCOL_VERSION,
            canonical_path=str(canonical),
        )
        encoded_result = result.model_dump_json().encode("utf-8")
        if len(encoded_result) > MAX_CANONICAL_PATH_RESULT_BYTES:
            return 1
        sys.stdout.buffer.write(encoded_result)
        sys.stdout.buffer.flush()
    except (OSError, RuntimeError, ValidationError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
