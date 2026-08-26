from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import LiteralString, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
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
from maestro.model_identity import ModelIdentifier
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


class _FakePGconn:
    def __init__(
        self,
        *,
        finish_error: Exception | None = None,
        release_on_finish: bool = True,
    ) -> None:
        self._finish_error = finish_error
        self._release_on_finish = release_on_finish
        self.finished = asyncio.Event()
        self.finish_calls = 0

    def finish(self) -> None:
        self.finish_calls += 1
        if self._release_on_finish:
            self.finished.set()
        if self._finish_error is not None:
            raise self._finish_error


@dataclass(frozen=True, slots=True)
class _FakeDurability:
    fsync: object = "on"
    full_page_writes: object = "on"
    synchronous_commit: object = "on"


class _FakeConnection:
    def __init__(
        self,
        *,
        transaction_exit_error: Exception | None = None,
        close_error: Exception | None = None,
        schema_version: int | None = 2,
        durability: _FakeDurability | None = None,
    ) -> None:
        self._transaction_exit_error = transaction_exit_error
        self._close_error = close_error
        self._schema_version = schema_version
        settings = durability or _FakeDurability()
        self._durability: dict[str, object] = {
            "SHOW fsync": settings.fsync,
            "SHOW full_page_writes": settings.full_page_writes,
            "SHOW synchronous_commit": settings.synchronous_commit,
        }
        self.closed = False
        self.statements: list[str] = []
        self.statement_params: list[tuple[object, ...] | None] = []
        self.executions: list[tuple[object, ...]] = []
        self.events: list[tuple[object, ...]] = []
        self.pgconn = _FakePGconn()

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
        self.statements.append(query)
        self.statement_params.append(params)
        fixed_rows = self._fixed_rows(query)
        rows: list[tuple[object, ...]]
        if fixed_rows is not None:
            rows = fixed_rows
        elif "INSERT INTO audit.executions" in query:
            assert params is not None
            candidate = params
            conflicting = [
                row for row in self.executions if row[0] == candidate[0] or row[1] == candidate[1]
            ]
            if conflicting:
                rows = []
            else:
                self.executions.append(candidate)
                rows = [(candidate[0],)]
        elif "INSERT INTO audit.events" in query:
            assert params is not None
            encoded_payload = params[7]
            assert isinstance(encoded_payload, Jsonb)
            candidate = (*params[:7], encoded_payload.obj)
            conflicting = [
                row
                for row in self.events
                if row[0] == candidate[0] or (row[1] == candidate[1] and row[2] == candidate[2])
            ]
            if conflicting:
                rows = []
            else:
                self.events.append(candidate)
                rows = [(candidate[0],)]
        elif "FROM audit.executions" in query:
            assert params is not None
            rows = [row for row in self.executions if row[0] == params[0] or row[1] == params[1]]
        elif "FROM audit.events AS event" in query:
            assert params is not None
            rows = []
            for event in self.events:
                if event[0] != params[0] and not (event[1] == params[1] and event[2] == params[2]):
                    continue
                execution = next(row for row in self.executions if row[0] == event[1])
                rows.append((*event[:2], execution[1], *event[2:]))
        else:
            rows = []
        return _FakeCursor(rows)

    def _fixed_rows(self, query: str) -> list[tuple[object, ...]] | None:
        if "SELECT version FROM audit.schema_version" in query:
            return [(self._schema_version,)] if self._schema_version is not None else []
        if query in self._durability:
            return [(self._durability[query],)]
        return None


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    async def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows.copy()


class _BlockingFailureConnection(_FakeConnection):
    def __init__(self, *, pgconn: _FakePGconn | None = None) -> None:
        super().__init__()
        self.pgconn = pgconn or _FakePGconn()
        self.failure_query_entered = asyncio.Event()

    async def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> _FakeCursor:
        if "INSERT INTO audit.events" not in query:
            return await super().execute(query, params)
        del params
        self.statements.append(query)
        self.failure_query_entered.set()
        await self.pgconn.finished.wait()
        raise errors.OperationalError("synthetic closed connection")


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

    assert len(connection.statements) == 10
    assert sum("SELECT version" in statement for statement in connection.statements) == 3
    assert sum(statement.startswith("SHOW ") for statement in connection.statements) == 3
    assert (
        sum("INSERT INTO audit.executions" in statement for statement in connection.statements) == 1
    )
    assert sum("INSERT INTO audit.events" in statement for statement in connection.statements) == 3
    assert connection.closed is True


@pytest.mark.asyncio
async def test_identical_start_and_terminal_retries_require_exact_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    _patch_connection(monkeypatch, connection)
    start, completion, _repository = await _records()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))

    await port.start_execution(start)
    await port.start_execution(start)
    await port.complete_investigation(completion)
    await port.complete_investigation(completion)

    assert len(connection.executions) == 1
    assert len(connection.events) == 2
    assert sum("FROM audit.executions" in statement for statement in connection.statements) == 1
    assert (
        sum("FROM audit.events AS event" in statement for statement in connection.statements) == 2
    )
    assert all(
        "ON CONFLICT DO NOTHING" in statement
        for statement in connection.statements
        if "INSERT INTO audit." in statement
    )


@pytest.mark.asyncio
async def test_identical_failure_retry_verifies_exact_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    _patch_connection(monkeypatch, connection)
    start, failure = await _failure_records()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))

    await port.start_execution(start)
    await port.fail_execution(failure)
    await port.fail_execution(failure)

    assert len(connection.executions) == 1
    assert len(connection.events) == 2
    assert any("FROM audit.events AS event" in statement for statement in connection.statements)
    assert port._active_failure_writes == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_ambiguous_start_retry_uses_a_fresh_connection_and_exact_prior_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _FakeConnection(
        transaction_exit_error=errors.AdminShutdown("lost commit acknowledgement")
    )
    second = _FakeConnection()
    second.executions = first.executions
    second.events = first.events
    connections = iter((first, second))

    async def connect(_database_url: str) -> _FakeConnection:
        return next(connections)

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    start, _completion, _repository = await _records()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))

    with pytest.raises(AuditWriteError) as ambiguous:
        await port.start_execution(start)
    assert ambiguous.value.kind is AuditWriteFailureKind.AMBIGUOUS
    await port.start_execution(start)

    assert first is not second
    assert first.closed is second.closed is True
    assert second.executions == first.executions
    assert second.events == first.events
    assert any("FROM audit.executions" in statement for statement in second.statements)
    assert any("FROM audit.events AS event" in statement for statement in second.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["execution", "event", "sequence"])
async def test_start_identity_reuse_with_different_content_is_rejected_atomically(
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    connection = _FakeConnection()
    _patch_connection(monkeypatch, connection)
    start, _completion, _repository = await _records()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))
    await port.start_execution(start)
    changed_execution = start.execution.model_copy(
        update={
            "audit_id": UUID(int=20) if collision == "event" else start.execution.audit_id,
            "execution_id": (
                UUID(int=21) if collision == "sequence" else start.execution.execution_id
            ),
            "repository_fingerprint": "c" * 64,
        }
    )
    changed_event = start.event.model_copy(
        update={
            "audit_id": changed_execution.audit_id,
            "event_id": UUID(int=22) if collision == "sequence" else start.event.event_id,
        }
    )
    changed = start.model_copy(update={"execution": changed_execution, "event": changed_event})

    with pytest.raises(AuditWriteError) as failure:
        await port.start_execution(changed)

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert connection.executions == [
        (
            start.execution.audit_id,
            start.execution.execution_id,
            start.execution.capability,
            start.execution.repository_id,
            start.execution.repository_fingerprint,
        )
    ]
    assert len(connection.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["event_id", "execution_id", "timestamp", "payload", "stored_hash", "stored_type"],
)
async def test_terminal_conflict_verifies_every_immutable_field(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    connection = _FakeConnection()
    _patch_connection(monkeypatch, connection)
    start, completion, _repository = await _records()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))
    await port.start_execution(start)
    await port.complete_investigation(completion)
    candidate = completion
    if mismatch == "event_id":
        candidate = completion.model_copy(
            update={"event": completion.event.model_copy(update={"event_id": UUID(int=50)})}
        )
    elif mismatch == "execution_id":
        candidate = completion.model_copy(update={"execution_id": UUID(int=51)})
    elif mismatch == "timestamp":
        candidate = completion.model_copy(
            update={
                "event": completion.event.model_copy(
                    update={"occurred_at": datetime(2026, 8, 25, 0, 0, 1, tzinfo=UTC)}
                )
            }
        )
    elif mismatch == "payload":
        payload = completion.event.payload
        assert hasattr(payload, "rationale")
        candidate = completion.model_copy(
            update={
                "event": completion.event.model_copy(
                    update={
                        "payload": payload.model_copy(
                            update={"rationale": "Different validated rationale."}
                        )
                    }
                )
            }
        )
    elif mismatch == "stored_hash":
        stored = connection.events[1]
        connection.events[1] = (*stored[:6], "0" * 64, stored[7])
    else:
        stored = connection.events[1]
        connection.events[1] = (*stored[:3], "execution.failed", *stored[4:])

    with pytest.raises(AuditWriteError) as failure:
        await port.complete_investigation(candidate)

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert len(connection.events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "durability",
    [
        _FakeDurability(fsync="off"),
        _FakeDurability(full_page_writes="off"),
        _FakeDurability(synchronous_commit="off"),
        _FakeDurability(synchronous_commit="local"),
        _FakeDurability(synchronous_commit="remote_write"),
        _FakeDurability(synchronous_commit="unsupported"),
        _FakeDurability(synchronous_commit=1),
    ],
)
async def test_unsafe_or_unsupported_durability_fails_before_start_write(
    monkeypatch: pytest.MonkeyPatch,
    durability: _FakeDurability,
) -> None:
    connection = _FakeConnection(durability=durability)
    _patch_connection(monkeypatch, connection)
    start, _completion, _repository = await _records()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))

    with pytest.raises(AuditWriteError) as failure:
        await port.start_execution(start)

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert connection.executions == []
    assert connection.events == []
    assert connection.closed is True
    assert all(
        params is None
        for statement, params in zip(
            connection.statements,
            connection.statement_params,
            strict=True,
        )
        if statement.startswith("SHOW ")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous_commit", ["on", "ON", "remote_apply"])
async def test_supported_durability_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
    synchronous_commit: str,
) -> None:
    connection = _FakeConnection(durability=_FakeDurability(synchronous_commit=synchronous_commit))
    _patch_connection(monkeypatch, connection)
    start, _completion, _repository = await _records()

    await PostgresAuditPort(SecretStr("postgresql:///synthetic")).start_execution(start)

    assert len(connection.executions) == 1
    assert connection.closed is True


@pytest.mark.asyncio
async def test_failure_abort_before_connection_marks_and_finishes_late_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_entered = asyncio.Event()
    release_connect = asyncio.Event()
    connection = _FakeConnection()

    async def connect(_database_url: str) -> _FakeConnection:
        connect_entered.set()
        await release_connect.wait()
        return connection

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    failure = await _failure_record()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))
    task = asyncio.create_task(port.fail_execution(failure))
    await connect_entered.wait()

    port.abort_execution_failure(failure.event.event_id)
    release_connect.set()

    with pytest.raises(AuditWriteError) as error:
        await task
    assert error.value.kind is AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED
    assert connection.pgconn.finish_calls == 1
    assert connection.closed is True
    assert port._active_failure_writes == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_failure_abort_then_cancellation_reaps_pending_connection_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_entered = asyncio.Event()
    connect_cleaned = asyncio.Event()

    async def connect(_database_url: str) -> _FakeConnection:
        connect_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            connect_cleaned.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    failure = await _failure_record()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))
    task = asyncio.create_task(port.fail_execution(failure))
    await connect_entered.wait()

    port.abort_execution_failure(failure.event.event_id)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert connect_cleaned.is_set()
    assert port._active_failure_writes == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_failure_abort_finishes_active_query_and_unregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _BlockingFailureConnection()
    _patch_connection(monkeypatch, connection)
    failure = await _failure_record()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))
    task = asyncio.create_task(port.fail_execution(failure))
    await connection.failure_query_entered.wait()

    port.abort_execution_failure(failure.event.event_id)

    with pytest.raises(AuditWriteError):
        await task
    assert connection.pgconn.finish_calls == 1
    assert connection.closed is True
    assert port._active_failure_writes == {}  # pyright: ignore[reportPrivateUsage]
    port.abort_execution_failure(failure.event.event_id)
    assert connection.pgconn.finish_calls == 1


@pytest.mark.asyncio
async def test_failure_abort_isolated_across_concurrent_event_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = _BlockingFailureConnection()
    second_connection = _BlockingFailureConnection()
    connections = iter((first_connection, second_connection))

    async def connect(_database_url: str) -> _BlockingFailureConnection:
        return next(connections)

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    first = await _failure_record(101)
    second = await _failure_record(201)
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))
    first_task = asyncio.create_task(port.fail_execution(first))
    second_task = asyncio.create_task(port.fail_execution(second))
    await first_connection.failure_query_entered.wait()
    await second_connection.failure_query_entered.wait()

    port.abort_execution_failure(first.event.event_id)
    with pytest.raises(AuditWriteError):
        await first_task
    assert second_task.done() is False
    assert second_connection.pgconn.finish_calls == 0

    port.abort_execution_failure(second.event.event_id)
    with pytest.raises(AuditWriteError):
        await second_task
    assert port._active_failure_writes == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_failure_abort_finish_error_still_allows_cancellation_to_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgconn = _FakePGconn(
        finish_error=RuntimeError("private finish diagnostic"),
        release_on_finish=False,
    )
    connection = _BlockingFailureConnection(pgconn=pgconn)
    _patch_connection(monkeypatch, connection)
    failure = await _failure_record()
    port = PostgresAuditPort(SecretStr("postgresql:///synthetic"))
    task = asyncio.create_task(port.fail_execution(failure))
    await connection.failure_query_entered.wait()

    port.abort_execution_failure(failure.event.event_id)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert pgconn.finish_calls == 1
    assert connection.closed is True
    assert port._active_failure_writes == {}  # pyright: ignore[reportPrivateUsage]


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
            model=ModelIdentifier("gpt-5.4"),
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


async def _failure_records(
    identifier_start: int = 101,
) -> tuple[AuditExecutionStartV1, AuditExecutionFailureV1]:
    identifiers = iter(UUID(int=value) for value in range(identifier_start, identifier_start + 5))
    fake = FakeAuditPort()
    recorder = AuditRecorder(
        fake,
        AuditRuntimeMetadata(
            server_version="1.0.0",
            runtime_name="codex",
            runtime_version="0.147.0",
            model=ModelIdentifier("gpt-5.4"),
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
    return fake.starts[0], fake.failures[0]


async def _failure_record(identifier_start: int = 101) -> AuditExecutionFailureV1:
    return (await _failure_records(identifier_start))[1]


async def _assert_native_mismatches_rollback(
    port: PostgresAuditPort,
    start: AuditExecutionStartV1,
    completion: AuditInvestigationCompletionV1,
) -> UUID:
    sequence_collision = completion.model_copy(
        update={"event": completion.event.model_copy(update={"event_id": UUID(int=12)})}
    )
    with pytest.raises(AuditWriteError) as duplicate_mismatch:
        await port.complete_investigation(sequence_collision)
    assert duplicate_mismatch.value.kind is AuditWriteFailureKind.PERMANENT

    content_collision = completion.model_copy(
        update={
            "event": completion.event.model_copy(
                update={"occurred_at": datetime(2026, 8, 25, 0, 0, 1, tzinfo=UTC)}
            )
        }
    )
    with pytest.raises(AuditWriteError) as content_mismatch:
        await port.complete_investigation(content_collision)
    assert content_mismatch.value.kind is AuditWriteFailureKind.PERMANENT

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
        content_hash=rollback_event.content_hash(),
    )
    with pytest.raises(AuditWriteError) as mismatch:
        await port.start_execution(rollback_start)
    assert mismatch.value.kind is AuditWriteFailureKind.PERMANENT
    return new_audit_id


async def _assert_native_unsafe_durability_rejected(
    raw_dsn: str,
    start: AuditExecutionStartV1,
) -> UUID:
    unsafe_execution = start.execution.model_copy(
        update={"audit_id": UUID(int=20), "execution_id": UUID(int=21)}
    )
    unsafe_event = start.event.model_copy(
        update={"audit_id": unsafe_execution.audit_id, "event_id": UUID(int=22)}
    )
    unsafe_start = start.model_copy(update={"execution": unsafe_execution, "event": unsafe_event})
    unsafe_dsn = make_conninfo(
        raw_dsn,
        application_name="maestro-audit-unsafe-durability",
        options="-c synchronous_commit=off",
    )
    with pytest.raises(AuditWriteError) as unsafe_durability:
        await PostgresAuditPort(SecretStr(unsafe_dsn)).start_execution(unsafe_start)
    assert unsafe_durability.value.kind is AuditWriteFailureKind.PERMANENT
    return unsafe_execution.audit_id


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
    await asyncio.gather(port.start_execution(start), port.start_execution(start))
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

    await port.complete_investigation(completion)
    new_audit_id = await _assert_native_mismatches_rollback(port, start, completion)
    unsafe_audit_id = await _assert_native_unsafe_durability_rejected(raw_dsn, start)

    observer = await AsyncConnection.connect(raw_dsn)
    try:
        cursor = await observer.execute(
            "SELECT count(*) FROM audit.executions WHERE audit_id IN (%s, %s)",
            (new_audit_id, unsafe_audit_id),
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
