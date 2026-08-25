from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import LiteralString, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from maestro.audit.contracts import (
    AuditEventType,
    AuditEventV1,
    AuditExecutionStartV1,
    AuditExecutionV1,
    AuditInvestigationCompletionV1,
)
from maestro.audit.postgres import PostgresAuditPort
from maestro.audit.postgres.migrations import packaged_migrations
from maestro.audit.recorder import AuditRecorder, AuditRuntimeMetadata
from maestro.audit.testing import FakeAuditPort
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    VerificationResult,
    VerificationStatus,
)
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

_TEST_DSN_ENV = "MAESTRO_TEST_POSTGRES_DSN"


def test_migration_is_an_ordered_packaged_explicit_resource() -> None:
    migrations = packaged_migrations()
    assert [(migration.version, migration.name) for migration in migrations] == [
        (1, "0001_audit_tracer.sql")
    ]
    assert migrations[0].sql.startswith("BEGIN;")
    assert migrations[0].sql.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE audit.executions" in migrations[0].sql
    assert "CREATE TABLE audit.events" in migrations[0].sql


async def _records() -> tuple[
    AuditExecutionStartV1,
    AuditInvestigationCompletionV1,
    AuthorizedRepository,
]:
    identifiers = iter(UUID(int=value) for value in range(1, 6))
    fake = FakeAuditPort()
    recorder = AuditRecorder(
        fake,
        AuditRuntimeMetadata(
            server_version="1.0.0",
            runtime_name="codex",
            runtime_version="0.147.0",
            model="gpt-5.4",
            prompt_policy_version="repository-verifier/v1",
        ),
        id_factory=lambda: next(identifiers),
        clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
    )
    repository = AuthorizedRepository(root=Path.cwd(), repository_id="a" * 16)
    fingerprint = RepositoryFingerprint(
        digest="b" * 64,
        repository_id=repository.repository_id,
        git_top_level_id=None,
        head=None,
        dirty_digest=None,
        files={},
        truncated=False,
    )
    handle = await recorder.start_resolve_codebase_fact(repository, fingerprint, "Is it true?")
    await recorder.record_investigation_completed(
        handle,
        repository,
        VerificationResult(
            status=VerificationStatus.UNCERTAIN,
            answer=None,
            confidence=Confidence.LOW,
            evidence=[],
            conflicts=[],
            reason="The repository does not establish this fact.",
        ),
    )
    return fake.starts[0], fake.completions[0], repository


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_postgres_atomic_start_terminal_uniqueness_and_connection_lifetime() -> None:
    raw_dsn = os.environ.get(_TEST_DSN_ENV)
    if raw_dsn is None:
        pytest.skip(f"set {_TEST_DSN_ENV} to run PostgreSQL Audit adapter tests")
    migration = packaged_migrations()[0]
    migration_connection = await AsyncConnection.connect(raw_dsn)
    try:
        reviewed_sql = cast(LiteralString, migration.sql)
        await migration_connection.execute(sql.SQL(reviewed_sql))
        await migration_connection.commit()
    finally:
        await migration_connection.close()

    application_dsn = make_conninfo(raw_dsn, application_name="maestro-audit-issue7")
    port = PostgresAuditPort(SecretStr(application_dsn))
    start, completion, _repository = await _records()
    await port.start_execution(start)
    await port.complete_investigation(completion)

    observer = await AsyncConnection.connect(raw_dsn)
    try:
        cursor = await observer.execute(
            """
            SELECT
                (SELECT count(*) FROM audit.executions),
                (SELECT count(*) FROM audit.events),
                (SELECT count(*) FROM pg_stat_activity WHERE application_name = %s)
            """,
            ("maestro-audit-issue7",),
        )
        assert await cursor.fetchone() == (1, 2, 0)
    finally:
        await observer.close()

    with pytest.raises(errors.UniqueViolation):
        await port.complete_investigation(completion)

    new_audit_id = UUID(int=10)
    rollback_event = AuditEventV1(
        event_id=start.event.event_id,
        audit_id=new_audit_id,
        sequence=1,
        event_type=AuditEventType.EXECUTION_STARTED,
        occurred_at=start.event.occurred_at,
        payload=start.event.payload,
    )
    rollback_start = AuditExecutionStartV1(
        execution=AuditExecutionV1(
            audit_id=new_audit_id,
            execution_id=UUID(int=11),
            repository_id="c" * 16,
            repository_fingerprint="d" * 64,
        ),
        event=rollback_event,
    )
    with pytest.raises(errors.UniqueViolation):
        await port.start_execution(rollback_start)

    observer = await AsyncConnection.connect(raw_dsn)
    try:
        cursor = await observer.execute(
            "SELECT count(*) FROM audit.executions WHERE audit_id = %s",
            (new_audit_id,),
        )
        assert await cursor.fetchone() == (0,)
        cursor = await observer.execute(
            "SELECT sequence, event_type FROM audit.events ORDER BY sequence"
        )
        assert await cursor.fetchall() == [
            (1, "execution.started"),
            (2, "investigation.completed"),
        ]
    finally:
        await observer.close()
