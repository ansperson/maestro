"""Isolated trusted entry point for bounded repository fingerprint scans."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

from maestro.repository.fingerprint_protocol import (
    FINGERPRINT_PROTOCOL_VERSION,
    MAX_FINGERPRINT_REQUEST_BYTES,
    MAX_FINGERPRINT_RESULT_BYTES,
    FingerprintFileV1,
    FingerprintScanRequestV1,
    FingerprintScanResultV1,
)
from maestro.repository.fingerprint_scan import scan_repository


def main() -> int:
    """Read one request and emit one protocol result without diagnostic output."""

    try:
        raw_request = sys.stdin.buffer.read(MAX_FINGERPRINT_REQUEST_BYTES + 1)
        if len(raw_request) > MAX_FINGERPRINT_REQUEST_BYTES:
            return 1
        request = FingerprintScanRequestV1.model_validate_json(raw_request, strict=True)
        root = Path(request.root)
        if root.resolve(strict=True) != root or not root.is_dir():
            return 1
        outcome = scan_repository(
            root,
            max_repository_files=request.max_repository_files,
            max_repository_bytes=request.max_repository_bytes,
            max_file_bytes=request.max_file_bytes,
        )
        result = FingerprintScanResultV1(
            protocol_version=FINGERPRINT_PROTOCOL_VERSION,
            files=tuple(
                FingerprintFileV1(
                    relative_path=relative,
                    token=scanned.state.token,
                    content_digest=scanned.state.content_digest,
                    line_count=scanned.state.line_count,
                    size=scanned.state.size,
                    consumed_bytes=scanned.consumed_bytes,
                )
                for relative, scanned in outcome.files.items()
            ),
            file_count=len(outcome.files),
            consumed_bytes=outcome.consumed_bytes,
            truncated=outcome.truncated,
        )
        encoded_result = result.model_dump_json().encode("utf-8")
        if len(encoded_result) > MAX_FINGERPRINT_RESULT_BYTES:
            return 1
        sys.stdout.buffer.write(encoded_result)
        sys.stdout.buffer.flush()
    except (OSError, RuntimeError, ValidationError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
