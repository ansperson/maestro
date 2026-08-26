from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from maestro.agents import FakeAgentRuntime
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind
from maestro.audit.recorder import AuditRetryTiming
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
from maestro.errors import AuditPersistenceError, AuditUnavailableError

SettingsFactory = Callable[..., Settings]


def _empty_float_list() -> list[float]:
    return []


@dataclass(slots=True)
class _FakeTime:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=_empty_float_list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    def timing(self) -> AuditRetryTiming:
        return AuditRetryTiming(monotonic_clock=self.monotonic, sleep=self.sleep)


def _result() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="An Order can have many Payments.",
        confidence=Confidence.HIGH,
        evidence=[Evidence(path="src/models.py", line_start=1, finding="The field is a list.")],
        conflicts=[],
        reason="The repository evidence establishes the fact.",
    )


def _request(repository: Path, question: str) -> ResolveCodebaseFactRequest:
    return ResolveCodebaseFactRequest(repository_path=str(repository), question=question)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    ["Can an Order have many Payments?", "Should Order support many Payments?"],
)
async def test_unavailable_start_prevents_worker_and_normative_result(
    repository: Path,
    settings_factory: SettingsFactory,
    caplog: pytest.LogCaptureFixture,
    question: str,
) -> None:
    timing = _FakeTime()

    def unavailable(_record: object) -> None:
        raise AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)

    port = FakeAuditPort(on_start=unavailable)
    runtime = FakeAgentRuntime(lambda _request: _result())
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        runtime,
        fake_audit_recorder(port, retry_timing=timing.timing()),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(AuditUnavailableError):
        await service.execute(_request(repository, question))

    assert runtime.requests == []
    assert len(port.start_attempts) == 3
    assert port.starts == []
    assert port.completion_attempts == []
    assert timing.sleeps == [0.1, 0.25]
    audit_records = [record for record in caplog.records if record.name == "maestro.audit"]
    assert len(audit_records) == 1
    metadata = cast(
        dict[str, object],
        audit_records[0].metadata,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    )
    assert metadata == {
        "error_code": "AUDIT_UNAVAILABLE",
        "audit_operation": "start",
        "attempts": 3,
        "retry_count": 2,
    }
    assert question not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected", "attempts"),
    [
        (AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED, AuditUnavailableError, 3),
        (AuditWriteFailureKind.PERMANENT, AuditPersistenceError, 1),
        (AuditWriteFailureKind.AMBIGUOUS, AuditPersistenceError, 1),
    ],
)
async def test_completion_failure_withholds_result_without_failure_event(
    repository: Path,
    settings_factory: SettingsFactory,
    failure: AuditWriteFailureKind,
    expected: type[Exception],
    attempts: int,
) -> None:
    timing = _FakeTime()

    def fail_completion(_record: object) -> None:
        raise AuditWriteError(failure)

    port = FakeAuditPort(on_completion=fail_completion)
    runtime = FakeAgentRuntime(lambda _request: _result())
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        runtime,
        fake_audit_recorder(port, retry_timing=timing.timing()),
    )

    with pytest.raises(expected):
        await service.execute(_request(repository, "Can an Order have many Payments?"))

    assert len(runtime.requests) == 1
    assert len(port.starts) == 1
    assert len(port.completion_attempts) == attempts
    assert port.completions == []
    assert port.failure_attempts == []


@pytest.mark.asyncio
async def test_retry_reuses_exact_start_record_and_stays_within_total_budget(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    timing = _FakeTime()
    failures_remaining = 2

    def transient_then_success(_record: object) -> None:
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)

    port = FakeAuditPort(on_start=transient_then_success)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(lambda _request: _result()),
        fake_audit_recorder(port, retry_timing=timing.timing()),
    )

    await service.execute(_request(repository, "Can an Order have many Payments?"))

    assert len(port.start_attempts) == 3
    assert all(record is port.start_attempts[0] for record in port.start_attempts)
    assert len({record.event.event_id for record in port.start_attempts}) == 1
    assert len({record.execution.execution_id for record in port.start_attempts}) == 1
    assert timing.sleeps == [0.1, 0.25]
    assert timing.now < 5.0


@pytest.mark.asyncio
async def test_retry_reuses_exact_completion_record_and_event_identity(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    timing = _FakeTime()
    failures_remaining = 2

    def transient_then_success(_record: object) -> None:
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)

    port = FakeAuditPort(on_completion=transient_then_success)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(lambda _request: _result()),
        fake_audit_recorder(port, retry_timing=timing.timing()),
    )

    result = await service.execute(_request(repository, "Can an Order have many Payments?"))

    assert result == _result()
    assert len(port.completion_attempts) == 3
    assert all(record is port.completion_attempts[0] for record in port.completion_attempts)
    assert len({record.event.event_id for record in port.completion_attempts}) == 1
    assert timing.sleeps == [0.1, 0.25]


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_stops_before_another_attempt(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    timing = _FakeTime()

    def consume_budget(_record: object) -> None:
        timing.now += 4.95
        raise AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)

    port = FakeAuditPort(on_start=consume_budget)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(lambda _request: _result()),
        fake_audit_recorder(port, retry_timing=timing.timing()),
    )

    with pytest.raises(AuditUnavailableError):
        await service.execute(_request(repository, "Can an Order have many Payments?"))

    assert len(port.start_attempts) == 1
    assert timing.sleeps == []
    assert timing.now < 5.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("unverifiable timeout"),
        RuntimeError("raw SQLSTATE 99999 host=db.internal user=audit"),
    ],
)
async def test_untyped_or_unverifiable_port_failure_is_safe_and_not_retried(
    repository: Path,
    settings_factory: SettingsFactory,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    def fail(_record: object) -> None:
        raise failure

    port = FakeAuditPort(on_start=fail)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(lambda _request: _result()),
        fake_audit_recorder(port, retry_timing=_FakeTime().timing()),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(AuditPersistenceError) as error:
        await service.execute(_request(repository, "Can an Order have many Payments?"))

    public = error.value.public_json()
    assert "AUDIT_PERSISTENCE_ERROR" in public
    assert "SQLSTATE" not in public + caplog.text
    assert "db.internal" not in public + caplog.text
    assert "user=audit" not in public + caplog.text
    assert len(port.start_attempts) == 1
    audit_records = [record for record in caplog.records if record.name == "maestro.audit"]
    assert len(audit_records) == 1
    metadata = cast(
        dict[str, object],
        audit_records[0].metadata,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    )
    assert metadata == {
        "error_code": "AUDIT_PERSISTENCE_ERROR",
        "audit_operation": "start",
        "attempts": 1,
        "retry_count": 0,
    }
