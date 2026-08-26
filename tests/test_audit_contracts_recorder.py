from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pytest
from helpers.audit_boundary_fixtures import audit_payload_boundary_result
from hypothesis import given, strategies as st
from pydantic import ValidationError

from maestro.audit.contracts import (
    MAX_AUDIT_PAYLOAD_BYTES,
    AuditConfidence,
    AuditEventType,
    AuditEventV1,
    AuditEvidenceV1,
    AuditExecutionFailureV1,
    AuditFailureStage,
    AuditResultStatus,
    ExecutionFailedV1,
    ExecutionStartedV1,
    InvestigationCompletedV1,
)
from maestro.audit.recorder import (
    AuditConflictInput,
    AuditEvidenceInput,
    AuditExecutionHandle,
    AuditInvestigationCompletionInput,
    AuditRecorder,
    AuditRuntimeMetadata,
)
from maestro.audit.sanitization import sanitize_audit_text
from maestro.audit.testing import FakeAuditPort, fake_audit_recorder
from maestro.capabilities.resolve_codebase_fact.audit_mapping import (
    map_result_to_audit_completion,
)
from maestro.errors import AuditPersistenceError, ErrorCode
from maestro.model_identity import ModelIdentifier
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

type DurableTextField = Literal[
    "objective",
    "answer",
    "rationale",
    "evidence_symbol",
    "evidence_finding",
    "conflict_description",
    "conflict_evidence_symbol",
    "conflict_evidence_finding",
]

_UNSAFE_MODEL_IDENTIFIERS = (
    "postgresql://reader:fixture-password@db/maestro",  # pragma: allowlist secret
    "/Users/alice/.config/model",
    r"C:\Users\alice\model",
    r"\\server\share\model",
    "gpt-5.4\nAPI_KEY=fixture-secret",
    "gpt-5.4\u200b",
    "API_KEY=fixture-secret",
    "the current production model",
)


def _metadata() -> AuditRuntimeMetadata:
    return AuditRuntimeMetadata(
        server_version="1.0.0",
        runtime_name="codex",
        runtime_version="0.147.0",
        model=ModelIdentifier("gpt-5.4"),
        prompt_policy_version="repository-verifier/v1",
    )


def _repository(root: Path) -> AuthorizedRepository:
    return AuthorizedRepository(root=root, repository_id="a" * 16)


def _fingerprint() -> RepositoryFingerprint:
    return RepositoryFingerprint(
        digest="b" * 64,
        repository_id="a" * 16,
        git_top_level_id=None,
        head=None,
        dirty_digest=None,
        files={},
        truncated=False,
    )


def _sensitive_completion() -> AuditInvestigationCompletionInput:
    return AuditInvestigationCompletionInput(
        status=AuditResultStatus.RESOLVED,
        answer="Found postgresql://reader:fixture-password@db/maestro.",  # pragma: allowlist secret
        confidence=AuditConfidence.HIGH,
        evidence=(
            AuditEvidenceInput(
                path="src/models.py",
                line_start=None,
                line_end=None,
                symbol=r"C:\Users\alice\secrets.txt",
                finding="See (/Users/alice/.aws/credentials).",
            ),
        ),
        conflicts=(
            AuditConflictInput(
                description=r"Compare \\server\share\private\settings.toml.",
                evidence=(
                    AuditEvidenceInput(
                        path="src/models.py",
                        line_start=None,
                        line_end=None,
                        symbol="postgresql://reader:" + "other-password@db/maestro",
                        finding="Compare /opt/company/private/settings.toml.",
                    ),
                ),
            ),
        ),
        rationale="Validated at path:/srv/company/private/config.toml.",
    )


def _completion_with_field(
    field: DurableTextField, value: str
) -> AuditInvestigationCompletionInput:
    primary_evidence = AuditEvidenceInput(
        path="src/models.py",
        line_start=None,
        line_end=None,
        symbol=value if field == "evidence_symbol" else "Order.payment_ids",
        finding=value if field == "evidence_finding" else "Validated primary evidence.",
    )
    conflict_evidence = AuditEvidenceInput(
        path="src/models.py",
        line_start=None,
        line_end=None,
        symbol=(value if field == "conflict_evidence_symbol" else "Payment.order_id"),
        finding=(value if field == "conflict_evidence_finding" else "Validated conflict evidence."),
    )
    return AuditInvestigationCompletionInput(
        status=AuditResultStatus.RESOLVED,
        answer=value if field == "answer" else "Safe answer.",
        confidence=AuditConfidence.HIGH,
        rationale=value if field == "rationale" else "Safe rationale.",
        evidence=(primary_evidence,),
        conflicts=(
            AuditConflictInput(
                description=(
                    value if field == "conflict_description" else "Safe conflict description."
                ),
                evidence=(conflict_evidence,),
            ),
        ),
    )


def _stored_field(
    port: FakeAuditPort,
    field: DurableTextField,
) -> str:
    start_payload = port.starts[0].event.payload
    completion_payload = port.completions[0].event.payload
    assert isinstance(start_payload, ExecutionStartedV1)
    assert isinstance(completion_payload, InvestigationCompletedV1)
    if field == "objective":
        stored = start_payload.objective
    elif field == "answer":
        assert completion_payload.answer is not None
        stored = completion_payload.answer
    elif field == "rationale":
        stored = completion_payload.rationale
    elif field == "evidence_symbol":
        symbol = completion_payload.evidence[0].symbol
        assert symbol is not None
        stored = symbol
    elif field == "evidence_finding":
        stored = completion_payload.evidence[0].finding
    elif field == "conflict_description":
        stored = completion_payload.conflicts[0].description
    elif field == "conflict_evidence_symbol":
        conflict_evidence = completion_payload.conflicts[0].evidence[0]
        assert conflict_evidence.symbol is not None
        stored = conflict_evidence.symbol
    else:
        stored = completion_payload.conflicts[0].evidence[0].finding
    return stored


def _semantic_case(category: str, repository_root: Path) -> tuple[str, tuple[str, ...], str]:
    root = str(repository_root)
    if category == "root_punctuation":
        case = (f"Repository is {root}.", (root,), "Repository is")
    elif category == "root_continuation":
        regex = r"\\d+\s+"
        case = (f"Regex {regex}; file {root}/src/models.py", (root,), regex)
    elif category == "root_embedded_prose":
        case = (
            f"before({root}), symbol Order.payment_ids after",
            (root,),
            "Order.payment_ids",
        )
    elif category == "backslash_unc":
        path = r"\\server.example\share_name-1$\private\settings.toml"
        regex = r"\\w+\d+"
        case = (f"Regex {regex}; host {path}.", (path,), regex)
    elif category == "forward_unc":
        path = "//server-name/share_name/private/settings.toml"
        case = (f"Domain example.com; host {path}.", (path,), "example.com")
    elif category == "credential_uri":
        credential = "fixture-password"
        value = "API /api/v1/items uses postgresql://reader:" + credential + "@db/maestro"
        case = (value, (credential,), "/api/v1/items")
    elif category == "private_host_path":
        path = "/opt/company/private/settings.toml"
        code = (
            r"\\server\share+ \\server\share* \\server\share? "
            r"\\server\share[0] \\server\share{x} \\server\share(x) "
            r"\\server\share^ \\server\share|"
        )
        case = (f"Code {code}; host {path}.", (path,), code)
    elif category == "drive_path":
        path = r"C:\Users\alice\private\settings.toml"
        case = (f"Symbol Order.payment_ids; host {path}.", (path,), "Order.payment_ids")
    elif category == "secret_assignment":
        fixture_value = "fixture-secret-value"
        case = (
            f"Symbol Order.payment_ids; api_key={fixture_value}",
            (fixture_value,),
            "Order.payment_ids",
        )
    elif category == "control":
        case = ("Symbol Order.payment_ids\x07 remains", ("\x07",), "Order.payment_ids")
    else:
        credential = "fixture-password"
        fixture_value = "fixture-secret-value"
        unc = r"\\server.example\share_name-1$\private\settings.toml"
        drive = r"C:\Users\alice\private\settings.toml"
        private = "/opt/company/private/settings.toml"
        value = (
            "API /api/v1/items uses postgresql://token="
            f"{credential}@db/maestro; api_key={fixture_value}; root {root}/src/models.py; "
            f"private {private}; drive {drive}; host ({unc}); unsafe\x07control."
        )
        case = (
            value,
            (credential, fixture_value, root, private, drive, unc, "\x07"),
            "/api/v1/items",
        )
    return case


@pytest.mark.parametrize(
    ("value", "forbidden"),
    [
        (
            "Is postgresql://audit_writer:" + "fixture-password@db/maestro configured?",
            "fixture-password",
        ),
        ("See /home/alice/private/settings.toml", "/home/alice"),
        ("See /private/var/company/settings.toml", "/private/var"),
        ("See /" + "tmp/company/settings.toml", "/" + "tmp/company"),
        ("See /var/lib/company/settings.toml", "/var/lib"),
        ("See /opt/company/private/settings.toml", "/opt/company"),
        ("See /srv/company/private/settings.toml", "/srv/company"),
        ("See /etc/company/private/settings.toml", "/etc/company"),
        ("See /root/.ssh/config", "/root/.ssh"),
        ("See file:///private/company/settings.toml", "/private/company"),
        ("See (/Users/alice/.aws/credentials)", "/Users/alice"),
        (r"See [C:\Users\alice\private\settings.toml]", r"C:\Users\alice"),
        ("See D:/Users/alice/private/settings.toml", "D:/Users/alice"),
        (r"See: \\server\share\private\settings.toml", r"\\server\share"),
        (r"See (\\server\share\private\settings.toml).", r"\\server\share"),
        (r"See [\\server\share\private\settings.toml].", r"\\server\share"),
        (r'See "\\server\share\private\settings.toml".', r"\\server\share"),
        (
            r"See \\server.example\share_name-1$\private\settings.toml",
            r"\\server.example\share_name-1$",
        ),
        ("See //server/share/private/settings.toml", "//server/share"),
        ("See (//server/share/private/settings.toml).", "//server/share"),
        ("See {//server/share/private/settings.toml}.", "//server/share"),
        ("See '//server/share/private/settings.toml'.", "//server/share"),
        (r"Ambiguous \\word\word redacts.", r"\\word\word"),
    ],
)
def test_audit_sanitizer_redacts_sensitive_categories_independent_of_prefix(
    tmp_path: Path,
    value: str,
    forbidden: str,
) -> None:
    sanitized = sanitize_audit_text(value, tmp_path)
    assert forbidden not in sanitized
    assert len(sanitized) <= len(value)


@pytest.mark.parametrize(
    "value",
    [
        "The endpoint is /api/v1/items.",
        r"The matcher is \\d+.",
        r"The matcher is \\d+\s+.",
        r"The matcher is \\w+\d+.",
        r"Code \\name[0] and \\value+(next) stays useful.",
        (
            r"Code \\server\share+ \\server\share* \\server\share? "
            r"\\server\share[0] \\server\share{x} \\server\share(x) "
            r"\\server\share^ \\server\share| stays useful."
        ),
        "Symbol Order.payment_ids remains useful.",
        "Compute total / item_count in example.com/domain.",
        "See https://example.com/api/v1/items.",
        "The relative path src/models.py is evidence.",
    ],
)
def test_audit_sanitizer_preserves_domain_and_code_text(tmp_path: Path, value: str) -> None:
    assert sanitize_audit_text(value, tmp_path) == value


@pytest.mark.parametrize(
    "template",
    [
        "Repository is {root}.",
        "Repository is {root}!",
        "Repository is ({root}).",
        "Read {root}/src/models.py next.",
        "prefix={root}, suffix remains",
        "embedded::{root}::prose",
    ],
)
def test_audit_sanitizer_redacts_exact_repository_identity(tmp_path: Path, template: str) -> None:
    value = template.format(root=tmp_path)
    sanitized = sanitize_audit_text(value, tmp_path)
    assert str(tmp_path) not in sanitized
    assert len(sanitized) <= len(value)


@given(value=st.text(min_size=1, max_size=2_000))
def test_audit_sanitizer_is_nonempty_and_nonexpanding(value: str) -> None:
    sanitized = sanitize_audit_text(value, Path("/private/tmp/maestro/repository"))
    assert sanitized
    assert len(sanitized) <= len(value)


def test_audit_sanitizer_does_not_treat_invalid_anchor_as_a_literal_root() -> None:
    value = "The endpoint is /api/v1/items."
    assert sanitize_audit_text(value, Path(Path.cwd().anchor)) == value


@given(
    order=st.permutations(
        (
            "credential_uri",
            "secret",
            "repository",
            "private_path",
            "drive_path",
            "unc_path",
            "control",
        )
    )
)
def test_audit_sanitizer_detectors_cannot_disable_each_other(order: list[str]) -> None:
    root = Path("/private/tmp/maestro/repository")
    governed = {
        "credential_uri": "postgresql://token=fixture-password@db/maestro",
        "secret": "api_key=fixture-secret-value",  # pragma: allowlist secret
        "repository": f"{root}/src/models.py",
        "private_path": "/opt/company/private/settings.toml",
        "drive_path": r"C:\Users\alice\private\settings.toml",
        "unc_path": r"(\\server.example\share_name-1$\private\settings.toml)",
        "control": "unsafe\x07control",
    }
    value = "; ".join(governed[item] for item in order)

    sanitized = sanitize_audit_text(value, root)

    for forbidden in (
        "fixture-password",
        "fixture-secret-value",
        str(root),
        "/opt/company",
        r"C:\Users\alice",
        r"\\server.example\share_name-1$",
        "\x07",
    ):
        assert forbidden not in sanitized
    assert "postgresql://*@" in sanitized
    assert len(sanitized) <= len(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "objective",
        "answer",
        "rationale",
        "evidence_symbol",
        "evidence_finding",
        "conflict_description",
        "conflict_evidence_symbol",
        "conflict_evidence_finding",
    ],
)
@pytest.mark.parametrize(
    "category",
    [
        "root_punctuation",
        "root_continuation",
        "root_embedded_prose",
        "backslash_unc",
        "forward_unc",
        "credential_uri",
        "private_host_path",
        "drive_path",
        "secret_assignment",
        "control",
        "composed",
    ],
)
async def test_recorder_redacts_governed_data_and_preserves_semantics_in_every_text_field(
    tmp_path: Path,
    field: DurableTextField,
    category: str,
) -> None:
    value, governed, preserved = _semantic_case(category, tmp_path)
    identifiers = iter(UUID(int=item) for item in range(1, 6))
    port = FakeAuditPort()
    recorder = AuditRecorder(
        port,
        _metadata(),
        id_factory=lambda: next(identifiers),
        clock=lambda: _NOW,
    )
    repository = _repository(tmp_path)
    handle = await recorder.start_resolve_codebase_fact(
        repository,
        _fingerprint(),
        value if field == "objective" else "Safe objective.",
    )
    await recorder.record_investigation_completed(
        handle,
        repository,
        _completion_with_field(field, value),
    )

    stored = _stored_field(port, field)
    for forbidden in governed:
        assert forbidden not in stored
    assert preserved in stored
    assert len(stored) <= len(value)


@pytest.mark.asyncio
async def test_recorder_builds_immutable_strict_versioned_records(tmp_path: Path) -> None:
    identifiers = iter(UUID(int=value) for value in range(1, 6))
    port = FakeAuditPort()
    recorder = AuditRecorder(
        port,
        _metadata(),
        id_factory=lambda: next(identifiers),
        clock=lambda: _NOW,
    )
    repository = _repository(tmp_path)

    handle = await recorder.start_resolve_codebase_fact(
        repository,
        _fingerprint(),
        f"Is token=fixture-secret-value-123456 in {tmp_path}/file.py?\x00",
    )
    await recorder.record_investigation_completed(handle, repository, _sensitive_completion())

    assert len(port.starts) == 1
    assert len(port.completions) == 1
    start = port.starts[0]
    completion = port.completions[0]
    assert start.event.sequence == 1
    assert start.event.event_version == 1
    assert start.event.event_type is AuditEventType.EXECUTION_STARTED
    assert completion.event.sequence == 2
    assert completion.event.event_version == 1
    assert completion.event.event_type is AuditEventType.INVESTIGATION_COMPLETED
    assert completion.event.event_id == handle.terminal_event_id
    assert completion.event.audit_id == handle.audit_id
    encoded = start.model_dump_json() + completion.model_dump_json()
    assert "fixture-secret-value" not in encoded
    assert "fixture-password" not in encoded
    assert "other-password" not in encoded
    assert str(tmp_path) not in encoded
    assert "/Users/alice" not in encoded
    assert "/opt/company" not in encoded
    assert "/srv/company" not in encoded
    assert r"C:\\Users\\alice" not in encoded
    assert r"\\\\server\\share" not in encoded
    assert "context" not in encoded
    assert "\u0000" not in encoded
    assert len(completion.event.content_hash()) == 64
    with pytest.raises(ValidationError, match="frozen"):
        start.event.sequence = 2  # type: ignore[misc]


@pytest.mark.asyncio
async def test_recorder_accepts_completion_payload_at_exact_byte_limit(tmp_path: Path) -> None:
    port = FakeAuditPort()
    recorder = fake_audit_recorder(port)
    repository = _repository(tmp_path)
    handle = await recorder.start_resolve_codebase_fact(
        repository,
        _fingerprint(),
        "Is the fact established?",
    )

    await recorder.record_investigation_completed(
        handle,
        repository,
        map_result_to_audit_completion(audit_payload_boundary_result(overflow=False)),
    )

    payload = port.completions[0].event.payload
    assert len(payload.model_dump_json().encode("utf-8")) == MAX_AUDIT_PAYLOAD_BYTES


@pytest.mark.asyncio
async def test_recorder_maps_completion_payload_overflow_before_port_call(tmp_path: Path) -> None:
    port = FakeAuditPort()
    recorder = fake_audit_recorder(port)
    repository = _repository(tmp_path)
    handle = await recorder.start_resolve_codebase_fact(
        repository,
        _fingerprint(),
        "Is the fact established?",
    )

    with pytest.raises(AuditPersistenceError):
        await recorder.record_investigation_completed(
            handle,
            repository,
            map_result_to_audit_completion(audit_payload_boundary_result(overflow=True)),
        )

    assert len(port.starts) == 1
    assert port.completion_attempts == []


@pytest.mark.asyncio
async def test_recorder_maps_start_contract_construction_failure_before_port_call(
    tmp_path: Path,
) -> None:
    port = FakeAuditPort()
    metadata = _metadata()
    recorder = AuditRecorder(
        port,
        AuditRuntimeMetadata(
            server_version=metadata.server_version,
            runtime_name=metadata.runtime_name,
            runtime_version=metadata.runtime_version,
            model=cast(ModelIdentifier, "m" * 129),
            prompt_policy_version=metadata.prompt_policy_version,
        ),
    )

    with pytest.raises(AuditPersistenceError):
        await recorder.start_resolve_codebase_fact(
            _repository(tmp_path),
            _fingerprint(),
            "Is the fact established?",
        )

    assert port.start_attempts == []


@pytest.mark.asyncio
async def test_recorder_does_not_hide_non_contract_programmer_errors(tmp_path: Path) -> None:
    port = FakeAuditPort()

    def broken_clock() -> datetime:
        raise RuntimeError("synthetic programmer error")

    recorder = AuditRecorder(port, _metadata(), clock=broken_clock)

    with pytest.raises(RuntimeError, match="programmer error"):
        await recorder.start_resolve_codebase_fact(
            _repository(tmp_path),
            _fingerprint(),
            "Is the fact established?",
        )

    assert port.start_attempts == []


def test_event_contract_rejects_extra_fields_and_mismatched_sequence() -> None:
    payload = ExecutionStartedV1(
        objective="Determine whether the fact is true.",
        server_version="1.0.0",
        runtime_name="codex",
        runtime_version="0.147.0",
        model=ModelIdentifier("gpt-5.4"),
        prompt_policy_version="repository-verifier/v1",
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ExecutionStartedV1.model_validate(
            {**payload.model_dump(), "unexpected": "rejected"}, strict=True
        )
    with pytest.raises(ValidationError, match="do not agree"):
        AuditEventV1(
            event_id=UUID(int=1),
            audit_id=UUID(int=2),
            sequence=2,
            event_type=AuditEventType.EXECUTION_STARTED,
            occurred_at=_NOW,
            payload=payload,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        AuditEventV1(
            event_id=UUID(int=1),
            audit_id=UUID(int=2),
            sequence=1,
            event_type=AuditEventType.EXECUTION_STARTED,
            occurred_at=datetime(2026, 8, 25),
            payload=payload,
        )


@pytest.mark.parametrize("model", _UNSAFE_MODEL_IDENTIFIERS)
def test_every_audit_event_payload_rejects_unsafe_model_identifiers(model: str) -> None:
    metadata = {
        "server_version": "1.0.0",
        "runtime_name": "codex",
        "runtime_version": "0.147.0",
        "model": model,
        "prompt_policy_version": "repository-verifier/v1",
    }
    payloads = (
        {
            "objective": "Determine whether the fact is true.",
            **metadata,
        },
        {
            "status": "uncertain",
            "answer": None,
            "confidence": "low",
            "rationale": "The repository does not establish the fact.",
            "evidence": (),
            "conflicts": (),
            **metadata,
        },
        {
            "error_code": "INTERNAL_ERROR",
            "failure_stage": "validation",
            **metadata,
        },
    )

    for contract, payload in zip(
        (ExecutionStartedV1, InvestigationCompletedV1, ExecutionFailedV1),
        payloads,
        strict=True,
    ):
        with pytest.raises(ValidationError, match="Audit-safe"):
            contract.model_validate(payload, strict=True)


def test_completed_contract_preserves_all_semantic_statuses() -> None:
    for status in AuditResultStatus:
        payload = InvestigationCompletedV1(
            status=status,
            answer="answer" if status is AuditResultStatus.RESOLVED else None,
            confidence=AuditConfidence.HIGH,
            rationale="Safe rationale.",
            evidence=(
                (AuditEvidenceV1(path="src/models.py", finding="Validated."),)
                if status is AuditResultStatus.RESOLVED
                else ()
            ),
            conflicts=(),
            server_version="1.0.0",
            runtime_name="codex",
            runtime_version="0.147.0",
            model=ModelIdentifier("gpt-5.4"),
            prompt_policy_version="repository-verifier/v1",
        )
        assert payload.status is status


@pytest.mark.asyncio
async def test_recorder_builds_strict_safe_operational_failure(tmp_path: Path) -> None:
    identifiers = iter(UUID(int=value) for value in range(20, 25))
    port = FakeAuditPort()
    recorder = AuditRecorder(
        port,
        _metadata(),
        id_factory=lambda: next(identifiers),
        clock=lambda: _NOW,
    )
    handle = await recorder.start_resolve_codebase_fact(
        _repository(tmp_path),
        _fingerprint(),
        "Is the fact established?",
    )

    await recorder.record_execution_failed(
        handle,
        ErrorCode.AGENT_RUNTIME_ERROR,
        AuditFailureStage.INVESTIGATION,
    )

    assert len(port.failures) == 1
    failure = port.failures[0].event
    assert failure.event_id == handle.terminal_event_id
    assert failure.audit_id == handle.audit_id
    assert failure.sequence == 2
    assert failure.event_type is AuditEventType.EXECUTION_FAILED
    assert isinstance(failure.payload, ExecutionFailedV1)
    assert failure.payload.model_dump(mode="json") == {
        "error_code": "AGENT_RUNTIME_ERROR",
        "failure_stage": "investigation",
        "server_version": "1.0.0",
        "runtime_name": "codex",
        "runtime_version": "0.147.0",
        "model": "gpt-5.4",
        "prompt_policy_version": "repository-verifier/v1",
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ExecutionFailedV1.model_validate(
            {**failure.payload.model_dump(), "exception": "private traceback"},
            strict=True,
        )


def test_failure_contract_rejects_non_terminal_shape() -> None:
    payload = ExecutionFailedV1(
        error_code=ErrorCode.INTERNAL_ERROR,
        failure_stage=AuditFailureStage.VALIDATION,
        server_version="1.0.0",
        runtime_name="codex",
        runtime_version="0.147.0",
        model=ModelIdentifier("gpt-5.4"),
        prompt_policy_version="repository-verifier/v1",
    )

    with pytest.raises(ValidationError, match="do not agree"):
        AuditEventV1(
            event_id=UUID(int=1),
            audit_id=UUID(int=2),
            sequence=1,
            event_type=AuditEventType.EXECUTION_FAILED,
            occurred_at=_NOW,
            payload=payload,
        )


def test_failure_record_rejects_a_semantic_completion_event() -> None:
    completion = InvestigationCompletedV1(
        status=AuditResultStatus.UNCERTAIN,
        answer=None,
        confidence=AuditConfidence.LOW,
        rationale="The repository does not establish the fact.",
        evidence=(),
        conflicts=(),
        server_version="1.0.0",
        runtime_name="codex",
        runtime_version="0.147.0",
        model=ModelIdentifier("gpt-5.4"),
        prompt_policy_version="repository-verifier/v1",
    )
    event = AuditEventV1(
        event_id=UUID(int=1),
        audit_id=UUID(int=2),
        sequence=2,
        event_type=AuditEventType.INVESTIGATION_COMPLETED,
        occurred_at=_NOW,
        payload=completion,
    )

    with pytest.raises(ValidationError, match=r"execution\.failed"):
        AuditExecutionFailureV1(event=event)


@pytest.mark.asyncio
async def test_failure_contract_construction_error_precedes_port_call() -> None:
    metadata = _metadata()
    port = FakeAuditPort()
    recorder = AuditRecorder(
        port,
        AuditRuntimeMetadata(
            server_version=metadata.server_version,
            runtime_name=metadata.runtime_name,
            runtime_version=metadata.runtime_version,
            model=cast(ModelIdentifier, "m" * 129),
            prompt_policy_version=metadata.prompt_policy_version,
        ),
        clock=lambda: _NOW,
    )

    with pytest.raises(AuditPersistenceError):
        await recorder.record_execution_failed(
            AuditExecutionHandle(
                audit_id=UUID(int=1),
                execution_id=UUID(int=2),
                terminal_event_id=UUID(int=3),
            ),
            ErrorCode.INTERNAL_ERROR,
            AuditFailureStage.VALIDATION,
        )

    assert port.failure_attempts == []
