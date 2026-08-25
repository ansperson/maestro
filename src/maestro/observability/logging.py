"""Structured stderr logging without sensitive payloads."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import cast, override


class JsonFormatter(logging.Formatter):
    """Small JSON formatter limited to approved metadata fields."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        raw_metadata: object = getattr(record, "metadata", None)
        if isinstance(raw_metadata, dict):
            metadata = cast(dict[object, object], raw_metadata)
            payload.update(
                {
                    str(key): value
                    for key, value in metadata.items()
                    if isinstance(value, str | int | float | bool) or value is None
                }
            )
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(level: str) -> None:
    """Configure application logs on stderr; stdout remains MCP-only."""

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
