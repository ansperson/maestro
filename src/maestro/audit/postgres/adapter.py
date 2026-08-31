"""Short-transaction Psycopg adapter for the Audit tracer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection, Error as PsycopgError, OperationalError
from psycopg.types.json import Jsonb

from maestro.audit.contracts import (
    AuditAuthorityApplicationV1,
    AuditEventV1,
    AuditExecutionFailureV1,
    AuditExecutionStartV1,
    AuditInvestigationCompletionV1,
)
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind
from maestro.config import AuditWriterConfiguration, validate_audit_libpq_environment

_SUPPORTED_SCHEMA_VERSION = 4
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "53300", "57P01", "57P02", "57P03"})
_TRANSACTION_ABORT_SQLSTATES = frozenset({"40001", "40P01"})
_SAFE_SYNCHRONOUS_COMMIT_VALUES = frozenset({"on", "remote_apply"})

type _TransactionOperation = Callable[[AsyncConnection[tuple[object, ...]]], Awaitable[None]]
type _ConnectionReady = Callable[[AsyncConnection[tuple[object, ...]]], None]


@dataclass(slots=True)
class _ActiveFailureWrite:
    """Event-loop-owned state for synchronously aborting one failure write."""

    connection: AsyncConnection[tuple[object, ...]] | None = None
    abort_requested: bool = False

    def attach(self, connection: AsyncConnection[tuple[object, ...]]) -> None:
        self.connection = connection
        if self.abort_requested:
            self._finish_connection()
            raise AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)

    def abort(self) -> None:
        self.abort_requested = True
        self._finish_connection()

    def _finish_connection(self) -> None:
        if self.connection is None:
            return
        with suppress(Exception):
            self.connection.pgconn.finish()


class PostgresAuditPort:
    """Persist Audit records without retaining a connection between operations."""

    def __init__(self, configuration: AuditWriterConfiguration) -> None:
        self._configuration = configuration
        self._active_failure_writes: dict[UUID, _ActiveFailureWrite] = {}

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        """Atomically insert the execution row and sequence-one start event."""

        async def write(connection: AsyncConnection[tuple[object, ...]]) -> None:
            await _verify_schema(connection)
            await _verify_database_durability(connection)
            execution_inserted = await _insert_execution(connection, record)
            event_inserted = await _insert_event(
                connection,
                record.event,
                record.content_hash,
            )
            if not execution_inserted or not event_inserted:
                await _verify_start_record(connection, record)

        await _run_transaction(self._configuration, write)

    async def apply_authority(self, record: AuditAuthorityApplicationV1) -> None:
        """Insert the single sequence-two applied decision in its own short transaction."""

        async def write(connection: AsyncConnection[tuple[object, ...]]) -> None:
            await _verify_schema(connection)
            if not await _insert_event(connection, record.event, record.content_hash):
                await _verify_event_record(
                    connection,
                    record.execution_id,
                    record.event,
                    record.content_hash,
                )

        await _run_transaction(self._configuration, write)

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        """Insert the single sequence-two completion in its own short transaction."""

        async def write(connection: AsyncConnection[tuple[object, ...]]) -> None:
            await _verify_schema(connection)
            if not await _insert_event(connection, record.event, record.content_hash):
                await _verify_event_record(
                    connection,
                    record.execution_id,
                    record.event,
                    record.content_hash,
                )

        await _run_transaction(self._configuration, write)

    async def fail_execution(self, record: AuditExecutionFailureV1) -> None:
        """Insert the single sequence-two operational failure in a short transaction."""

        event_id = record.event.event_id
        if event_id in self._active_failure_writes:
            raise AuditWriteError(AuditWriteFailureKind.PERMANENT)
        active = _ActiveFailureWrite()
        self._active_failure_writes[event_id] = active

        async def write(connection: AsyncConnection[tuple[object, ...]]) -> None:
            await _verify_schema(connection)
            if not await _insert_event(connection, record.event, record.content_hash):
                await _verify_event_record(
                    connection,
                    record.execution_id,
                    record.event,
                    record.content_hash,
                )

        try:
            await _run_transaction(
                self._configuration,
                write,
                connection_ready=active.attach,
            )
        finally:
            if self._active_failure_writes.get(event_id) is active:
                del self._active_failure_writes[event_id]

    def abort_execution_failure(self, event_id: UUID) -> None:
        """Synchronously finish the active libpq connection for one failure write."""

        active = self._active_failure_writes.get(event_id)
        if active is not None:
            active.abort()


async def _run_transaction(
    configuration: AuditWriterConfiguration,
    operation: _TransactionOperation,
    *,
    connection_ready: _ConnectionReady | None = None,
) -> None:
    try:
        validate_audit_libpq_environment()
        connection = await AsyncConnection.connect(
            host=configuration.host,
            port=configuration.port,
            dbname=configuration.database,
            user=configuration.user,
            password=configuration.password.get_secret_value(),
            application_name="maestro-audit-writer",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _classify_connection_failure(exc) from None

    failure: AuditWriteError | None = None
    body_completed = False
    try:
        try:
            if connection_ready is not None:
                connection_ready(connection)
            async with connection.transaction():
                await operation(connection)
                body_completed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = (
                _classify_commit_failure(exc)
                if body_completed
                else _classify_precommit_failure(exc)
            )
    finally:
        try:
            await connection.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            if failure is None:
                failure = AuditWriteError(AuditWriteFailureKind.AMBIGUOUS)
    if failure is not None:
        raise failure from None


def _classify_connection_failure(error: Exception) -> AuditWriteError:
    sqlstate = _sqlstate(error)
    if (
        isinstance(error, TimeoutError)
        or (isinstance(error, OperationalError) and (sqlstate is None or sqlstate.startswith("08")))
        or sqlstate in _RETRYABLE_SQLSTATES
    ):
        return AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)
    return AuditWriteError(AuditWriteFailureKind.PERMANENT)


def _classify_precommit_failure(error: Exception) -> AuditWriteError:
    if isinstance(error, AuditWriteError):
        return error
    sqlstate = _sqlstate(error)
    if (
        isinstance(error, TimeoutError)
        or (isinstance(error, OperationalError) and (sqlstate is None or sqlstate.startswith("08")))
        or sqlstate in _RETRYABLE_SQLSTATES
    ):
        return AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)
    return AuditWriteError(AuditWriteFailureKind.PERMANENT)


def _classify_commit_failure(error: Exception) -> AuditWriteError:
    if isinstance(error, AuditWriteError):
        return error
    if _sqlstate(error) in _TRANSACTION_ABORT_SQLSTATES:
        return AuditWriteError(AuditWriteFailureKind.RETRYABLE_NOT_COMMITTED)
    return AuditWriteError(AuditWriteFailureKind.AMBIGUOUS)


def _sqlstate(error: Exception) -> str | None:
    return error.sqlstate if isinstance(error, PsycopgError) else None


async def _verify_schema(connection: AsyncConnection[tuple[object, ...]]) -> None:
    cursor = await connection.execute("SELECT version FROM audit.schema_version WHERE singleton")
    row = await cursor.fetchone()
    if row is None or row[0] != _SUPPORTED_SCHEMA_VERSION:
        raise AuditWriteError(AuditWriteFailureKind.PERMANENT)


async def _verify_database_durability(
    connection: AsyncConnection[tuple[object, ...]],
) -> None:
    checks: tuple[tuple[LiteralString, frozenset[str]], ...] = (
        ("SHOW fsync", frozenset({"on"})),
        ("SHOW full_page_writes", frozenset({"on"})),
        ("SHOW synchronous_commit", _SAFE_SYNCHRONOUS_COMMIT_VALUES),
    )
    for statement, accepted_values in checks:
        cursor = await connection.execute(statement)
        row = await cursor.fetchone()
        if (
            row is None
            or len(row) != 1
            or not isinstance(row[0], str)
            or row[0].lower() not in accepted_values
        ):
            raise AuditWriteError(AuditWriteFailureKind.PERMANENT)


async def _insert_execution(
    connection: AsyncConnection[tuple[object, ...]],
    record: AuditExecutionStartV1,
) -> bool:
    cursor = await connection.execute(
        """
        INSERT INTO audit.executions (
            audit_id,
            execution_id,
            capability,
            repository_id,
            repository_fingerprint
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            record.execution.audit_id,
            record.execution.execution_id,
            record.execution.capability,
            record.execution.repository_id,
            record.execution.repository_fingerprint,
        ),
    )
    return cursor.rowcount == 1


async def _insert_event(
    connection: AsyncConnection[tuple[object, ...]],
    event: AuditEventV1,
    content_hash: str,
) -> bool:
    cursor = await connection.execute(
        """
        INSERT INTO audit.events (
            event_id,
            audit_id,
            sequence,
            event_type,
            event_version,
            occurred_at,
            content_hash,
            payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            event.event_id,
            event.audit_id,
            event.sequence,
            event.event_type.value,
            event.event_version,
            event.occurred_at,
            content_hash,
            Jsonb(event.payload.model_dump(mode="json")),
        ),
    )
    return cursor.rowcount == 1


async def _verify_start_record(
    connection: AsyncConnection[tuple[object, ...]],
    record: AuditExecutionStartV1,
) -> None:
    cursor = await connection.execute(
        """
        SELECT audit.verify_execution_v1(%s, %s, %s, %s, %s)
        """,
        (
            record.execution.audit_id,
            record.execution.execution_id,
            record.execution.capability,
            record.execution.repository_id,
            record.execution.repository_fingerprint,
        ),
    )
    if await cursor.fetchone() != (True,):
        raise AuditWriteError(AuditWriteFailureKind.PERMANENT)
    await _verify_event_record(
        connection,
        record.execution.execution_id,
        record.event,
        record.content_hash,
    )


async def _verify_event_record(
    connection: AsyncConnection[tuple[object, ...]],
    execution_id: UUID,
    event: AuditEventV1,
    content_hash: str,
) -> None:
    cursor = await connection.execute(
        """
        SELECT audit.verify_event_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event.event_id,
            event.audit_id,
            execution_id,
            event.sequence,
            event.event_type.value,
            event.event_version,
            event.occurred_at,
            content_hash,
            Jsonb(event.payload.model_dump(mode="json")),
        ),
    )
    if await cursor.fetchone() != (True,):
        raise AuditWriteError(AuditWriteFailureKind.PERMANENT)
