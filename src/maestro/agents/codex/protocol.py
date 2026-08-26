"""Private bounded protocol between Maestro and its isolated Codex worker."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from maestro.capabilities.resolve_codebase_fact.contracts import StrictModel, VerificationResult
from maestro.model_identity import ModelIdentifier


class CodexWorkerRequest(StrictModel):
    """Non-secret request sent to the isolated worker over stdin."""

    repository_root: Path
    question: str
    context: str | None
    model: ModelIdentifier
    max_output_bytes: Annotated[int, Field(ge=1_024, le=1_048_576)]


class CodexWorkerSuccess(StrictModel):
    """Successful worker envelope."""

    kind: Literal["success"] = "success"
    result: VerificationResult


class CodexWorkerFailure(StrictModel):
    """Sanitized worker failure envelope."""

    kind: Literal["failure"] = "failure"
    category: Literal["runtime", "invalid_output"]
