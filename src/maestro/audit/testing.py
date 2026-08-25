"""Typed deterministic Audit adapter for application tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from maestro.audit.contracts import (
    AuditExecutionFailureV1,
    AuditExecutionStartV1,
    AuditInvestigationCompletionV1,
)
from maestro.audit.recorder import AuditRecorder, AuditRetryTiming, AuditRuntimeMetadata

type StartHook = Callable[[AuditExecutionStartV1], Awaitable[None] | None]
type CompletionHook = Callable[[AuditInvestigationCompletionV1], Awaitable[None] | None]
type FailureHook = Callable[[AuditExecutionFailureV1], Awaitable[None] | None]


class FakeAuditPort:
    """Record strict Audit writes without introducing a second persistence backend."""

    def __init__(
        self,
        *,
        on_start: StartHook | None = None,
        on_completion: CompletionHook | None = None,
        on_failure: FailureHook | None = None,
    ) -> None:
        self._on_start = on_start
        self._on_completion = on_completion
        self._on_failure = on_failure
        self.start_attempts: list[AuditExecutionStartV1] = []
        self.completion_attempts: list[AuditInvestigationCompletionV1] = []
        self.failure_attempts: list[AuditExecutionFailureV1] = []
        self.starts: list[AuditExecutionStartV1] = []
        self.completions: list[AuditInvestigationCompletionV1] = []
        self.failures: list[AuditExecutionFailureV1] = []

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        self.start_attempts.append(record)
        if self._on_start is not None:
            outcome = self._on_start(record)
            if isinstance(outcome, Awaitable):
                await outcome
        self.starts.append(record)

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        self.completion_attempts.append(record)
        if self._on_completion is not None:
            outcome = self._on_completion(record)
            if isinstance(outcome, Awaitable):
                await outcome
        self.completions.append(record)

    async def fail_execution(self, record: AuditExecutionFailureV1) -> None:
        self.failure_attempts.append(record)
        if self._on_failure is not None:
            outcome = self._on_failure(record)
            if isinstance(outcome, Awaitable):
                await outcome
        self.failures.append(record)


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
            model="fake-model",
            prompt_policy_version="test-policy/v1",
        ),
        retry_timing=retry_timing,
    )
