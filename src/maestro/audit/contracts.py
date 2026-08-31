"""Strict, versioned persistence contracts for the Audit tracer."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from maestro.errors import ErrorCode
from maestro.model_identity import ModelIdentifier

MAX_AUDIT_OBJECTIVE_CHARS = 4_000
MAX_AUDIT_ANSWER_CHARS = 8_000
MAX_AUDIT_RATIONALE_CHARS = 4_000
MAX_AUDIT_FINDING_CHARS = 1_000
MAX_AUDIT_SYMBOL_CHARS = 256
MAX_AUDIT_EVIDENCE_ITEMS = 20
MAX_AUDIT_CONFLICTS = 10
MAX_AUDIT_CONFLICT_EVIDENCE_ITEMS = 10
MAX_AUDIT_AUTHORITY_CHARS = 500
MAX_AUDIT_AUTHORITY_CHOICE_CHARS = 1_000
MAX_AUDIT_PAYLOAD_BYTES = 65_536
_TERMINAL_SEQUENCE = 2

_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_VersionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
_AuthorityText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUDIT_AUTHORITY_CHARS),
]
_AuthorityChoiceText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_AUDIT_AUTHORITY_CHOICE_CHARS
    ),
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AuditEventType(StrEnum):
    """Semantic event types implemented by the v1 Audit tracer."""

    EXECUTION_STARTED = "execution.started"
    AUTHORITY_APPLIED = "authority.applied"
    INVESTIGATION_COMPLETED = "investigation.completed"
    EXECUTION_FAILED = "execution.failed"

    @property
    def is_terminal(self) -> bool:
        """Report whether this type ends an execution.

        A decision-authority execution ends when the decision it applied is recorded, so
        `authority.applied` is terminal rather than a step inside a longer run. When durable
        Jobs make an applied decision part of a larger execution, that is the change that
        moves it, and it is a deliberate one.
        """

        return self is not AuditEventType.EXECUTION_STARTED


class AuditFailureStage(StrEnum):
    """Bounded lifecycle stages safe to persist for operational failures."""

    AUTHORITY = "authority"
    INVESTIGATION = "investigation"
    VALIDATION = "validation"
    TERMINAL_PERSISTENCE = "terminal_persistence"


class AuditCapability(StrEnum):
    """The capabilities whose executions Audit records."""

    RESOLVE_CODEBASE_FACT = "resolve_codebase_fact"
    DECISION_AUTHORITY = "decision_authority"


class AuditResultStatus(StrEnum):
    RESOLVED = "resolved"
    UNCERTAIN = "uncertain"
    HUMAN_DECISION_REQUIRED = "human_decision_required"


class AuditConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuditEvidenceV1(_StrictFrozenModel):
    path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_024)]
    line_start: Annotated[int | None, Field(ge=1)] = None
    line_end: Annotated[int | None, Field(ge=1)] = None
    symbol: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUDIT_SYMBOL_CHARS),
    ] = None
    finding: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUDIT_FINDING_CHARS),
    ]

    @model_validator(mode="after")
    def validate_anchor(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            "\x00" in self.path
            or "\\" in self.path
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != self.path
        ):
            raise ValueError("Audit evidence paths must be normalized and repository-relative")
        if self.line_end is not None and self.line_start is None:
            raise ValueError("line_end requires line_start")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_start > self.line_end
        ):
            raise ValueError("line_start must not exceed line_end")
        return self


class AuditConflictV1(_StrictFrozenModel):
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUDIT_FINDING_CHARS),
    ]
    evidence: tuple[AuditEvidenceV1, ...] = Field(max_length=MAX_AUDIT_CONFLICT_EVIDENCE_ITEMS)


class ExecutionStartedV1(_StrictFrozenModel):
    objective: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_AUDIT_OBJECTIVE_CHARS,
        ),
    ]
    server_version: _VersionText
    runtime_name: _VersionText
    runtime_version: _VersionText
    model: ModelIdentifier
    prompt_policy_version: _VersionText


class InvestigationCompletedV1(_StrictFrozenModel):
    status: AuditResultStatus
    answer: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUDIT_ANSWER_CHARS),
    ] = None
    confidence: AuditConfidence
    rationale: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_AUDIT_RATIONALE_CHARS,
        ),
    ]
    evidence: tuple[AuditEvidenceV1, ...] = Field(max_length=MAX_AUDIT_EVIDENCE_ITEMS)
    conflicts: tuple[AuditConflictV1, ...] = Field(max_length=MAX_AUDIT_CONFLICTS)
    server_version: _VersionText
    runtime_name: _VersionText
    runtime_version: _VersionText
    model: ModelIdentifier
    prompt_policy_version: _VersionText

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        if self.status is AuditResultStatus.RESOLVED and (self.answer is None or not self.evidence):
            raise ValueError("resolved Audit results require an answer and evidence")
        if self.status is AuditResultStatus.HUMAN_DECISION_REQUIRED and self.answer is not None:
            raise ValueError("human-decision Audit results cannot contain an answer")
        return self


class ExecutionFailedV1(_StrictFrozenModel):
    error_code: ErrorCode
    failure_stage: AuditFailureStage
    server_version: _VersionText
    runtime_name: _VersionText
    runtime_version: _VersionText
    model: ModelIdentifier
    prompt_policy_version: _VersionText


class AuthorityAppliedV1(_StrictFrozenModel):
    """One decision or written rule as it stood when an execution applied it.

    The content is captured rather than referenced. A work item is editable by design, so a
    reference alone would let a later edit change what the Trail says was authorized. The
    digest makes such an edit detectable rather than invisible.

    This is the only authority event Audit records. Requesting, proposing, approving,
    rejecting, and superseding a decision are coordination, and ADR-0004 gives coordination
    to Work Management.
    """

    source_kind: _AuthorityText
    subject: _AuthorityText
    choice: _AuthorityChoiceText
    scope: _AuthorityText
    validity: _AuthorityText
    approved_by: _AuthorityText | None = None
    rationale: Annotated[
        str | None,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=MAX_AUDIT_RATIONALE_CHARS
        ),
    ] = None
    origin: _AuthorityText
    work_item: _AuthorityText
    source_digest: _Digest
    server_version: _VersionText
    runtime_name: _VersionText
    runtime_version: _VersionText
    model: ModelIdentifier
    prompt_policy_version: _VersionText


type AuditPayloadV1 = (
    ExecutionStartedV1 | AuthorityAppliedV1 | InvestigationCompletedV1 | ExecutionFailedV1
)


_PAYLOAD_BY_EVENT_TYPE: dict[AuditEventType, type[BaseModel]] = {
    AuditEventType.EXECUTION_STARTED: ExecutionStartedV1,
    AuditEventType.AUTHORITY_APPLIED: AuthorityAppliedV1,
    AuditEventType.INVESTIGATION_COMPLETED: InvestigationCompletedV1,
    AuditEventType.EXECUTION_FAILED: ExecutionFailedV1,
}


class AuditEventV1(_StrictFrozenModel):
    """Immutable relational envelope plus one strict v1 semantic payload."""

    event_id: UUID
    audit_id: UUID
    sequence: Literal[1, 2]
    event_type: AuditEventType
    event_version: Literal[1] = 1
    occurred_at: datetime
    payload: AuditPayloadV1

    @model_validator(mode="after")
    def validate_event_shape(self) -> Self:
        """Require the type, position, and payload to agree.

        Sequence one is the start and nothing else is; every terminal type takes the second
        position. An execution therefore has exactly one outcome, whatever kind it is.
        """

        expected_payload = _PAYLOAD_BY_EVENT_TYPE[self.event_type]
        expected_sequence = _TERMINAL_SEQUENCE if self.event_type.is_terminal else 1
        if self.sequence != expected_sequence or not isinstance(self.payload, expected_payload):
            raise ValueError("Audit event type, sequence, and payload do not agree")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("Audit event timestamps must be timezone-aware")
        if len(self.payload.model_dump_json().encode("utf-8")) > MAX_AUDIT_PAYLOAD_BYTES:
            raise ValueError("Audit event payload exceeds its byte limit")
        return self

    def content_hash(self) -> str:
        """Hash canonical immutable application content; database timestamps are excluded."""

        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditExecutionV1(_StrictFrozenModel):
    audit_id: UUID
    execution_id: UUID
    capability: AuditCapability = AuditCapability.RESOLVE_CODEBASE_FACT
    repository_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
    repository_fingerprint: _Digest


class AuditExecutionStartV1(_StrictFrozenModel):
    execution: AuditExecutionV1
    event: AuditEventV1
    content_hash: _Digest

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.execution.audit_id != self.event.audit_id:
            raise ValueError("execution and start event Audit identities must agree")
        if self.event.event_type is not AuditEventType.EXECUTION_STARTED:
            raise ValueError("start record requires execution.started")
        if self.content_hash != self.event.content_hash():
            raise ValueError("start record content hash must match its immutable event")
        return self


class AuditInvestigationCompletionV1(_StrictFrozenModel):
    execution_id: UUID
    event: AuditEventV1
    content_hash: _Digest

    @model_validator(mode="after")
    def validate_type(self) -> Self:
        if self.event.event_type is not AuditEventType.INVESTIGATION_COMPLETED:
            raise ValueError("completion record requires investigation.completed")
        if self.content_hash != self.event.content_hash():
            raise ValueError("completion record content hash must match its immutable event")
        return self


class AuditExecutionFailureV1(_StrictFrozenModel):
    execution_id: UUID
    event: AuditEventV1
    content_hash: _Digest

    @model_validator(mode="after")
    def validate_type(self) -> Self:
        if self.event.event_type is not AuditEventType.EXECUTION_FAILED:
            raise ValueError("failure record requires execution.failed")
        if self.content_hash != self.event.content_hash():
            raise ValueError("failure record content hash must match its immutable event")
        return self


class AuditAuthorityApplicationV1(_StrictFrozenModel):
    """One applied decision recorded against an execution already in progress."""

    execution_id: UUID
    event: AuditEventV1
    content_hash: _Digest

    @model_validator(mode="after")
    def validate_type(self) -> Self:
        if self.event.event_type is not AuditEventType.AUTHORITY_APPLIED:
            raise ValueError("authority record requires authority.applied")
        if self.content_hash != self.event.content_hash():
            raise ValueError("authority record content hash must match its immutable event")
        return self
