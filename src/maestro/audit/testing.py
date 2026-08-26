"""Typed deterministic Audit adapter for application tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from maestro.audit.contracts import (
    AuditEventV1,
    AuditExecutionFailureV1,
    AuditExecutionStartV1,
    AuditInvestigationCompletionV1,
)
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind
from maestro.audit.recorder import AuditRecorder, AuditRetryTiming, AuditRuntimeMetadata
from maestro.model_identity import ModelIdentifier

type StartHook = Callable[[AuditExecutionStartV1], Awaitable[None] | None]
type CompletionHook = Callable[[AuditInvestigationCompletionV1], Awaitable[None] | None]
type FailureHook = Callable[[AuditExecutionFailureV1], Awaitable[None] | None]
type FailureAbortHook = Callable[[UUID], None]


class FakeAuditPort:
    """Record strict Audit writes without introducing a second persistence backend."""

    def __init__(
        self,
        *,
        on_start: StartHook | None = None,
        on_completion: CompletionHook | None = None,
        on_failure: FailureHook | None = None,
        on_failure_abort: FailureAbortHook | None = None,
    ) -> None:
        self._on_start = on_start
        self._on_completion = on_completion
        self._on_failure = on_failure
        self._on_failure_abort = on_failure_abort
        self.start_attempts: list[AuditExecutionStartV1] = []
        self.completion_attempts: list[AuditInvestigationCompletionV1] = []
        self.failure_attempts: list[AuditExecutionFailureV1] = []
        self.starts: list[AuditExecutionStartV1] = []
        self.completions: list[AuditInvestigationCompletionV1] = []
        self.failures: list[AuditExecutionFailureV1] = []
        self.failure_aborts: list[UUID] = []

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        self.start_attempts.append(record)
        if self._on_start is not None:
            outcome = self._on_start(record)
            if isinstance(outcome, Awaitable):
                await outcome
        self._store_start(record)

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        self.completion_attempts.append(record)
        if self._on_completion is not None:
            outcome = self._on_completion(record)
            if isinstance(outcome, Awaitable):
                await outcome
        self._store_terminal(record)

    async def fail_execution(self, record: AuditExecutionFailureV1) -> None:
        self.failure_attempts.append(record)
        if self._on_failure is not None:
            outcome = self._on_failure(record)
            if isinstance(outcome, Awaitable):
                await outcome
        self._store_terminal(record)

    def abort_execution_failure(self, event_id: UUID) -> None:
        """Record and synchronously signal cancellation of one active fake write."""

        self.failure_aborts.append(event_id)
        if self._on_failure_abort is not None:
            self._on_failure_abort(event_id)

    def _store_start(self, record: AuditExecutionStartV1) -> None:
        execution_conflicts = [
            existing
            for existing in self.starts
            if existing.execution.audit_id == record.execution.audit_id
            or existing.execution.execution_id == record.execution.execution_id
        ]
        event_conflicts = self._event_conflicts(record.event)
        if not execution_conflicts and not event_conflicts:
            self.starts.append(record)
            return
        if execution_conflicts == [record] and event_conflicts == [record.event]:
            return
        raise AuditWriteError(AuditWriteFailureKind.PERMANENT)

    def _store_terminal(
        self,
        record: AuditInvestigationCompletionV1 | AuditExecutionFailureV1,
    ) -> None:
        event_conflicts = self._event_conflicts(record.event)
        if not event_conflicts:
            if isinstance(record, AuditInvestigationCompletionV1):
                self.completions.append(record)
            else:
                self.failures.append(record)
            return
        existing_records = (*self.completions, *self.failures)
        if event_conflicts == [record.event] and record in existing_records:
            return
        raise AuditWriteError(AuditWriteFailureKind.PERMANENT)

    def _event_conflicts(self, event: AuditEventV1) -> list[AuditEventV1]:
        events = [record.event for record in self.starts]
        events.extend(record.event for record in self.completions)
        events.extend(record.event for record in self.failures)
        return [
            existing
            for existing in events
            if existing.event_id == event.event_id
            or (existing.audit_id == event.audit_id and existing.sequence == event.sequence)
        ]


def fake_audit_recorder(
    port: FakeAuditPort | None = None,
    *,
    retry_timing: AuditRetryTiming | None = None,
) -> AuditRecorder:
    """Build a recorder with stable non-secret test metadata."""

    return AuditRecorder(
        port or FakeAuditPort(),
        AuditRuntimeMetadata(
            server_version="1.0.0",
            runtime_name="fake",
            runtime_version="1.0.0",
            model=ModelIdentifier("fake-model"),
            prompt_policy_version="test-policy/v1",
        ),
        retry_timing=retry_timing,
    )
