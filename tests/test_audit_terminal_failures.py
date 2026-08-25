from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

import maestro.capabilities.resolve_codebase_fact.service as service_module
from maestro.agents import FakeAgentRuntime, InvestigationRequest
from maestro.audit.contracts import AuditFailureStage, ExecutionFailedV1
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind
from maestro.audit.testing import FakeAuditPort, fake_audit_recorder
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Evidence,
    ResolveCodebaseFactRequest,
    VerificationResult,
    VerificationStatus,
)
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import Settings
from maestro.errors import (
    AgentRuntimeError,
    AgentTimeoutError,
    AuditPersistenceError,
    ErrorCode,
    EvidenceValidationError,
    OutputLimitExceededError,
    RepositoryChangedError,
)
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint, RepositoryGuard

SettingsFactory = Callable[..., Settings]


@dataclass(frozen=True, slots=True)
class _FailureCase:
    name: str
    expected_error: type[Exception]
    error_code: ErrorCode
    stage: AuditFailureStage


@dataclass(slots=True)
class _CancellationHarness:
    phase: str
    guard: RepositoryGuard
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    owned_cleanup: asyncio.Event = field(default_factory=asyncio.Event)
    fingerprint_calls: int = 0

    async def respond(self, _request: InvestigationRequest) -> VerificationResult:
        if self.phase == "investigation":
            await self._block()
        return _result()

    async def validate_evidence(
        self,
        _repository: AuthorizedRepository,
        _fingerprint: RepositoryFingerprint,
        _evidence: Iterable[Evidence],
    ) -> None:
        if self.phase == "evidence":
            await self._block()

    async def fingerprint(
        self,
        repository_input: AuthorizedRepository,
    ) -> RepositoryFingerprint:
        self.fingerprint_calls += 1
        if self.phase == "result" and self.fingerprint_calls == 2:
            await self._block()
        return await RepositoryGuard.fingerprint(self.guard, repository_input)

    async def complete(self, _record: object) -> None:
        if self.phase == "terminal":
            await self._block()

    async def _block(self) -> None:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.owned_cleanup.set()


def _request(repository: Path) -> ResolveCodebaseFactRequest:
    return ResolveCodebaseFactRequest(
        repository_path=str(repository),
        question="Can an Order have many Payments?",
        context="Raw caller context is not Audit data.",
    )


def _result() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="An Order can have many Payments.",
        confidence=Confidence.HIGH,
        evidence=[Evidence(path="src/models.py", line_start=1, finding="The field is a list.")],
        conflicts=[],
        reason="Validated repository evidence establishes the fact.",
    )


def _assert_failure(
    port: FakeAuditPort,
    error_code: ErrorCode,
    stage: AuditFailureStage,
) -> ExecutionFailedV1:
    assert len(port.starts) == 1
    assert port.completions == []
    assert len(port.failures) == 1
    failure = port.failures[0].event
    assert failure.sequence == 2
    assert failure.audit_id == port.starts[0].execution.audit_id
    assert failure.event_id != port.starts[0].event.event_id
    payload = failure.payload
    assert isinstance(payload, ExecutionFailedV1)
    assert payload.error_code is error_code
    assert payload.failure_stage is stage
    assert set(payload.model_dump()) == {
        "error_code",
        "failure_stage",
        "server_version",
        "runtime_name",
        "runtime_version",
        "model",
        "prompt_policy_version",
    }
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_case",
    [
        _FailureCase(
            "agent",
            AgentRuntimeError,
            ErrorCode.AGENT_RUNTIME_ERROR,
            AuditFailureStage.INVESTIGATION,
        ),
        _FailureCase(
            "evidence",
            EvidenceValidationError,
            ErrorCode.EVIDENCE_VALIDATION_ERROR,
            AuditFailureStage.VALIDATION,
        ),
        _FailureCase(
            "mutation",
            RepositoryChangedError,
            ErrorCode.REPOSITORY_CHANGED_DURING_INVESTIGATION,
            AuditFailureStage.VALIDATION,
        ),
        _FailureCase(
            "output",
            OutputLimitExceededError,
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            AuditFailureStage.VALIDATION,
        ),
        _FailureCase(
            "timeout",
            AgentTimeoutError,
            ErrorCode.AGENT_TIMEOUT,
            AuditFailureStage.INVESTIGATION,
        ),
    ],
)
async def test_post_start_operational_failures_are_safe_terminal_events(
    repository: Path,
    settings_factory: SettingsFactory,
    failure_case: _FailureCase,
) -> None:
    case = failure_case.name
    result = _result()
    overrides: dict[str, object] = {"allowed_roots": (repository,)}

    async def respond(_request: InvestigationRequest) -> VerificationResult:
        if case == "agent":
            raise AgentRuntimeError(
                "private traceback postgresql://writer:fixture-password@db/internal"
            )
        if case == "mutation":
            (repository / "mutation-after-start.txt").write_text("changed", encoding="utf-8")
        if case == "timeout":
            await asyncio.Event().wait()
        return result

    if case == "evidence":
        result = result.model_copy(
            update={"evidence": [Evidence(path="missing.py", finding="not validated")]}
        )
    elif case == "output":
        result = result.model_copy(
            update={
                "evidence": [
                    Evidence(path="src/models.py", finding="one"),
                    Evidence(path="migrations/001_payments.sql", finding="two"),
                ]
            }
        )
        overrides["max_evidence_items"] = 1
    elif case == "timeout":
        overrides["verifier_timeout_seconds"] = 0.3

    port = FakeAuditPort()
    service = ResolveCodebaseFactService(
        settings_factory(**overrides),
        FakeAgentRuntime(respond),
        fake_audit_recorder(port),
    )

    try:
        with pytest.raises(failure_case.expected_error):
            await service.execute(_request(repository))
    finally:
        mutation = repository / "mutation-after-start.txt"
        if mutation.exists():
            mutation.unlink()

    payload = _assert_failure(port, failure_case.error_code, failure_case.stage)
    encoded = payload.model_dump_json()
    assert "traceback" not in encoded
    assert "fixture-password" not in encoded
    assert "db/internal" not in encoded
    assert str(repository) not in encoded


@pytest.mark.asyncio
async def test_dual_operation_and_audit_failure_returns_audit_error(
    repository: Path,
    settings_factory: SettingsFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_terminal(_record: object) -> None:
        raise AuditWriteError(AuditWriteFailureKind.PERMANENT)

    async def fail_operation(_request: InvestigationRequest) -> VerificationResult:
        raise AgentRuntimeError("private SQLSTATE 99999 host=db.internal")

    port = FakeAuditPort(on_failure=fail_terminal)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(fail_operation),
        fake_audit_recorder(port),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(AuditPersistenceError) as error:
        await service.execute(_request(repository))

    assert error.value.code is ErrorCode.AUDIT_PERSISTENCE_ERROR
    assert len(port.failure_attempts) == 1
    assert port.failures == []
    assert "SQLSTATE" not in caplog.text
    assert "db.internal" not in caplog.text
    capability_record = next(
        record for record in caplog.records if record.name == "maestro.resolve_codebase_fact"
    )
    metadata = cast(dict[str, object], capability_record.__dict__["metadata"])
    assert metadata["request_id"] == port.starts[0].execution.execution_id.hex
    assert metadata["error_code"] == "AGENT_RUNTIME_ERROR"
    assert metadata["failure_stage"] == "investigation"


@pytest.mark.asyncio
async def test_untyped_failure_recorder_error_is_mapped_to_audit_persistence(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_operation(_request: InvestigationRequest) -> VerificationResult:
        raise AgentRuntimeError

    async def broken_failure_recorder(*_arguments: object) -> None:
        raise RuntimeError("private adapter diagnostic")

    port = FakeAuditPort()
    recorder = fake_audit_recorder(port)
    monkeypatch.setattr(recorder, "record_execution_failed", broken_failure_recorder)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(fail_operation),
        recorder,
    )

    with pytest.raises(AuditPersistenceError):
        await service.execute(_request(repository))

    assert len(port.starts) == 1
    assert port.failures == []


@pytest.mark.asyncio
async def test_failure_retry_reuses_one_terminal_identity(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    failures_remaining = 2

    def retry(_record: object) -> None:
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)

    async def fail_operation(_request: InvestigationRequest) -> VerificationResult:
        raise AgentRuntimeError

    port = FakeAuditPort(on_failure=retry)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(fail_operation),
        fake_audit_recorder(port),
    )

    with pytest.raises(AgentRuntimeError):
        await service.execute(_request(repository))

    assert len(port.failure_attempts) == 3
    assert all(record is port.failure_attempts[0] for record in port.failure_attempts)
    assert {record.event.event_id for record in port.failure_attempts} == {
        port.failures[0].event.event_id
    }


@pytest.mark.asyncio
async def test_unexpected_exception_records_internal_error_then_reraises(
    repository: Path,
    settings_factory: SettingsFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail(_request: InvestigationRequest) -> VerificationResult:
        raise RuntimeError("private traceback /Users/alice/.ssh/id_ed25519")

    port = FakeAuditPort()
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(fail),
        fake_audit_recorder(port),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="private traceback"):
        await service.execute(_request(repository))

    _assert_failure(port, ErrorCode.INTERNAL_ERROR, AuditFailureStage.INVESTIGATION)
    assert "id_ed25519" not in caplog.text
    metadata = cast(dict[str, object], caplog.records[-1].__dict__["metadata"])
    assert metadata["request_id"] == port.starts[0].execution.execution_id.hex
    assert metadata["error_code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_cancellation_during_start_has_no_manufactured_terminal(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def block_start(_record: object) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    port = FakeAuditPort(on_start=block_start)
    runtime = FakeAgentRuntime(lambda _request: _result())
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        runtime,
        fake_audit_recorder(port),
    )
    task = asyncio.create_task(service.execute(_request(repository)))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleaned.is_set()
    assert len(port.start_attempts) == 1
    assert port.starts == []
    assert port.failures == []
    assert runtime.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["investigation", "evidence", "result", "terminal"])
async def test_post_start_cancellation_is_propagated_after_joined_failure_record(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    guard = RepositoryGuard(settings_factory(allowed_roots=(repository,)))
    harness = _CancellationHarness(phase, guard)
    monkeypatch.setattr(guard, "validate_evidence", harness.validate_evidence)
    monkeypatch.setattr(guard, "fingerprint", harness.fingerprint)
    port = FakeAuditPort(on_completion=harness.complete)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(harness.respond),
        fake_audit_recorder(port),
        repository_guard=guard,
    )
    task = asyncio.create_task(service.execute(_request(repository)))
    await harness.entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.owned_cleanup.is_set()
    payload = _assert_failure(
        port,
        ErrorCode.AGENT_CANCELLED,
        (
            AuditFailureStage.INVESTIGATION
            if phase == "investigation"
            else AuditFailureStage.TERMINAL_PERSISTENCE
            if phase == "terminal"
            else AuditFailureStage.VALIDATION
        ),
    )
    assert payload.error_code is ErrorCode.AGENT_CANCELLED


@pytest.mark.asyncio
async def test_cancellation_cleanup_has_hard_budget_and_no_orphan_task(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_entered = asyncio.Event()
    worker_cleaned = asyncio.Event()
    audit_entered = asyncio.Event()
    audit_cleaned = asyncio.Event()

    async def block_worker(_request: InvestigationRequest) -> VerificationResult:
        worker_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_cleaned.set()
        return _result()

    async def block_audit(_record: object) -> None:
        audit_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            audit_cleaned.set()

    monkeypatch.setattr(service_module, "_CANCELLATION_AUDIT_BUDGET_SECONDS", 0.05)
    port = FakeAuditPort(on_failure=block_audit)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(block_worker),
        fake_audit_recorder(port),
    )
    task = asyncio.create_task(service.execute(_request(repository)))
    await worker_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker_cleaned.is_set()
    assert audit_entered.is_set()
    assert audit_cleaned.is_set()
    assert port.failures == []
    assert all(
        not task.get_name().startswith("audit-cancellation-") for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_orphan_audit_cleanup(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    worker_entered = asyncio.Event()
    audit_entered = asyncio.Event()
    audit_cleaned = asyncio.Event()

    async def block_worker(_request: InvestigationRequest) -> VerificationResult:
        worker_entered.set()
        await asyncio.Event().wait()
        return _result()

    async def block_audit(_record: object) -> None:
        audit_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            audit_cleaned.set()

    port = FakeAuditPort(on_failure=block_audit)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(block_worker),
        fake_audit_recorder(port),
    )
    task = asyncio.create_task(service.execute(_request(repository)))
    await worker_entered.wait()
    task.cancel()
    await audit_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert audit_cleaned.is_set()
    assert all(
        not task.get_name().startswith("audit-cancellation-") for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_audit_cleanup_failure_never_replaces_cancellation(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    entered = asyncio.Event()

    async def block_worker(_request: InvestigationRequest) -> VerificationResult:
        entered.set()
        await asyncio.Event().wait()
        return _result()

    def fail_audit(_record: object) -> None:
        raise AuditWriteError(AuditWriteFailureKind.PERMANENT)

    port = FakeAuditPort(on_failure=fail_audit)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(block_worker),
        fake_audit_recorder(port),
    )
    task = asyncio.create_task(service.execute(_request(repository)))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(port.failure_attempts) == 1
    assert port.failures == []


@pytest.mark.asyncio
async def test_start_only_trail_remains_explicitly_incomplete(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    port = FakeAuditPort()
    recorder = fake_audit_recorder(port)
    guard_settings = settings_factory(allowed_roots=(repository,))
    guard = RepositoryGuard(guard_settings)
    authorized = guard.authorize(str(repository))
    fingerprint = await guard.fingerprint(authorized)

    await recorder.start_resolve_codebase_fact(
        authorized,
        fingerprint,
        "Is the fact established?",
    )

    assert len(port.starts) == 1
    assert port.completions == []
    assert port.failures == []
