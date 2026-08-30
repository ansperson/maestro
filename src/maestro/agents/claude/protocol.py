"""Private bounded protocol between Maestro and the Claude Code binary."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_CLAUDE_RESULT_BYTES = 1_048_576


class ClaudeResultEnvelope(BaseModel):
    """The subset of the binary's JSON envelope Maestro is permitted to read.

    The binary reports far more than this. Only these fields are read, so envelope
    additions cannot reach Maestro and diagnostic detail cannot leak into a result.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    is_error: bool
    subtype: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = ""
    result: Annotated[str, Field(max_length=MAX_CLAUDE_RESULT_BYTES)] = ""
