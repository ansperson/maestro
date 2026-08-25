from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, strategies as st
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
from maestro.audit.recorder import (
    AuditConflictInput,
    AuditEvidenceInput,
    AuditInvestigationCompletionInput,
    AuditRecorder,
    AuditRuntimeMetadata,
)
from maestro.audit.sanitization import sanitize_audit_text
from maestro.audit.testing import FakeAuditPort
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
        (r"See {\\server\share\private\settings.toml}", r"\\server\share"),
        ("See //server/share/private/settings.toml", "//server/share"),
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
        "Symbol Order.payment_ids remains useful.",
        "Compute total / item_count in example.com/domain.",
        "See https://example.com/api/v1/items.",
        "The relative path src/models.py is evidence.",
    ],
)
def test_audit_sanitizer_preserves_domain_and_code_text(tmp_path: Path, value: str) -> None:
    assert sanitize_audit_text(value, tmp_path) == value


@given(value=st.text(min_size=1, max_size=2_000))
def test_audit_sanitizer_is_nonempty_and_nonexpanding(value: str) -> None:
    sanitized = sanitize_audit_text(value, Path("/private/tmp/maestro/repository"))
    assert sanitized
    assert len(sanitized) <= len(value)


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
