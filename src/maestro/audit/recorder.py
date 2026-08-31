"""Application-owned recorder for the successful resolve-codebase-fact Audit tracer."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, TypedDict
from uuid import UUID, uuid4

from pydantic import ValidationError

from maestro.audit.contracts import (
    AuditAuthorityApplicationV1,
    AuditCapability,
    AuditConfidence,
    AuditConflictV1,
    AuditEventType,
    AuditEventV1,
    AuditEvidenceV1,
    AuditExecutionFailureV1,
    AuditExecutionStartV1,
    AuditExecutionV1,
    AuditFailureStage,
    AuditInvestigationCompletionV1,
    AuditResultStatus,
    AuthorityAppliedV1,
    ExecutionFailedV1,
    ExecutionStartedV1,
    InvestigationCompletedV1,
)
from maestro.audit.port import AuditPort, AuditWriteError, AuditWriteFailureKind
from maestro.audit.sanitization import sanitize_audit_text
from maestro.errors import AuditPersistenceError, AuditUnavailableError, ErrorCode, MaestroError
from maestro.model_identity import ModelIdentifier
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

_LOGGER = logging.getLogger("maestro.audit")

type _PersistenceOperation = Callable[[], Awaitable[None]]
type _Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _AuditRetryPolicy:
    max_attempts: int
    total_budget_seconds: float
    backoffs_seconds: tuple[float, ...]


_AUDIT_RETRY_POLICY = _AuditRetryPolicy(
    max_attempts=3,
    total_budget_seconds=5.0,
    backoffs_seconds=(0.1, 0.25),
)


@dataclass(frozen=True, slots=True)
class AuditRetryTiming:
    """Injectable monotonic time and sleep behavior for deterministic retry tests."""

    monotonic_clock: Callable[[], float] = time.monotonic
    sleep: _Sleep = asyncio.sleep


@dataclass(frozen=True, slots=True)
class AuditRuntimeMetadata:
    server_version: str
    runtime_name: str
    runtime_version: str
    model: ModelIdentifier
    prompt_policy_version: str


class _MetadataFields(TypedDict):
    server_version: str
    runtime_name: str
    runtime_version: str
    model: ModelIdentifier
    prompt_policy_version: str


@dataclass(frozen=True, slots=True)
class AuditEvidenceInput:
    """Validated repository evidence supplied to the Audit boundary."""

    path: str
    line_start: int | None
    line_end: int | None
    symbol: str | None
    finding: str


@dataclass(frozen=True, slots=True)
class AuditConflictInput:
    """Validated semantic conflict supplied to the Audit boundary."""

    description: str
    evidence: tuple[AuditEvidenceInput, ...]


@dataclass(frozen=True, slots=True)
class AuditInvestigationCompletionInput:
    """Capability-neutral semantic input for a successful Audit completion."""

    status: AuditResultStatus
    answer: str | None
    confidence: AuditConfidence
    rationale: str
    evidence: tuple[AuditEvidenceInput, ...]
    conflicts: tuple[AuditConflictInput, ...]


@dataclass(frozen=True, slots=True)
class AuditAppliedDecisionInput:
    """One decision or written rule, as it stood when an execution applied it."""

    source_kind: str
    subject: str
    choice: str
    scope: str
    validity: str
    approved_by: str | None
    rationale: str | None
    origin: str
    work_item: str
    source_digest: str


@dataclass(frozen=True, slots=True)
class AuditExecutionHandle:
    """Stable identities generated once for one audited execution lifecycle."""

    audit_id: UUID
    execution_id: UUID
    terminal_event_id: UUID


class AuditRecorder:
    """Construct strict Audit records and delegate only bounded persistence operations."""

    def __init__(
        self,
        port: AuditPort,
        metadata: AuditRuntimeMetadata,
        *,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] | None = None,
        retry_timing: AuditRetryTiming | None = None,
    ) -> None:
        self._port = port
        self._metadata = metadata
        self._id_factory = id_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retry_timing = retry_timing or AuditRetryTiming()

    async def start_resolve_codebase_fact(
        self,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint,
        objective: str,
    ) -> AuditExecutionHandle:
        """Persist the atomic execution/start pair and return immutable terminal identities."""

        return await self._start_execution(
            AuditCapability.RESOLVE_CODEBASE_FACT,
            repository,
            fingerprint,
            objective,
        )

    async def start_authority_check(
        self,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint,
        objective: str,
    ) -> AuditExecutionHandle:
        """Begin the audited execution of one authority evaluation."""

        return await self._start_execution(
            AuditCapability.DECISION_AUTHORITY,
            repository,
            fingerprint,
            objective,
        )

    async def record_authority_applied(
        self,
        handle: AuditExecutionHandle,
        repository: AuthorizedRepository,
        applied: AuditAppliedDecisionInput,
    ) -> None:
        """Persist the decision an execution applied, with its content as it stood.

        The content is captured here rather than referenced, so a later edit to the work item
        cannot change what the Trail says was authorized.
        """

        try:
            payload = AuthorityAppliedV1(
                source_kind=applied.source_kind,
                subject=sanitize_audit_text(applied.subject, repository.root),
                choice=sanitize_audit_text(applied.choice, repository.root),
                scope=sanitize_audit_text(applied.scope, repository.root),
                validity=applied.validity,
                approved_by=self._sanitize_optional(applied.approved_by, repository.root),
                rationale=self._sanitize_optional(applied.rationale, repository.root),
                origin=sanitize_audit_text(applied.origin, repository.root),
                work_item=sanitize_audit_text(applied.work_item, repository.root),
                source_digest=applied.source_digest,
                **self._metadata_fields(),
            )
            event = AuditEventV1(
                event_id=handle.terminal_event_id,
                audit_id=handle.audit_id,
                sequence=2,
                event_type=AuditEventType.AUTHORITY_APPLIED,
                occurred_at=self._clock(),
                payload=payload,
            )
            record = AuditAuthorityApplicationV1(
                execution_id=handle.execution_id,
                event=event,
                content_hash=event.content_hash(),
            )
        except ValidationError:
            self._raise_public(AuditPersistenceError(), "authority", 0)
        await self._persist("authority", lambda: self._port.apply_authority(record))

    async def _start_execution(
        self,
        capability: AuditCapability,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint,
        objective: str,
    ) -> AuditExecutionHandle:
        handle = AuditExecutionHandle(
            audit_id=self._id_factory(),
            execution_id=self._id_factory(),
            terminal_event_id=self._id_factory(),
        )
        sanitized_objective = sanitize_audit_text(objective, repository.root)
        try:
            execution = AuditExecutionV1(
                audit_id=handle.audit_id,
                execution_id=handle.execution_id,
                capability=capability,
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
                    objective=sanitized_objective,
                    **self._metadata_fields(),
                ),
            )
            record = AuditExecutionStartV1(
                execution=execution,
                event=event,
                content_hash=event.content_hash(),
            )
        except ValidationError:
            self._raise_public(AuditPersistenceError(), "start", 0)
        await self._persist("start", lambda: self._port.start_execution(record))
        return handle

    async def record_investigation_completed(
        self,
        handle: AuditExecutionHandle,
        repository: AuthorizedRepository,
        result: AuditInvestigationCompletionInput,
    ) -> None:
        """Persist one accepted semantic result as the sequence-two completion."""

        sanitized_answer = self._sanitize_optional(result.answer, repository.root)
        sanitized_rationale = sanitize_audit_text(result.rationale, repository.root)
        try:
            payload = InvestigationCompletedV1(
                status=result.status,
                answer=sanitized_answer,
                confidence=result.confidence,
                rationale=sanitized_rationale,
                evidence=tuple(self._evidence(item, repository.root) for item in result.evidence),
                conflicts=tuple(self._conflict(item, repository.root) for item in result.conflicts),
                **self._metadata_fields(),
            )
            event = AuditEventV1(
                event_id=handle.terminal_event_id,
                audit_id=handle.audit_id,
                sequence=2,
                event_type=AuditEventType.INVESTIGATION_COMPLETED,
                occurred_at=self._clock(),
                payload=payload,
            )
            record = AuditInvestigationCompletionV1(
                execution_id=handle.execution_id,
                event=event,
                content_hash=event.content_hash(),
            )
        except ValidationError:
            self._raise_public(AuditPersistenceError(), "completion", 0)
        await self._persist("completion", lambda: self._port.complete_investigation(record))

    async def record_execution_failed(
        self,
        handle: AuditExecutionHandle,
        error_code: ErrorCode,
        failure_stage: AuditFailureStage,
    ) -> None:
        """Persist one safe typed operational failure as the sequence-two terminal event."""

        try:
            event = AuditEventV1(
                event_id=handle.terminal_event_id,
                audit_id=handle.audit_id,
                sequence=2,
                event_type=AuditEventType.EXECUTION_FAILED,
                occurred_at=self._clock(),
                payload=ExecutionFailedV1(
                    error_code=error_code,
                    failure_stage=failure_stage,
                    **self._metadata_fields(),
                ),
            )
            record = AuditExecutionFailureV1(
                execution_id=handle.execution_id,
                event=event,
                content_hash=event.content_hash(),
            )
        except ValidationError:
            self._raise_public(AuditPersistenceError(), "failure", 0)
        await self._persist("failure", lambda: self._port.fail_execution(record))

    def abort_execution_failure(self, handle: AuditExecutionHandle) -> None:
        """Abort only this execution's active failure write without awaiting adapter I/O."""

        self._port.abort_execution_failure(handle.terminal_event_id)

    async def _persist(self, operation_name: str, operation: _PersistenceOperation) -> None:
        policy = _AUDIT_RETRY_POLICY
        started = self._retry_timing.monotonic_clock()
        attempts = 0
        ambiguous_outcome = False
        for attempt in range(1, policy.max_attempts + 1):
            remaining = self._remaining_budget(started)
            if remaining <= 0:
                self._raise_exhausted(operation_name, attempts, ambiguous_outcome)
            attempts = attempt
            failure = await self._attempt(operation, remaining)
            if failure is None:
                return
            if failure is AuditWriteFailureKind.PERMANENT:
                self._raise_public(AuditPersistenceError(), operation_name, attempts)
            ambiguous_outcome |= failure is AuditWriteFailureKind.AMBIGUOUS
            if attempt == policy.max_attempts:
                self._raise_exhausted(operation_name, attempts, ambiguous_outcome)
            backoff = policy.backoffs_seconds[attempt - 1]
            remaining = self._remaining_budget(started)
            if backoff >= remaining:
                self._raise_exhausted(operation_name, attempts, ambiguous_outcome)
            await self._backoff(
                backoff,
                remaining,
                operation_name,
                attempts,
                ambiguous_outcome,
            )

    @staticmethod
    async def _attempt(
        operation: _PersistenceOperation,
        remaining: float,
    ) -> AuditWriteFailureKind | None:
        try:
            async with asyncio.timeout(remaining):
                await operation()
        except AuditWriteError as exc:
            return exc.kind
        except TimeoutError:
            return AuditWriteFailureKind.AMBIGUOUS
        except asyncio.CancelledError:
            raise
        except Exception:
            return AuditWriteFailureKind.PERMANENT
        return None

    async def _backoff(
        self,
        backoff: float,
        remaining: float,
        operation_name: str,
        attempts: int,
        ambiguous_outcome: bool,
    ) -> None:
        try:
            async with asyncio.timeout(remaining):
                await self._retry_timing.sleep(backoff)
        except TimeoutError:
            self._raise_exhausted(operation_name, attempts, ambiguous_outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._raise_public(AuditPersistenceError(), operation_name, attempts)

    def _remaining_budget(self, started: float) -> float:
        return _AUDIT_RETRY_POLICY.total_budget_seconds - (
            self._retry_timing.monotonic_clock() - started
        )

    @classmethod
    def _raise_exhausted(
        cls,
        operation_name: str,
        attempts: int,
        ambiguous_outcome: bool,
    ) -> NoReturn:
        error: MaestroError = (
            AuditPersistenceError() if ambiguous_outcome else AuditUnavailableError()
        )
        cls._raise_public(error, operation_name, attempts)

    @staticmethod
    def _raise_public(error: MaestroError, operation_name: str, attempts: int) -> NoReturn:
        _LOGGER.warning(
            "audit persistence failed",
            extra={
                "metadata": {
                    "error_code": error.code.value,
                    "audit_operation": operation_name,
                    "attempts": attempts,
                    "retry_count": max(0, attempts - 1),
                }
            },
        )
        raise error from None

    def _metadata_fields(self) -> _MetadataFields:
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
    def _evidence(value: AuditEvidenceInput, repository_root: Path) -> AuditEvidenceV1:
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
    def _conflict(cls, value: AuditConflictInput, repository_root: Path) -> AuditConflictV1:
        return AuditConflictV1(
            description=sanitize_audit_text(value.description, repository_root),
            evidence=tuple(cls._evidence(item, repository_root) for item in value.evidence),
        )
