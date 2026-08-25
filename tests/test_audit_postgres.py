from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import LiteralString, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

import maestro.audit.postgres.adapter as adapter_module
from maestro.audit.contracts import (
    AuditConfidence,
    AuditEventType,
    AuditEventV1,
    AuditExecutionFailureV1,
    AuditExecutionStartV1,
    AuditExecutionV1,
    AuditFailureStage,
    AuditInvestigationCompletionV1,
    AuditResultStatus,
)
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind
from maestro.audit.postgres import PostgresAuditPort
from maestro.audit.postgres.migrations import packaged_migrations
from maestro.audit.recorder import (
    AuditInvestigationCompletionInput,
    AuditRecorder,
    AuditRuntimeMetadata,
)
from maestro.audit.testing import FakeAuditPort
from maestro.errors import ErrorCode
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

_TEST_DSN_ENV = "MAESTRO_TEST_POSTGRES_DSN"


class _FakeTransaction:
    def __init__(self, exit_error: Exception | None = None) -> None:
        self._exit_error = exit_error

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception_type, traceback
        if exception is None and self._exit_error is not None:
            raise self._exit_error
        return False


class _FakeConnection:
    def __init__(
        self,
        *,
        transaction_exit_error: Exception | None = None,
        close_error: Exception | None = None,
        schema_version: int | None = 2,
    ) -> None:
        self._transaction_exit_error = transaction_exit_error
        self._close_error = close_error
        self._schema_version = schema_version
        self.closed = False
        self.statements: list[str] = []

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._transaction_exit_error)

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error

    async def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> _FakeCursor:
        del params
        self.statements.append(query)
        return _FakeCursor(self._schema_version)


class _FakeCursor:
    def __init__(self, schema_version: int | None) -> None:
        self._schema_version = schema_version

    async def fetchone(self) -> tuple[int] | None:
        return (self._schema_version,) if self._schema_version is not None else None


def _patch_connection(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeConnection | Exception,
) -> None:
    async def connect(_database_url: str) -> _FakeConnection:
        if isinstance(connection, Exception):
            raise connection
        return connection

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))


def test_migration_is_an_ordered_packaged_explicit_resource() -> None:
    migrations = packaged_migrations()
    assert [(migration.version, migration.name) for migration in migrations] == [
        (1, "0001_audit_tracer.sql"),
        (2, "0002_execution_failed.sql"),
    ]
    assert migrations[0].sql.startswith("BEGIN;")
    assert migrations[0].sql.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE audit.executions" in migrations[0].sql
    assert "CREATE TABLE audit.events" in migrations[0].sql
    assert migrations[1].sql.startswith("BEGIN;")
    assert migrations[1].sql.rstrip().endswith("COMMIT;")
    assert "execution.failed" in migrations[1].sql
    assert "UPDATE audit.events" not in migrations[1].sql
    assert "DELETE FROM audit.events" not in migrations[1].sql


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            errors.OperationalError("connection unavailable"),
            AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED,
        ),
        (
            errors.ConnectionFailure("connection lost"),
            AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED,
        ),
        (errors.AdminShutdown("shutdown"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (errors.TooManyConnections("exhausted"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (errors.InvalidPassword("private credential detail"), AuditWriteFailureKind.PERMANENT),
        (ValueError("private malformed DSN detail"), AuditWriteFailureKind.PERMANENT),
    ],
)
def test_postgres_connection_failure_classification_is_safe(
    error: Exception, expected: AuditWriteFailureKind
) -> None:
    classified = adapter_module._classify_connection_failure(error)  # pyright: ignore[reportPrivateUsage]
    assert classified.kind is expected
    assert "private" not in str(classified)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("known pre-commit timeout"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (
            errors.SerializationFailure("serialization"),
            AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED,
        ),
        (errors.DeadlockDetected("deadlock"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (errors.AdminShutdown("shutdown"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (errors.TooManyConnections("exhausted"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (errors.InvalidPassword("authentication"), AuditWriteFailureKind.PERMANENT),
        (errors.InsufficientPrivilege("permission"), AuditWriteFailureKind.PERMANENT),
        (errors.UndefinedTable("schema"), AuditWriteFailureKind.PERMANENT),
        (errors.UniqueViolation("identity mismatch"), AuditWriteFailureKind.PERMANENT),
    ],
)
def test_postgres_precommit_failure_classification(
    error: Exception, expected: AuditWriteFailureKind
) -> None:
    classified = adapter_module._classify_precommit_failure(error)  # pyright: ignore[reportPrivateUsage]
    assert classified.kind is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            errors.SerializationFailure("serialization"),
            AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED,
        ),
        (errors.DeadlockDetected("deadlock"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (errors.AdminShutdown("ambiguous shutdown"), AuditWriteFailureKind.AMBIGUOUS),
        (errors.ConnectionFailure("ambiguous connection"), AuditWriteFailureKind.AMBIGUOUS),
        (TimeoutError("ambiguous timeout"), AuditWriteFailureKind.AMBIGUOUS),
        (errors.UniqueViolation("constraint"), AuditWriteFailureKind.AMBIGUOUS),
    ],
)
def test_postgres_commit_failure_is_conservative(
    error: Exception, expected: AuditWriteFailureKind
) -> None:
    classified = adapter_module._classify_commit_failure(error)  # pyright: ignore[reportPrivateUsage]
    assert classified.kind is expected


@pytest.mark.asyncio
async def test_postgres_port_runs_all_short_transaction_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    _patch_connection(monkeypatch, connection)
    start, completion, _repository = await _records()
    failure = await _failure_record()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))

    await port.start_execution(start)
    await port.complete_investigation(completion)
    await port.fail_execution(failure)

    assert len(connection.statements) == 7
    assert sum("SELECT version" in statement for statement in connection.statements) == 3
    assert (
        sum("INSERT INTO audit.executions" in statement for statement in connection.statements) == 1
    )
    assert sum("INSERT INTO audit.events" in statement for statement in connection.statements) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", [None, 1, 999])
async def test_postgres_port_rejects_unsupported_schema_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int | None,
) -> None:
    connection = _FakeConnection(schema_version=schema_version)
    _patch_connection(monkeypatch, connection)
    start, _completion, _repository = await _records()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))

    with pytest.raises(AuditWriteError) as failure:
        await port.start_execution(start)

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert len(connection.statements) == 1
    assert "SELECT version" in connection.statements[0]


@pytest.mark.asyncio
async def test_transaction_runner_closes_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeConnection()
    _patch_connection(monkeypatch, connection)
    calls = 0

    async def operation(_connection: AsyncConnection[tuple[object, ...]]) -> None:
        nonlocal calls
        calls += 1

    await adapter_module._run_transaction("postgresql:///synthetic", operation)  # pyright: ignore[reportPrivateUsage]

    assert calls == 1
    assert connection.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (errors.OperationalError("unavailable"), AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED),
        (errors.InvalidPassword("private"), AuditWriteFailureKind.PERMANENT),
    ],
)
async def test_transaction_runner_classifies_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: AuditWriteFailureKind,
) -> None:
    _patch_connection(monkeypatch, error)

    with pytest.raises(AuditWriteError) as failure:
        await adapter_module._run_transaction(  # pyright: ignore[reportPrivateUsage]
            "postgresql:///synthetic",
            _successful_operation,
        )

    assert failure.value.kind is expected


@pytest.mark.asyncio
async def test_transaction_runner_classifies_precommit_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(close_error=RuntimeError("close detail"))
    _patch_connection(monkeypatch, connection)

    async def fail_before_commit(_connection: AsyncConnection[tuple[object, ...]]) -> None:
        raise errors.UndefinedTable("schema detail")

    with pytest.raises(AuditWriteError) as failure:
        await adapter_module._run_transaction(  # pyright: ignore[reportPrivateUsage]
            "postgresql:///synthetic",
            fail_before_commit,
        )

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert connection.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            errors.SerializationFailure("serialization"),
            AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED,
        ),
        (errors.AdminShutdown("ambiguous"), AuditWriteFailureKind.AMBIGUOUS),
    ],
)
async def test_transaction_runner_classifies_commit_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: AuditWriteFailureKind,
) -> None:
    connection = _FakeConnection(transaction_exit_error=error)
    _patch_connection(monkeypatch, connection)

    with pytest.raises(AuditWriteError) as failure:
        await adapter_module._run_transaction(  # pyright: ignore[reportPrivateUsage]
            "postgresql:///synthetic",
            _successful_operation,
        )

    assert failure.value.kind is expected
    assert connection.closed is True


@pytest.mark.asyncio
async def test_transaction_runner_treats_close_failure_as_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(close_error=RuntimeError("private close detail"))
    _patch_connection(monkeypatch, connection)

    with pytest.raises(AuditWriteError) as failure:
        await adapter_module._run_transaction(  # pyright: ignore[reportPrivateUsage]
            "postgresql:///synthetic",
            _successful_operation,
        )

    assert failure.value.kind is AuditWriteFailureKind.AMBIGUOUS


async def _successful_operation(_connection: AsyncConnection[tuple[object, ...]]) -> None:
    return None


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
        AuditInvestigationCompletionInput(
            status=AuditResultStatus.UNCERTAIN,
            answer=None,
            confidence=AuditConfidence.LOW,
            evidence=(),
            conflicts=(),
            rationale="The repository does not establish this fact.",
        ),
    )
    return fake.starts[0], fake.completions[0], repository


async def _failure_record() -> AuditExecutionFailureV1:
    identifiers = iter(UUID(int=value) for value in range(101, 106))
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
    repository = AuthorizedRepository(root=Path.cwd(), repository_id="e" * 16)
    fingerprint = RepositoryFingerprint(
        digest="f" * 64,
        repository_id=repository.repository_id,
        git_top_level_id=None,
        head=None,
        dirty_digest=None,
        files={},
        truncated=False,
    )
    handle = await recorder.start_resolve_codebase_fact(
        repository,
        fingerprint,
        "Can the operation complete?",
    )
    await recorder.record_execution_failed(
        handle,
        ErrorCode.AGENT_RUNTIME_ERROR,
        AuditFailureStage.INVESTIGATION,
    )
    return fake.failures[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_postgres_atomic_start_terminal_uniqueness_and_connection_lifetime() -> None:
    raw_dsn = os.environ.get(_TEST_DSN_ENV)
    if raw_dsn is None:
        pytest.skip(f"set {_TEST_DSN_ENV} to run PostgreSQL Audit adapter tests")
    migrations = packaged_migrations()
    migration_connection = await AsyncConnection.connect(raw_dsn)
    try:
        for migration in migrations:
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

    with pytest.raises(AuditWriteError) as duplicate:
        await port.complete_investigation(completion)
    assert duplicate.value.kind is AuditWriteFailureKind.PERMANENT

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
    with pytest.raises(AuditWriteError) as mismatch:
        await port.start_execution(rollback_start)
    assert mismatch.value.kind is AuditWriteFailureKind.PERMANENT

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
