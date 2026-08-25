"""Strict public and agent-output contracts for ``resolve_codebase_fact``."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_REPOSITORY_PATH_CHARS = 4_096
MAX_QUESTION_CHARS = 4_000
MAX_CONTEXT_CHARS = 8_000
MAX_ANSWER_CHARS = 8_000
MAX_REASON_CHARS = 4_000
MAX_FINDING_CHARS = 1_000
MAX_SYMBOL_CHARS = 256
MAX_EVIDENCE_ITEMS = 20
MAX_CONFLICTS = 10
MAX_CONFLICT_EVIDENCE_ITEMS = 10
_CONTROL_CHARACTER_LIMIT = 32

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    """Base model for untrusted external data."""

    model_config = ConfigDict(extra="forbid", strict=True)


class VerificationStatus(StrEnum):
    """Semantic outcome of a completed repository investigation."""

    RESOLVED = "resolved"
    UNCERTAIN = "uncertain"
    HUMAN_DECISION_REQUIRED = "human_decision_required"


class Confidence(StrEnum):
    """Strength of the evidence, not a probability or correctness guarantee."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ResolveCodebaseFactRequest(StrictModel):
    """Public request for one objective repository fact."""

    repository_path: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_REPOSITORY_PATH_CHARS,
        ),
    ]
    question: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS),
    ]
    context: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CONTEXT_CHARS),
    ] = None

    @model_validator(mode="after")
    def reject_unsafe_controls(self) -> Self:
        """Reject NUL and non-whitespace C0 controls at the MCP boundary."""

        for field_name in ("repository_path", "question", "context"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if any(ord(char) < _CONTROL_CHARACTER_LIMIT and char not in "\n\r\t" for char in value):
                msg = f"{field_name} contains an invalid control character"
                raise ValueError(msg)
        return self


class Evidence(StrictModel):
    """One concise, repository-relative evidence anchor."""

    path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_024)]
    line_start: Annotated[int | None, Field(ge=1)] = None
    line_end: Annotated[int | None, Field(ge=1)] = None
    symbol: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SYMBOL_CHARS),
    ] = None
    finding: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FINDING_CHARS),
    ]

    @model_validator(mode="after")
    def validate_anchor(self) -> Self:
        """Enforce a normalized relative path and coherent line range."""

        if "\x00" in self.path or "\\" in self.path:
            raise ValueError("evidence path must be a normalized POSIX path")
        path = PurePosixPath(self.path)
        if path.is_absolute() or self.path != path.as_posix() or ".." in path.parts:
            raise ValueError("evidence path must be normalized and repository-relative")
        if self.line_end is not None and self.line_start is None:
            raise ValueError("line_end requires line_start")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_start > self.line_end
        ):
            raise ValueError("line_start must be less than or equal to line_end")
        return self


class Conflict(StrictModel):
    """A contradiction that prevents a reliable factual conclusion."""

    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FINDING_CHARS),
    ]
    evidence: list[Evidence] = Field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list, max_length=MAX_CONFLICT_EVIDENCE_ITEMS
    )


class VerificationResult(StrictModel):
    """Public semantic result returned only after deterministic validation."""

    status: VerificationStatus
    answer: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ANSWER_CHARS),
    ] = None
    confidence: Confidence
    evidence: list[Evidence] = Field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list, max_length=MAX_EVIDENCE_ITEMS
    )
    conflicts: list[Conflict] = Field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list, max_length=MAX_CONFLICTS
    )
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REASON_CHARS),
    ]

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        """Enforce status-dependent factual-result invariants."""

        if self.status is VerificationStatus.RESOLVED:
            if self.answer is None:
                raise ValueError("resolved results require an answer")
            if not self.evidence:
                raise ValueError("resolved results require evidence")
        if self.status is VerificationStatus.HUMAN_DECISION_REQUIRED and self.answer is not None:
            raise ValueError("human_decision_required results must not include an answer")
        return self


def human_decision_result(reason: str) -> VerificationResult:
    """Build the only valid response for a non-factual question."""

    return VerificationResult(
        status=VerificationStatus.HUMAN_DECISION_REQUIRED,
        answer=None,
        confidence=Confidence.HIGH,
        evidence=[],
        conflicts=[],
        reason=reason,
    )
