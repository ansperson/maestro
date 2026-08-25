from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from shutil import which

import pytest
from helpers.audit_boundary_fixtures import audit_payload_boundary_result

import maestro.repository.guard as repository_module
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
    AgentTimeoutError,
    AuditPersistenceError,
    EvidenceValidationError,
    InvalidInputError,
    OutputLimitExceededError,
    RepositoryChangedError,
    RepositoryNotAllowedError,
    ServerBusyError,
)
from maestro.execution import AdmissionController
from maestro.repository.guard import RepositoryGuard

SettingsFactory = Callable[..., Settings]
_FINGERPRINT_PROCESS_FIXTURE = Path(__file__).parent / "helpers" / "fingerprint_process.py"


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
        answer=(
            "Endpoint /api/v1/items uses postgresql://reader:" + "fixture-password@db/maestro."
        ),
        confidence=Confidence.HIGH,
        evidence=[
            Evidence(
                path="src/models.py",
                line_start=1,
                symbol=r"C:\Users\alice\secrets.txt",
                finding=r"Regex \\d+; see (/Users/alice/.aws/credentials).",
            )
        ],
        conflicts=[
            Conflict(
                description=(
                    r"Symbol Order.payment_ids; compare \\server\share\private\settings.toml."
                ),
                evidence=[
                    Evidence(
                        path="migrations/001_payments.sql",
                        symbol="postgresql://reader:" + "other-password@db/maestro",
                        finding="Domain example.com; compare /opt/company/private/settings.toml.",
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
async def test_initial_fingerprint_timeout_cancels_before_audit_or_worker(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(
        allowed_roots=(repository,),
        verifier_timeout_seconds=0.2,
    )
    guard = RepositoryGuard(settings)
    marker = repository.parent / "service-fingerprint-timeout.pid"
    monkeypatch.setattr(
        repository_module,
        "_fingerprint_worker_command",
        lambda: (
            sys.executable,
            "-I",
            str(_FINGERPRINT_PROCESS_FIXTURE),
            "block",
            str(marker),
        ),
    )
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))
    service = ResolveCodebaseFactService(
        settings,
        runtime,
        fake_audit_recorder(port),
        repository_guard=guard,
    )

    with pytest.raises(AgentTimeoutError) as error:
        await service.execute(_request(repository))

    assert "AGENT_TIMEOUT" in error.value.public_json()
    assert await asyncio.to_thread(marker.is_file)
    process_id = int(await asyncio.to_thread(marker.read_text, encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
    assert port.start_attempts == []
    assert port.completion_attempts == []
    assert runtime.requests == []


@pytest.mark.asyncio
async def test_git_canonicalization_timeout_quiesces_before_public_error(
    repository: Path,
    settings_factory: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = which("git")
    if git is None:
        pytest.skip("git is required for repository preparation coverage")
    init_process = await asyncio.create_subprocess_exec(
        git,
        "init",
        "-q",
        cwd=repository,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await init_process.wait() == 0
    settings = settings_factory(
        allowed_roots=(repository,),
        verifier_timeout_seconds=0.4,
    )
    marker = repository.parent / "git-canonical-timeout.pid"
    monkeypatch.setattr(
        repository_module,
        "_canonical_path_worker_command",
        lambda: (
            sys.executable,
            "-I",
            str(_FINGERPRINT_PROCESS_FIXTURE),
            "block",
            str(marker),
        ),
    )
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))
    service = ResolveCodebaseFactService(
        settings,
        runtime,
        fake_audit_recorder(port),
    )

    with pytest.raises(AgentTimeoutError):
        await service.execute(_request(repository))

    assert await asyncio.to_thread(marker.is_file)
    process_id = int(await asyncio.to_thread(marker.read_text, encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
    assert port.start_attempts == []
    assert port.completion_attempts == []
    assert runtime.requests == []


@pytest.mark.asyncio
async def test_filesystem_anchor_request_is_safely_rejected_before_audit(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)), runtime, fake_audit_recorder(port)
    )
    request = _request(repository).model_copy(update={"repository_path": repository.anchor})

    with pytest.raises(RepositoryNotAllowedError) as error:
        await service.execute(request)

    assert error.value.public_json() == (
        '{"code":"REPOSITORY_NOT_ALLOWED",'
        '"message":"The repository is outside the configured allowed roots."}'
    )
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

    with pytest.raises(AuditPersistenceError):
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
async def test_valid_public_result_with_oversized_audit_payload_is_withheld(
    repository: Path,
    settings_factory: SettingsFactory,
) -> None:
    result = audit_payload_boundary_result(overflow=True)
    settings = settings_factory(allowed_roots=(repository,))
    encoded_size = len(result.model_dump_json().encode("utf-8"))
    assert encoded_size <= settings.max_result_bytes
    assert encoded_size <= settings.max_agent_output_bytes
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: result)
    service = ResolveCodebaseFactService(settings, runtime, fake_audit_recorder(port))

    with pytest.raises(AuditPersistenceError) as error:
        await service.execute(_request(repository))

    assert "AUDIT_PERSISTENCE_ERROR" in error.value.public_json()
    assert len(runtime.requests) == 1
    assert len(port.starts) == 1
    assert port.completion_attempts == []


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
    for preserved in (
        "/api/v1/items",
        r"\\\\d+",
        "Order.payment_ids",
        "example.com",
        "postgresql://*@db/maestro",
    ):
        assert preserved in encoded
    assert str(repository) not in encoded


@pytest.mark.asyncio
async def test_maximum_normalization_expansion_fits_audited_execution(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    private_root = "/" + "tmp"
    claim = f"{private_root} " * 798 + "/t"
    question = "confirm " + claim
    objective = neutralize_question(question)
    assert len(question) == 4_000
    assert objective == f"Verify {claim}"
    assert len(objective) < MAX_AUDIT_OBJECTIVE_CHARS
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
    assert len(start_payload.objective) <= MAX_AUDIT_OBJECTIVE_CHARS
    assert private_root not in start_payload.objective
    assert len(port.completions) == 1


@pytest.mark.asyncio
async def test_exact_maximum_objective_fits_audited_execution(
    repository: Path, settings_factory: SettingsFactory
) -> None:
    question = "x" * MAX_AUDIT_OBJECTIVE_CHARS
    port = FakeAuditPort()
    runtime = FakeAgentRuntime(lambda _request: _result(VerificationStatus.RESOLVED))
    service = ResolveCodebaseFactService(
        settings_factory(allowed_roots=(repository,)),
        runtime,
        fake_audit_recorder(port),
    )

    result = await service.execute(_request(repository, question))

    assert result.status is VerificationStatus.RESOLVED
    assert runtime.requests[0].question == question
    start_payload = port.starts[0].event.payload
    assert isinstance(start_payload, ExecutionStartedV1)
    assert start_payload.objective == question
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
