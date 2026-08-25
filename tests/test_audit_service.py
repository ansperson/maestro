from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from maestro.agents import FakeAgentRuntime, InvestigationRequest
from maestro.audit.contracts import (
    MAX_AUDIT_OBJECTIVE_CHARS,
    ExecutionStartedV1,
    InvestigationCompletedV1,
)
from maestro.audit.testing import FakeAuditPort, fake_audit_recorder
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Conflict,
    Evidence,
    ResolveCodebaseFactRequest,
    VerificationResult,
    VerificationStatus,
)
from maestro.capabilities.resolve_codebase_fact.policy import neutralize_question
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import Settings
from maestro.errors import (
    EvidenceValidationError,
    InvalidInputError,
    OutputLimitExceededError,
    RepositoryChangedError,
    RepositoryNotAllowedError,
    ServerBusyError,
)
from maestro.execution import AdmissionController

SettingsFactory = Callable[..., Settings]


def _request(
    repository: Path,
    question: str = "Can an Order have many Payments?",
) -> ResolveCodebaseFactRequest:
    return ResolveCodebaseFactRequest(
        repository_path=str(repository),
        question=question,
        context="Raw caller context must not be audited.",
    )


def _result(status: VerificationStatus) -> VerificationResult:
    resolved = status is VerificationStatus.RESOLVED
    return VerificationResult(
        status=status,
        answer="An Order can have many Payments." if resolved else None,
        confidence=Confidence.HIGH if resolved else Confidence.LOW,
        evidence=(
            [Evidence(path="src/models.py", line_start=1, finding="The field is a list.")]
            if resolved
            else []
        ),
        conflicts=[],
        reason="The accepted repository evidence determines this status.",
    )


def _sensitive_result() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="Found postgresql://reader:fixture-password@db/maestro.",  # pragma: allowlist secret
        confidence=Confidence.HIGH,
        evidence=[
            Evidence(
                path="src/models.py",
                line_start=1,
                symbol=r"C:\Users\alice\secrets.txt",
                finding="See (/Users/alice/.aws/credentials).",
            )
        ],
        conflicts=[
            Conflict(
                description=r"Compare \\server\share\private\settings.toml.",
                evidence=[
                    Evidence(
                        path="migrations/001_payments.sql",
                        symbol="postgresql://reader:" + "other-password@db/maestro",
                        finding="Compare /opt/company/private/settings.toml.",
                    )
                ],
            )
        ],
        reason="Validated at path:/srv/company/private/config.toml.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [VerificationStatus.RESOLVED, VerificationStatus.UNCERTAIN])
async def test_audit_start_precedes_worker_and_completion_precedes_return(
    repository: Path,
    settings_factory: SettingsFactory,
    status: VerificationStatus,
) -> None:
    operations: list[str] = []
    port = FakeAuditPort(
        on_start=lambda _record: operations.append("start"),
        on_completion=lambda _record: operations.append("completion"),
    )

    def respond(_request: InvestigationRequest) -> VerificationResult:
        assert operations == ["start"]
        operations.append("worker")
        return _result(status)

    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(respond),
        fake_audit_recorder(port),
    )
    result = await service.execute(_request(repository))

    assert result.status is status
    assert operations == ["start", "worker", "completion"]
    assert len(port.starts) == 1
    assert len(port.completions) == 1
    payload = port.completions[0].event.payload
    assert isinstance(payload, InvestigationCompletedV1)
    assert payload.status.value == status.value


@pytest.mark.asyncio
async def test_normative_result_is_audited_without_calling_worker(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)), runtime, fake_audit_recorder(port)
    )

    result = await service.execute(_request(repository, "Should Order support many Payments?"))

    assert result.status is VerificationStatus.HUMAN_DECISION_REQUIRED
    assert runtime.requests == []
    assert len(port.starts) == 1
    assert len(port.completions) == 1
    payload = port.completions[0].event.payload
    assert isinstance(payload, InvestigationCompletedV1)
    assert payload.status.value == VerificationStatus.HUMAN_DECISION_REQUIRED.value


@pytest.mark.asyncio
async def test_preflight_rejections_and_waiting_cancellation_create_no_audit(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))

    length_service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,), max_question_chars=5),
        runtime,
        fake_audit_recorder(port),
    )
    with pytest.raises(InvalidInputError):
        await length_service.execute(_request(repository))

    unauthorized_service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository / "src",)),
        runtime,
        fake_audit_recorder(port),
    )
    with pytest.raises(RepositoryNotAllowedError):
        await unauthorized_service.execute(_request(repository))

    admission = AdmissionController(max_concurrency=1, max_queue_size=0)
    rejected_service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        runtime,
        fake_audit_recorder(port),
        admission=admission,
    )
    async with admission.slot():
        with pytest.raises(ServerBusyError):
            await rejected_service.execute(_request(repository))

    waiting_admission = AdmissionController(max_concurrency=1, max_queue_size=1)
    waiting_service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        runtime,
        fake_audit_recorder(port),
        admission=waiting_admission,
    )
    async with waiting_admission.slot():
        task = asyncio.create_task(waiting_service.execute(_request(repository)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert port.starts == []
    assert port.completions == []
    assert runtime.requests == []


@pytest.mark.asyncio
async def test_start_failure_prevents_worker_invocation(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    async def fail_start(_record: object) -> None:
        raise RuntimeError("synthetic start failure")

    port = FakeAuditPort(on_start=fail_start)
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)), runtime, fake_audit_recorder(port)
    )

    with pytest.raises(RuntimeError, match="start failure"):
        await service.execute(_request(repository))
    assert runtime.requests == []
    assert port.starts == []
    assert port.completions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["evidence", "size"])
async def test_invalid_or_oversized_result_is_not_persisted_as_completion(
    repository: Path,
    settings_factory: SettingsFactory,
    failure: str,
) -> None:
    result = _result(VerificationStatus.RESOLVED)
    settings_overrides: dict[str, object] = {"allowed_roots": (repository,)}
    expected_error: type[Exception] = EvidenceValidationError
    if failure == "evidence":
        result = result.model_copy(
            update={"evidence": [Evidence(path="missing.py", finding="not validated")]}
        )
    else:
        result = result.model_copy(update={"answer": "x" * 2_000})
        settings_overrides["max_result_bytes"] = 1_024
        expected_error = OutputLimitExceededError
    port = FakeAuditPort()
    service = ResolveCodebaseFactService(
        settings_factory(**settings_overrides),
        FakeAgentRuntime(lambda _request: result),
        fake_audit_recorder(port),
    )

    with pytest.raises(expected_error):
        await service.execute(_request(repository))
    assert len(port.starts) == 1
    assert port.completions == []


@pytest.mark.asyncio
async def test_repository_stability_and_sanitization_precede_completion(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    port = FakeAuditPort()

    async def mutate(_request: InvestigationRequest) -> VerificationResult:
        (repository / "changed-after-start.txt").write_text("changed", encoding="utf-8")
        return _result(VerificationStatus.RESOLVED)

    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(mutate),
        fake_audit_recorder(port),
    )
    with pytest.raises(RepositoryChangedError):
        await service.execute(_request(repository))
    assert len(port.starts) == 1
    assert port.completions == []

    (repository / "changed-after-start.txt").unlink()
    clean_port = FakeAuditPort()
    clean_service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(lambda _request: _sensitive_result()),
        fake_audit_recorder(clean_port),
    )
    await clean_service.execute(
        _request(
            repository,
            "Is postgresql://audit_writer:" + "objective-password@db/maestro configured?",
        )
    )
    encoded = clean_port.starts[0].model_dump_json() + clean_port.completions[0].model_dump_json()
    for forbidden in (
        "objective-password",
        "fixture-password",
        "other-password",
        "/Users/alice",
        "/opt/company",
        "/srv/company",
        r"C:\\Users\\alice",
        r"\\\\server\\share",
    ):
        assert forbidden not in encoded
    assert str(repository) not in encoded


@pytest.mark.asyncio
async def test_maximum_normalization_expansion_fits_audited_execution(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    question = "confirm " + "x" * (4_000 - len("confirm "))
    objective = neutralize_question(question)
    assert len(question) == 4_000
    assert len(objective) == MAX_AUDIT_OBJECTIVE_CHARS
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        runtime,
        fake_audit_recorder(port),
    )

    result = await service.execute(_request(repository, question))

    assert result.status is VerificationStatus.RESOLVED
    assert runtime.requests[0].question == objective
    assert len(port.starts) == 1
    start_payload = port.starts[0].event.payload
    assert isinstance(start_payload, ExecutionStartedV1)
    assert start_payload.objective == objective
    assert len(port.completions) == 1


@pytest.mark.asyncio
async def test_completion_write_must_finish_before_result_is_returned(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_completion(_record: object) -> None:
        entered.set()
        await release.wait()

    port = FakeAuditPort(on_completion=block_completion)
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED)),
        fake_audit_recorder(port),
    )
    task = asyncio.create_task(service.execute(_request(repository)))
    await entered.wait()
    assert not task.done()
    release.set()
    result = await task
    assert result.status is VerificationStatus.RESOLVED
    assert len(port.completions) == 1
