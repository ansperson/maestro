from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from maestro.audit.contracts import (
    AuditConfidence,
    AuditEventType,
    AuditEventV1,
    AuditEvidenceV1,
    AuditResultStatus,
    ExecutionStartedV1,
    InvestigationCompletedV1,
)
from maestro.audit.recorder import AuditRecorder, AuditRuntimeMetadata
from maestro.audit.sanitization import sanitize_audit_text
from maestro.audit.testing import FakeAuditPort
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Conflict,
    Evidence,
    VerificationResult,
    VerificationStatus,
)
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _metadata() -> AuditRuntimeMetadata:
    return AuditRuntimeMetadata(
        server_version="1.0.0",
        runtime_name="codex",
        runtime_version="0.147.0",
        model="gpt-5.4",
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


def _sensitive_result() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="Found postgresql://reader:fixture-password@db/maestro.",  # pragma: allowlist secret
        confidence=Confidence.HIGH,
        evidence=[
            Evidence(
                path="src/models.py",
                symbol=r"C:\Users\alice\secrets.txt",
                finding="See (/Users/alice/.aws/credentials).",
            )
        ],
        conflicts=[
            Conflict(
                description=r"Compare \\server\share\private\settings.toml.",
                evidence=[
                    Evidence(
                        path="src/models.py",
                        symbol="postgresql://reader:" + "other-password@db/maestro",
                        finding="Compare /opt/company/private/settings.toml.",
                    )
                ],
            )
        ],
        reason="Validated at path:/srv/company/private/config.toml.",
    )


@pytest.mark.parametrize(
    ("value", "forbidden"),
    [
        (
            "Is postgresql://audit_writer:" + "fixture-password@db/maestro configured?",
            "fixture-password",
        ),
        ("See /opt/company/private/settings.toml", "/opt/company"),
        ("See (/Users/alice/.aws/credentials)", "/Users/alice"),
        (r"See [C:\Users\alice\private\settings.toml]", r"C:\Users\alice"),
        (r"See {\\server\share\private\settings.toml}", r"\\server\share"),
    ],
)
def test_audit_sanitizer_redacts_sensitive_categories_independent_of_prefix(
    tmp_path: Path,
    value: str,
    forbidden: str,
) -> None:
    sanitized = sanitize_audit_text(value, tmp_path)
    assert forbidden not in sanitized
    assert "[REDACTED]" in sanitized or "<absolute-path>" in sanitized


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
    await recorder.record_investigation_completed(handle, repository, _sensitive_result())

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
    assert completion.event.event_id == handle.completion_event_id
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


def test_event_contract_rejects_extra_fields_and_mismatched_sequence() -> None:
    payload = ExecutionStartedV1(
        objective="Determine whether the fact is true.",
        server_version="1.0.0",
        runtime_name="codex",
        runtime_version="0.147.0",
        model="gpt-5.4",
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
            model="gpt-5.4",
            prompt_policy_version="repository-verifier/v1",
        )
        assert payload.status is status
