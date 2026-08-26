from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import LiteralString, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from pydantic import SecretStr

import maestro.audit.postgres.adapter as adapter_module
from maestro.agents import FakeAgentRuntime
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
from maestro.audit.postgres.migrations import packaged_migrations, packaged_role_bootstrap
from maestro.audit.recorder import (
    AuditConflictInput,
    AuditEvidenceInput,
    AuditInvestigationCompletionInput,
    AuditRecorder,
    AuditRuntimeMetadata,
)
from maestro.audit.testing import FakeAuditPort
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Evidence,
    ResolveCodebaseFactRequest,
    VerificationResult,
    VerificationStatus,
)
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import AuditWriterConfiguration, Settings
from maestro.errors import ErrorCode
from maestro.model_identity import ModelIdentifier
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

_TEST_DSN_ENV = "MAESTRO_TEST_POSTGRES_DSN"
_MIGRATOR_ROLE = "maestro_audit_migrator"
_WRITER_ROLE = "maestro_audit_writer"
_READER_ROLE = "maestro_audit_reader"
_OUTSIDER_ROLE = "maestro_audit_outsider_test"


def _writer_configuration() -> AuditWriterConfiguration:
    return AuditWriterConfiguration(
        host="127.0.0.1",
        port=5432,
        database="maestro",
        user="audit_writer",
        password=SecretStr("synthetic-password"),
    )


def _writer_configuration_from_dsn(raw_dsn: str) -> AuditWriterConfiguration:
    values = conninfo_to_dict(raw_dsn)
    host = values.get("host")
    port = values.get("port")
    database = values.get("dbname") or values.get("database")
    user = values.get("user")
    password = values.get("password")
    return AuditWriterConfiguration(
        host=host if isinstance(host, str) else "localhost",
        port=int(port) if isinstance(port, str | int) else 5432,
        database=database if isinstance(database, str) else "maestro",
        user=user if isinstance(user, str) else "audit_writer",
        password=SecretStr(password if isinstance(password, str) else "integration-placeholder"),
    )


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
        schema_version: int | None = 3,
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
        rowcount = -1
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
                rowcount = 0
            else:
                self.executions.append(candidate)
                rows = []
                rowcount = 1
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
                rowcount = 0
            else:
                self.events.append(candidate)
                rows = []
                rowcount = 1
        elif "audit.verify_execution_v1" in query:
            assert params is not None
            conflicting = [
                row for row in self.executions if row[0] == params[0] or row[1] == params[1]
            ]
            rows = [(len(conflicting) == 1 and conflicting[0] == params,)]
        elif "audit.verify_event_v1" in query:
            assert params is not None
            encoded_payload = params[8]
            assert isinstance(encoded_payload, Jsonb)
            conflicting = [
                row
                for row in self.events
                if row[0] == params[0] or (row[1] == params[1] and row[2] == params[3])
            ]
            expected = (
                params[0],
                params[1],
                params[3],
                params[4],
                params[5],
                params[6],
                params[7],
                encoded_payload.obj,
            )
            execution_ids = [row[1] for row in self.executions if row[0] == params[1]]
            rows = [
                (
                    len(conflicting) == 1
                    and conflicting[0] == expected
                    and execution_ids == [params[2]],
                )
            ]
        else:
            rows = []
        return _FakeCursor(rows, rowcount=rowcount)

    def _fixed_rows(self, query: str) -> list[tuple[object, ...]] | None:
        if "SELECT version FROM audit.schema_version" in query:
            return [(self._schema_version,)] if self._schema_version is not None else []
        if query in self._durability:
            return [(self._durability[query],)]
        return None


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]], *, rowcount: int = -1) -> None:
        self._rows = rows
        self.rowcount = rowcount

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
    async def connect(**_connection_values: object) -> _FakeConnection:
        if isinstance(connection, Exception):
            raise connection
        return connection

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))


def test_migration_is_an_ordered_packaged_explicit_resource() -> None:
    bootstrap = packaged_role_bootstrap()
    migrations = packaged_migrations()
    assert bootstrap.startswith("BEGIN;")
    assert bootstrap.rstrip().endswith("COMMIT;")
    assert "LOGIN PASSWORD NULL" in bootstrap
    assert "GRANT CONNECT, CREATE" in bootstrap
    assert "GRANT CONNECT ON DATABASE" in bootstrap
    assert "PASSWORD '" not in bootstrap
    assert [(migration.version, migration.name) for migration in migrations] == [
        (1, "0001_audit_tracer.sql"),
        (2, "0002_execution_failed.sql"),
        (3, "0003_roles_and_read_views.sql"),
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
    assert "CREATE VIEW audit_read.execution_summary" in migrations[2].sql
    assert "CREATE VIEW audit_read.event_timeline" in migrations[2].sql
    assert "CREATE VIEW audit_read.evidence" in migrations[2].sql
    assert "SECURITY DEFINER" in migrations[2].sql
    assert "SET search_path = pg_catalog" in migrations[2].sql
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA audit_read" in migrations[2].sql
    assert "UPDATE audit.events" not in migrations[2].sql
    assert "DELETE FROM audit.events" not in migrations[2].sql


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


@pytest.mark.asyncio
async def test_adapter_passes_only_typed_fields_and_ephemeral_password_to_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    captured: dict[str, object] = {}

    async def connect(**connection_values: object) -> _FakeConnection:
        captured.update(connection_values)
        return connection

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    start, _completion, _repository = await _records()
    await PostgresAuditPort(_writer_configuration()).start_execution(start)

    assert captured == {
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": "maestro",
        "user": "audit_writer",
        "password": "synthetic-password",  # pragma: allowlist secret
        "application_name": "maestro-audit-writer",
    }
    assert "conninfo" not in captured
    assert "password_file" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variable", "private_value"),
    [
        ("PGSERVICE", "private-service"),
        ("PGOPTIONS", "-c search_path=private_schema"),
        ("PGPASSFILE", "/private/passfile"),
        ("PGSSLMODE", "disable"),
        ("PGSERVICEFILE", "/private/service-file"),
        ("PGSYSCONFDIR", "/private/system-config"),
        ("pgservice", "case-variant-private-service"),
    ],
)
async def test_adapter_rejects_libpq_environment_added_after_projection_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    variable: str,
    private_value: str,
) -> None:
    configuration = _writer_configuration()
    connect_called = False

    async def connect(**_connection_values: object) -> _FakeConnection:
        nonlocal connect_called
        connect_called = True
        return _FakeConnection()

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    monkeypatch.setenv(variable, private_value)
    start, _completion, _repository = await _records()

    with pytest.raises(AuditWriteError) as failure:
        await PostgresAuditPort(configuration).start_execution(start)

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert connect_called is False
    assert variable not in str(failure.value)
    assert private_value not in str(failure.value)
    assert private_value not in caplog.text


@pytest.mark.asyncio
async def test_adapter_rechecks_libpq_environment_before_second_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _writer_configuration()
    connection = _FakeConnection()
    connect_calls = 0

    async def connect(**_connection_values: object) -> _FakeConnection:
        nonlocal connect_calls
        connect_calls += 1
        return connection

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    start, completion, _repository = await _records()
    port = PostgresAuditPort(configuration)
    await port.start_execution(start)
    monkeypatch.setenv("PGOPTIONS", "-c search_path=private_schema")

    with pytest.raises(AuditWriteError) as failure:
        await port.complete_investigation(completion)

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert connect_calls == 1


@pytest.mark.asyncio
async def test_driver_failure_does_not_log_or_expose_connection_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_detail = (
        "postgresql://audit_writer:" + "synthetic-password@private-db/maestro SELECT private_table"
    )

    async def connect(**_connection_values: object) -> _FakeConnection:
        raise errors.InvalidPassword(private_detail)

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    start, _completion, _repository = await _records()
    with pytest.raises(AuditWriteError) as failure:
        await PostgresAuditPort(_writer_configuration()).start_execution(start)

    assert failure.value.kind is AuditWriteFailureKind.PERMANENT
    assert private_detail not in str(failure.value)
    assert private_detail not in caplog.text


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
    port = PostgresAuditPort(_writer_configuration())

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
    port = PostgresAuditPort(_writer_configuration())

    await port.start_execution(start)
    await port.start_execution(start)
    await port.complete_investigation(completion)
    await port.complete_investigation(completion)

    assert len(connection.executions) == 1
    assert len(connection.events) == 2
    assert sum("audit.verify_execution_v1" in statement for statement in connection.statements) == 1
    assert sum("audit.verify_event_v1" in statement for statement in connection.statements) == 2
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
    port = PostgresAuditPort(_writer_configuration())

    await port.start_execution(start)
    await port.fail_execution(failure)
    await port.fail_execution(failure)

    assert len(connection.executions) == 1
    assert len(connection.events) == 2
    assert any("audit.verify_event_v1" in statement for statement in connection.statements)
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

    async def connect(**_connection_values: object) -> _FakeConnection:
        return next(connections)

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    start, _completion, _repository = await _records()
    port = PostgresAuditPort(_writer_configuration())

    with pytest.raises(AuditWriteError) as ambiguous:
        await port.start_execution(start)
    assert ambiguous.value.kind is AuditWriteFailureKind.AMBIGUOUS
    await port.start_execution(start)

    assert first is not second
    assert first.closed is second.closed is True
    assert second.executions == first.executions
    assert second.events == first.events
    assert any("audit.verify_execution_v1" in statement for statement in second.statements)
    assert any("audit.verify_event_v1" in statement for statement in second.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["execution", "event", "sequence"])
async def test_start_identity_reuse_with_different_content_is_rejected_atomically(
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    connection = _FakeConnection()
    _patch_connection(monkeypatch, connection)
    start, _completion, _repository = await _records()
    port = PostgresAuditPort(_writer_configuration())
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
    port = PostgresAuditPort(_writer_configuration())
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
    port = PostgresAuditPort(_writer_configuration())

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

    await PostgresAuditPort(_writer_configuration()).start_execution(start)

    assert len(connection.executions) == 1
    assert connection.closed is True


@pytest.mark.asyncio
async def test_failure_abort_before_connection_marks_and_finishes_late_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_entered = asyncio.Event()
    release_connect = asyncio.Event()
    connection = _FakeConnection()

    async def connect(**_connection_values: object) -> _FakeConnection:
        connect_entered.set()
        await release_connect.wait()
        return connection

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    failure = await _failure_record()
    port = PostgresAuditPort(_writer_configuration())
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

    async def connect(**_connection_values: object) -> _FakeConnection:
        connect_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            connect_cleaned.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    failure = await _failure_record()
    port = PostgresAuditPort(_writer_configuration())
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
    port = PostgresAuditPort(_writer_configuration())
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

    async def connect(**_connection_values: object) -> _BlockingFailureConnection:
        return next(connections)

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))
    first = await _failure_record(101)
    second = await _failure_record(201)
    port = PostgresAuditPort(_writer_configuration())
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
    port = PostgresAuditPort(_writer_configuration())
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
    port = PostgresAuditPort(_writer_configuration())

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

    await adapter_module._run_transaction(  # pyright: ignore[reportPrivateUsage]
        _writer_configuration(), operation
    )

    assert calls == 1
    assert connection.closed is True


@pytest.mark.asyncio
async def test_start_connection_is_closed_before_agent_runtime_begins(
    repository: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_connection = _FakeConnection()
    terminal_connection = _FakeConnection()
    connections = iter((start_connection, terminal_connection))

    async def connect(**_connection_values: object) -> _FakeConnection:
        return next(connections)

    monkeypatch.setattr(adapter_module.AsyncConnection, "connect", staticmethod(connect))

    def investigate(_request: object) -> VerificationResult:
        assert start_connection.closed is True
        return VerificationResult(
            status=VerificationStatus.RESOLVED,
            answer="The model stores a list of payments.",
            confidence=Confidence.HIGH,
            evidence=[
                Evidence(
                    path="src/models.py",
                    line_start=1,
                    finding="The field is a list.",
                )
            ],
            conflicts=[],
            reason="The repository evidence establishes the representation.",
        )

    settings = settings_factory(allowed_roots=(repository,))
    recorder = AuditRecorder(
        PostgresAuditPort(settings.audit_writer_configuration()),
        AuditRuntimeMetadata(
            server_version="1.0.0",
            runtime_name="codex",
            runtime_version="0.147.0",
            model=ModelIdentifier("gpt-5.4"),
            prompt_policy_version="repository-verifier/v1",
        ),
    )
    service = ResolveCodebaseFactService(settings, FakeAgentRuntime(investigate), recorder)
    try:
        result = await service.execute(
            ResolveCodebaseFactRequest(
                repository_path=str(repository),
                question="Does Order store a list of payments?",
            )
        )
    finally:
        await service.shutdown()

    assert result.status is VerificationStatus.RESOLVED
    assert terminal_connection.closed is True


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
            _writer_configuration(),
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
            _writer_configuration(),
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
            _writer_configuration(),
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
            _writer_configuration(),
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


async def _completion_records(
    identifier_start: int,
    *,
    repository_id: str,
    objective: str,
    result: AuditInvestigationCompletionInput,
) -> tuple[AuditExecutionStartV1, AuditInvestigationCompletionV1]:
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
    repository = AuthorizedRepository(root=Path.cwd(), repository_id=repository_id)
    fingerprint = RepositoryFingerprint(
        digest=f"{identifier_start:064x}",
        repository_id=repository.repository_id,
        git_top_level_id=None,
        head=None,
        dirty_digest=None,
        files={},
        truncated=False,
    )
    handle = await recorder.start_resolve_codebase_fact(repository, fingerprint, objective)
    await recorder.record_investigation_completed(
        handle,
        repository,
        result,
    )
    return fake.starts[0], fake.completions[0]


async def _incomplete_record(identifier_start: int) -> AuditExecutionStartV1:
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
    repository = AuthorizedRepository(root=Path.cwd(), repository_id="a" * 16)
    fingerprint = RepositoryFingerprint(
        digest=f"{identifier_start:064x}",
        repository_id=repository.repository_id,
        git_top_level_id=None,
        head=None,
        dirty_digest=None,
        files={},
        truncated=False,
    )
    await recorder.start_resolve_codebase_fact(
        repository,
        fingerprint,
        "Will this execution reach a terminal event?",
    )
    return fake.starts[0]


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
    del start
    unsafe_audit_id = UUID(int=20)
    unsafe_dsn = make_conninfo(
        raw_dsn,
        application_name="maestro-audit-unsafe-durability",
        options="-c synchronous_commit=off",
    )
    unsafe_connection = await AsyncConnection.connect(unsafe_dsn)
    try:
        with pytest.raises(AuditWriteError) as unsafe_durability:
            await adapter_module._verify_database_durability(  # pyright: ignore[reportPrivateUsage]
                unsafe_connection
            )
        assert unsafe_durability.value.kind is AuditWriteFailureKind.PERMANENT
    finally:
        await unsafe_connection.close()
    return unsafe_audit_id


def _role_dsn(raw_dsn: str, role: str, password: str, application_name: str) -> str:
    return make_conninfo(
        raw_dsn,
        user=role,
        password=password,
        application_name=application_name,
    )


async def _execute_resource(
    connection: AsyncConnection[tuple[object, ...]],
    resource: str,
) -> None:
    reviewed_sql = cast(LiteralString, resource)
    await connection.execute(sql.SQL(reviewed_sql))


async def _set_role_password(
    connection: AsyncConnection[tuple[object, ...]],
    role: str,
    password: str,
) -> None:
    statement = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
        sql.Identifier(role),
        sql.Literal(password),
    )
    await connection.execute(statement)


async def _assert_denied(
    dsn: str,
    statement: LiteralString,
    *,
    autocommit: bool = False,
) -> None:
    connection = await AsyncConnection.connect(dsn, autocommit=autocommit)
    try:
        with pytest.raises(errors.InsufficientPrivilege):
            await connection.execute(statement)
    finally:
        await connection.close()


async def _bootstrap_v2_database(raw_dsn: str) -> tuple[str, str, str]:
    migrations = packaged_migrations()
    admin = await AsyncConnection.connect(raw_dsn)
    try:
        for migration in migrations[:2]:
            await _execute_resource(admin, migration.sql)
        await _execute_resource(admin, packaged_role_bootstrap())

        cursor = await admin.execute(
            """
            SELECT
                rolname,
                rolcanlogin,
                rolsuper,
                rolcreatedb,
                rolcreaterole,
                rolinherit,
                rolreplication,
                rolbypassrls,
                rolpassword IS NULL
            FROM pg_catalog.pg_authid
            WHERE rolname IN (%s, %s, %s)
            ORDER BY rolname
            """,
            (_MIGRATOR_ROLE, _WRITER_ROLE, _READER_ROLE),
        )
        expected_role_rows = sorted(
            (
                role,
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                True,
            )
            for role in (_MIGRATOR_ROLE, _WRITER_ROLE, _READER_ROLE)
        )
        assert await cursor.fetchall() == expected_role_rows

        cursor = await admin.execute(
            """
            SELECT
                rolname,
                pg_catalog.has_database_privilege(rolname, current_database(), 'CONNECT'),
                pg_catalog.has_database_privilege(rolname, current_database(), 'CREATE'),
                pg_catalog.has_database_privilege(rolname, current_database(), 'TEMP')
            FROM pg_catalog.pg_roles
            WHERE rolname IN (%s, %s, %s)
            ORDER BY rolname
            """,
            (_MIGRATOR_ROLE, _WRITER_ROLE, _READER_ROLE),
        )
        assert await cursor.fetchall() == sorted(
            [
                (_MIGRATOR_ROLE, True, True, False),
                (_WRITER_ROLE, True, False, False),
                (_READER_ROLE, True, False, False),
            ]
        )
        cursor = await admin.execute(
            """
            SELECT
                pg_catalog.has_database_privilege('public', current_database(), 'CONNECT'),
                pg_catalog.has_database_privilege('public', current_database(), 'CREATE'),
                pg_catalog.has_database_privilege('public', current_database(), 'TEMP'),
                pg_catalog.has_schema_privilege('public', 'public', 'USAGE'),
                pg_catalog.has_schema_privilege('public', 'public', 'CREATE')
            """
        )
        assert await cursor.fetchone() == (False, False, False, False, False)
        cursor = await admin.execute(
            """
            SELECT count(*)
            FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.roleid IN (
                SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN (%s, %s, %s)
            ) OR membership.member IN (
                SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN (%s, %s, %s)
            )
            """,
            (
                _MIGRATOR_ROLE,
                _WRITER_ROLE,
                _READER_ROLE,
                _MIGRATOR_ROLE,
                _WRITER_ROLE,
                _READER_ROLE,
            ),
        )
        assert await cursor.fetchone() == (0,)

        migrator_password = secrets.token_urlsafe(24)
        writer_password = secrets.token_urlsafe(24)
        reader_password = secrets.token_urlsafe(24)
        outsider_password = secrets.token_urlsafe(24)
        await _set_role_password(admin, _MIGRATOR_ROLE, migrator_password)
        await _set_role_password(admin, _WRITER_ROLE, writer_password)
        await _set_role_password(admin, _READER_ROLE, reader_password)
        create_outsider = sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        ).format(sql.Identifier(_OUTSIDER_ROLE), sql.Literal(outsider_password))
        await admin.execute(create_outsider)
        await admin.commit()

        cursor = await admin.execute(
            """
            SELECT count(*)
            FROM pg_catalog.pg_authid
            WHERE rolname IN (%s, %s, %s) AND rolpassword IS NOT NULL
            """,
            (_MIGRATOR_ROLE, _WRITER_ROLE, _READER_ROLE),
        )
        assert await cursor.fetchone() == (3,)
    finally:
        await admin.close()

    outsider_dsn = _role_dsn(raw_dsn, _OUTSIDER_ROLE, outsider_password, "audit-outsider")
    with pytest.raises(errors.OperationalError):
        await AsyncConnection.connect(outsider_dsn)
    return migrator_password, writer_password, reader_password


async def _apply_roles_and_views(raw_dsn: str, migrator_password: str) -> str:
    migrator_dsn = _role_dsn(
        raw_dsn,
        _MIGRATOR_ROLE,
        migrator_password,
        "maestro-audit-migrator",
    )
    connection = await AsyncConnection.connect(migrator_dsn)
    try:
        await _execute_resource(connection, packaged_migrations()[2].sql)
        await connection.commit()
        await connection.execute("CREATE SCHEMA audit_migrator_probe")
        await connection.execute("DROP SCHEMA audit_migrator_probe")
        await connection.commit()
    finally:
        await connection.close()
    return migrator_dsn


async def _assert_role_and_object_security(
    raw_dsn: str,
    migrator_dsn: str,
    writer_dsn: str,
    reader_dsn: str,
) -> None:
    admin = await AsyncConnection.connect(raw_dsn)
    try:
        cursor = await admin.execute(
            """
            SELECT nspname, pg_catalog.pg_get_userbyid(nspowner)
            FROM pg_catalog.pg_namespace
            WHERE nspname IN ('audit', 'audit_read')
            ORDER BY nspname
            """
        )
        assert await cursor.fetchall() == [
            ("audit", _MIGRATOR_ROLE),
            ("audit_read", _MIGRATOR_ROLE),
        ]
        cursor = await admin.execute(
            """
            SELECT namespace.nspname, object.relname, pg_catalog.pg_get_userbyid(object.relowner)
            FROM pg_catalog.pg_class AS object
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = object.relnamespace
            WHERE namespace.nspname IN ('audit', 'audit_read')
              AND object.relkind IN ('r', 'v')
            ORDER BY namespace.nspname, object.relname
            """
        )
        assert all(row[2] == _MIGRATOR_ROLE for row in await cursor.fetchall())
        cursor = await admin.execute(
            """
            SELECT
                proname,
                pg_catalog.pg_get_userbyid(proowner),
                prosecdef,
                proconfig
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'audit' AND proname LIKE 'verify_%'
            ORDER BY proname
            """
        )
        assert await cursor.fetchall() == [
            ("verify_event_v1", _MIGRATOR_ROLE, True, ["search_path=pg_catalog"]),
            ("verify_execution_v1", _MIGRATOR_ROLE, True, ["search_path=pg_catalog"]),
        ]
        cursor = await admin.execute(
            """
            SELECT
                pg_catalog.has_function_privilege(
                    'public',
                    'audit.verify_execution_v1(uuid,uuid,text,text,text)',
                    'EXECUTE'
                ),
                pg_catalog.has_function_privilege(
                    'public',
                    'audit.verify_event_v1(uuid,uuid,uuid,smallint,text,smallint,timestamptz,text,jsonb)',
                    'EXECUTE'
                ),
                pg_catalog.has_function_privilege(
                    %s,
                    'audit.verify_execution_v1(uuid,uuid,text,text,text)',
                    'EXECUTE'
                ),
                pg_catalog.has_function_privilege(
                    %s,
                    'audit.verify_event_v1(uuid,uuid,uuid,smallint,text,smallint,timestamptz,text,jsonb)',
                    'EXECUTE'
                )
            """,
            (_WRITER_ROLE, _WRITER_ROLE),
        )
        assert await cursor.fetchone() == (False, False, True, True)
        cursor = await admin.execute(
            """
            SELECT
                pg_catalog.has_schema_privilege(%s, 'audit', 'USAGE'),
                pg_catalog.has_schema_privilege(%s, 'audit', 'CREATE'),
                pg_catalog.has_schema_privilege(%s, 'audit_read', 'USAGE'),
                pg_catalog.has_schema_privilege(%s, 'audit', 'USAGE'),
                pg_catalog.has_schema_privilege(%s, 'audit_read', 'USAGE'),
                pg_catalog.has_column_privilege(%s, 'audit.events', 'event_id', 'INSERT'),
                pg_catalog.has_table_privilege(%s, 'audit.events', 'SELECT'),
                pg_catalog.has_column_privilege(
                    %s, 'audit.schema_version', 'version', 'SELECT'
                ),
                pg_catalog.has_column_privilege(
                    %s, 'audit.schema_version', 'applied_at', 'SELECT'
                ),
                pg_catalog.has_table_privilege(
                    %s, 'audit_read.execution_summary', 'SELECT'
                ),
                pg_catalog.has_table_privilege(
                    %s, 'audit_read.execution_summary', 'UPDATE'
                ),
                pg_catalog.has_table_privilege(%s, 'audit.events', 'SELECT')
            """,
            (
                _WRITER_ROLE,
                _WRITER_ROLE,
                _WRITER_ROLE,
                _READER_ROLE,
                _READER_ROLE,
                _WRITER_ROLE,
                _WRITER_ROLE,
                _WRITER_ROLE,
                _WRITER_ROLE,
                _READER_ROLE,
                _READER_ROLE,
                _READER_ROLE,
            ),
        )
        assert await cursor.fetchone() == (
            True,
            False,
            False,
            False,
            True,
            True,
            False,
            True,
            False,
            True,
            False,
            False,
        )
    finally:
        await admin.close()

    writer_denials: tuple[LiteralString, ...] = (
        "SELECT * FROM audit.executions",
        "SELECT applied_at FROM audit.schema_version",
        "SELECT * FROM audit_read.execution_summary",
        "UPDATE audit.events SET content_hash = repeat('0', 64)",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
        "ALTER TABLE audit.events ADD COLUMN forbidden integer",
        "CREATE SCHEMA writer_forbidden",
        "CREATE TEMP TABLE writer_forbidden (value integer)",
        "GRANT SELECT ON audit.events TO maestro_audit_reader",
        "CREATE TRIGGER writer_forbidden BEFORE UPDATE ON audit.events "
        "FOR EACH ROW EXECUTE FUNCTION pg_catalog.suppress_redundant_updates_trigger()",
        "SET ROLE maestro_audit_migrator",
        "GRANT maestro_audit_reader TO maestro_audit_writer",
    )
    for statement in writer_denials:
        await _assert_denied(writer_dsn, statement)

    reader_denials: tuple[LiteralString, ...] = (
        "SELECT * FROM audit.events",
        "INSERT INTO audit.events DEFAULT VALUES",
        "CREATE SCHEMA reader_forbidden",
        "CREATE TEMP TABLE reader_forbidden (value integer)",
        "SET ROLE maestro_audit_writer",
        "SELECT audit.verify_execution_v1(gen_random_uuid(), gen_random_uuid(), '', '', '')",
    )
    for statement in reader_denials:
        await _assert_denied(reader_dsn, statement)

    migrator = await AsyncConnection.connect(migrator_dsn)
    try:
        await migrator.execute("CREATE TABLE audit.default_table_probe (value integer)")
        await migrator.execute(
            "CREATE VIEW audit_read.default_view_probe AS SELECT 1::integer AS value"
        )
        await migrator.execute(
            "CREATE FUNCTION audit.default_function_probe() RETURNS integer "
            "LANGUAGE sql AS 'SELECT 1'"
        )
        await migrator.commit()
    finally:
        await migrator.close()

    await _assert_denied(writer_dsn, "SELECT * FROM audit.default_table_probe")
    await _assert_denied(writer_dsn, "INSERT INTO audit.default_table_probe VALUES (1)")
    await _assert_denied(reader_dsn, "SELECT * FROM audit.default_table_probe")
    reader = await AsyncConnection.connect(reader_dsn)
    try:
        cursor = await reader.execute("SELECT value FROM audit_read.default_view_probe")
        assert await cursor.fetchone() == (1,)
    finally:
        await reader.close()
    admin = await AsyncConnection.connect(raw_dsn)
    try:
        cursor = await admin.execute(
            """
            SELECT
                pg_catalog.has_function_privilege(
                    'public', 'audit.default_function_probe()', 'EXECUTE'
                ),
                pg_catalog.has_function_privilege(
                    %s, 'audit.default_function_probe()', 'EXECUTE'
                ),
                pg_catalog.has_function_privilege(
                    %s, 'audit.default_function_probe()', 'EXECUTE'
                )
            """,
            (_WRITER_ROLE, _READER_ROLE),
        )
        assert await cursor.fetchone() == (False, False, False)
    finally:
        await admin.close()

    await _assert_denied(migrator_dsn, "CREATE ROLE migrator_forbidden")
    await _assert_denied(
        migrator_dsn,
        "CREATE DATABASE migrator_forbidden",
        autocommit=True,
    )


async def _write_view_scenarios(port: PostgresAuditPort) -> dict[str, UUID]:
    uncertain_start, uncertain_completion, _repository = await _records()
    resolved_start, resolved_completion = await _completion_records(
        1001,
        repository_id="b" * 16,
        objective="Where is the supported behavior established?",
        result=AuditInvestigationCompletionInput(
            status=AuditResultStatus.RESOLVED,
            answer="The implementation and migration establish it.",
            confidence=AuditConfidence.HIGH,
            rationale="The resolved evidence establishes the behavior.",
            evidence=(
                AuditEvidenceInput(
                    path="src/maestro/audit/contracts.py",
                    line_start=10,
                    line_end=12,
                    symbol="AuditEventV1",
                    finding="The contract defines the semantic event.",
                ),
            ),
            conflicts=(
                AuditConflictInput(
                    description="An older document describes a different behavior.",
                    evidence=(
                        AuditEvidenceInput(
                            path="docs/legacy.md",
                            line_start=3,
                            line_end=3,
                            symbol=None,
                            finding="The older document conflicts with the implementation.",
                        ),
                    ),
                ),
            ),
        ),
    )
    human_start, human_completion = await _completion_records(
        1101,
        repository_id="c" * 16,
        objective="Which policy should the maintainers select?",
        result=AuditInvestigationCompletionInput(
            status=AuditResultStatus.HUMAN_DECISION_REQUIRED,
            answer=None,
            confidence=AuditConfidence.MEDIUM,
            evidence=(),
            conflicts=(),
            rationale="Maintainer authority is required.",
        ),
    )
    failed_start, failure = await _failure_records(1201)
    incomplete_start = await _incomplete_record(1301)

    await asyncio.gather(
        port.start_execution(uncertain_start),
        port.start_execution(uncertain_start),
    )
    await port.complete_investigation(uncertain_completion)
    await port.start_execution(resolved_start)
    await port.complete_investigation(resolved_completion)
    await port.start_execution(human_start)
    await port.complete_investigation(human_completion)
    await port.start_execution(failed_start)
    await port.fail_execution(failure)
    await port.start_execution(incomplete_start)
    await port.complete_investigation(uncertain_completion)

    return {
        "uncertain": uncertain_start.execution.execution_id,
        "resolved": resolved_start.execution.execution_id,
        "human_decision_required": human_start.execution.execution_id,
        "failed": failed_start.execution.execution_id,
        "incomplete": incomplete_start.execution.execution_id,
        "resolved_audit": resolved_start.execution.audit_id,
        "resolved_start_event": resolved_start.event.event_id,
        "resolved_terminal_event": resolved_completion.event.event_id,
    }


async def _assert_curated_views(reader_dsn: str, identities: dict[str, UUID]) -> None:
    reader = await AsyncConnection.connect(reader_dsn)
    try:
        cursor = await reader.execute(
            """
            SELECT
                execution_id,
                outcome,
                is_incomplete,
                error_code,
                failure_stage,
                evidence_count,
                conflict_count
            FROM audit_read.execution_summary
            ORDER BY execution_id
            """
        )
        rows = {row[0]: row[1:] for row in await cursor.fetchall()}
        assert rows[identities["uncertain"]] == ("uncertain", False, None, None, 0, 0)
        assert rows[identities["resolved"]] == ("resolved", False, None, None, 1, 1)
        assert rows[identities["human_decision_required"]] == (
            "human_decision_required",
            False,
            None,
            None,
            0,
            0,
        )
        assert rows[identities["failed"]] == (
            "failed",
            False,
            "AGENT_RUNTIME_ERROR",
            "investigation",
            0,
            0,
        )
        assert rows[identities["incomplete"]] == ("incomplete", True, None, None, 0, 0)

        cursor = await reader.execute(
            """
            SELECT outcome, count(*)
            FROM audit_read.execution_summary
            GROUP BY outcome
            ORDER BY outcome
            """
        )
        assert await cursor.fetchall() == [
            ("failed", 1),
            ("human_decision_required", 1),
            ("incomplete", 1),
            ("resolved", 1),
            ("uncertain", 1),
        ]
        cursor = await reader.execute(
            """
            SELECT count(*)
            FROM audit_read.execution_summary
            WHERE repository_id = %s
            """,
            ("a" * 16,),
        )
        assert await cursor.fetchone() == (2,)

        cursor = await reader.execute(
            """
            SELECT event_id, sequence, event_type
            FROM audit_read.event_timeline
            WHERE audit_id = %s
            ORDER BY sequence
            """,
            (identities["resolved_audit"],),
        )
        assert await cursor.fetchall() == [
            (identities["resolved_start_event"], 1, "execution.started"),
            (identities["resolved_terminal_event"], 2, "investigation.completed"),
        ]

        cursor = await reader.execute(
            """
            SELECT
                evidence_scope,
                conflict_ordinal,
                conflict_description,
                evidence_ordinal,
                path,
                line_start,
                line_end,
                symbol,
                finding
            FROM audit_read.evidence
            WHERE execution_id = %s
            ORDER BY evidence_scope DESC, evidence_ordinal
            """,
            (identities["resolved"],),
        )
        assert await cursor.fetchall() == [
            (
                "primary",
                None,
                None,
                1,
                "src/maestro/audit/contracts.py",
                "10",
                "12",
                "AuditEventV1",
                "The contract defines the semantic event.",
            ),
            (
                "conflict",
                1,
                "An older document describes a different behavior.",
                1,
                "docs/legacy.md",
                "3",
                "3",
                None,
                "The older document conflicts with the implementation.",
            ),
        ]
        cursor = await reader.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'audit_read'
            ORDER BY table_name, ordinal_position
            """
        )
        exposed_columns = {row[0] for row in await cursor.fetchall()}
        assert "payload" not in exposed_columns
        assert "content_hash" not in exposed_columns
        assert "repository_fingerprint" not in exposed_columns
        assert "persisted_at" not in exposed_columns
        assert "created_at" not in exposed_columns
    finally:
        await reader.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
async def test_postgres_roles_views_and_forward_migration() -> None:
    raw_dsn = os.environ.get(_TEST_DSN_ENV)
    if raw_dsn is None:
        pytest.skip(f"set {_TEST_DSN_ENV} to run PostgreSQL Audit adapter tests")
    migrator_password, writer_password, reader_password = await _bootstrap_v2_database(raw_dsn)
    migrator_dsn = await _apply_roles_and_views(raw_dsn, migrator_password)
    writer_dsn = _role_dsn(raw_dsn, _WRITER_ROLE, writer_password, "maestro-audit-issue11")
    reader_dsn = _role_dsn(raw_dsn, _READER_ROLE, reader_password, "maestro-audit-reader")
    port = PostgresAuditPort(_writer_configuration_from_dsn(writer_dsn))
    start, completion, _repository = await _records()
    identities = await _write_view_scenarios(port)
    new_audit_id = await _assert_native_mismatches_rollback(port, start, completion)
    unsafe_audit_id = await _assert_native_unsafe_durability_rejected(writer_dsn, start)
    await _assert_curated_views(reader_dsn, identities)
    await _assert_role_and_object_security(raw_dsn, migrator_dsn, writer_dsn, reader_dsn)

    observer = await AsyncConnection.connect(raw_dsn)
    try:
        cursor = await observer.execute(
            "SELECT count(*) FROM audit.executions WHERE audit_id IN (%s, %s)",
            (new_audit_id, unsafe_audit_id),
        )
        assert await cursor.fetchone() == (0,)
        cursor = await observer.execute(
            """
            SELECT
                (SELECT count(*) FROM audit.executions),
                (SELECT count(*) FROM audit.events),
                (SELECT count(*) FROM pg_stat_activity WHERE application_name = %s)
            """,
            ("maestro-audit-writer",),
        )
        assert await cursor.fetchone() == (5, 9, 0)
    finally:
        await observer.close()
