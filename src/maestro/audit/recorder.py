"""Application-owned recorder for the successful resolve-codebase-fact Audit tracer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from maestro.audit.contracts import (
    AuditConfidence,
    AuditConflictV1,
    AuditEventType,
    AuditEventV1,
    AuditEvidenceV1,
    AuditExecutionStartV1,
    AuditExecutionV1,
    AuditInvestigationCompletionV1,
    AuditResultStatus,
    ExecutionStartedV1,
    InvestigationCompletedV1,
)
from maestro.audit.port import AuditPort
from maestro.audit.sanitization import sanitize_audit_text
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Conflict,
    Evidence,
    VerificationResult,
)
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint


@dataclass(frozen=True, slots=True)
class AuditRuntimeMetadata:
    server_version: str
    runtime_name: str
    runtime_version: str
    model: str
    prompt_policy_version: str


@dataclass(frozen=True, slots=True)
class AuditExecutionHandle:
    """Stable identities generated once for one successful-tracer lifecycle."""

    audit_id: UUID
    execution_id: UUID
    completion_event_id: UUID


class AuditRecorder:
    """Construct strict Audit records and delegate only bounded persistence operations."""

    def __init__(
        self,
        port: AuditPort,
        metadata: AuditRuntimeMetadata,
        *,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._port = port
        self._metadata = metadata
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def start_resolve_codebase_fact(
        self,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint,
        objective: str,
    ) -> AuditExecutionHandle:
        """Persist the atomic execution/start pair and return immutable terminal identities."""

        handle = AuditExecutionHandle(
            audit_id=self._id_factory(),
            execution_id=self._id_factory(),
            completion_event_id=self._id_factory(),
        )
        execution = AuditExecutionV1(
            audit_id=handle.audit_id,
            execution_id=handle.execution_id,
            repository_id=repository.repository_id,
            repository_fingerprint=fingerprint.digest,
        )
        event = AuditEventV1(
            event_id=self._id_factory(),
            audit_id=handle.audit_id,
            sequence=1,
            event_type=AuditEventType.EXECUTION_STARTED,
            occurred_at=self._clock(),
            payload=ExecutionStartedV1(
                objective=sanitize_audit_text(objective, repository.root),
                **self._metadata_fields(),
            ),
        )
        await self._port.start_execution(AuditExecutionStartV1(execution=execution, event=event))
        return handle

    async def record_investigation_completed(
        self,
        handle: AuditExecutionHandle,
        repository: AuthorizedRepository,
        result: VerificationResult,
    ) -> None:
        """Persist one accepted semantic result as the sequence-two completion."""

        payload = InvestigationCompletedV1(
            status=AuditResultStatus(result.status.value),
            answer=self._sanitize_optional(result.answer, repository.root),
            confidence=AuditConfidence(result.confidence.value),
            rationale=sanitize_audit_text(result.reason, repository.root),
            evidence=tuple(self._evidence(item, repository.root) for item in result.evidence),
            conflicts=tuple(self._conflict(item, repository.root) for item in result.conflicts),
            **self._metadata_fields(),
        )
        event = AuditEventV1(
            event_id=handle.completion_event_id,
            audit_id=handle.audit_id,
            sequence=2,
            event_type=AuditEventType.INVESTIGATION_COMPLETED,
            occurred_at=self._clock(),
            payload=payload,
        )
        await self._port.complete_investigation(AuditInvestigationCompletionV1(event=event))

    def _metadata_fields(self) -> dict[str, str]:
        return {
            "server_version": self._metadata.server_version,
            "runtime_name": self._metadata.runtime_name,
            "runtime_version": self._metadata.runtime_version,
            "model": self._metadata.model,
            "prompt_policy_version": self._metadata.prompt_policy_version,
        }

    @staticmethod
    def _sanitize_optional(value: str | None, repository_root: Path) -> str | None:
        return sanitize_audit_text(value, repository_root) if value is not None else None

    @staticmethod
    def _evidence(value: Evidence, repository_root: Path) -> AuditEvidenceV1:
        return AuditEvidenceV1(
            path=value.path,
            line_start=value.line_start,
            line_end=value.line_end,
            symbol=(
                sanitize_audit_text(value.symbol, repository_root)
                if value.symbol is not None
                else None
            ),
            finding=sanitize_audit_text(value.finding, repository_root),
        )

    @classmethod
    def _conflict(cls, value: Conflict, repository_root: Path) -> AuditConflictV1:
        return AuditConflictV1(
            description=sanitize_audit_text(value.description, repository_root),
            evidence=tuple(cls._evidence(item, repository_root) for item in value.evidence),
        )
