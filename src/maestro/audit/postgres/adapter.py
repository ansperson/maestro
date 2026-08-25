"""Short-transaction Psycopg adapter for the Audit tracer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection, Error as PsycopgError, OperationalError
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from maestro.audit.contracts import (
    AuditEventV1,
    AuditExecutionStartV1,
    AuditInvestigationCompletionV1,
)
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind

_SUPPORTED_SCHEMA_VERSION = 1
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "53300", "57P01", "57P02", "57P03"})
_TRANSACTION_ABORT_SQLSTATES = frozenset({"40001", "40P01"})

type _TransactionOperation = Callable[[AsyncConnection[tuple[object, ...]]], Awaitable[None]]


class PostgresAuditPort:
    """Persist Audit records without retaining a connection between operations."""

    def __init__(self, database_url: SecretStr) -> None:
        self._database_url = database_url

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        """Atomically insert the execution row and sequence-one start event."""

        async def write(connection: AsyncConnection[tuple[object, ...]]) -> None:
            await _verify_schema(connection)
            await connection.execute(
                """
                INSERT INTO audit.executions (
                    audit_id,
                    execution_id,
                    capability,
                    repository_id,
                    repository_fingerprint
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    record.execution.audit_id,
                    record.execution.execution_id,
                    record.execution.capability,
                    record.execution.repository_id,
                    record.execution.repository_fingerprint,
                ),
            )
            await _insert_event(connection, record.event)

        await _run_transaction(self._database_url.get_secret_value(), write)

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        """Insert the single sequence-two completion in its own short transaction."""

        async def write(connection: AsyncConnection[tuple[object, ...]]) -> None:
            await _verify_schema(connection)
            await _insert_event(connection, record.event)

        await _run_transaction(self._database_url.get_secret_value(), write)


async def _run_transaction(database_url: str, operation: _TransactionOperation) -> None:
    try:
        connection = await AsyncConnection.connect(database_url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _classify_connection_failure(exc) from None

    failure: AuditWriteError | None = None
    body_completed = False
    try:
        try:
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


async def _insert_event(
    connection: AsyncConnection[tuple[object, ...]],
    event: AuditEventV1,
) -> None:
    await connection.execute(
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
        """,
        (
            event.event_id,
            event.audit_id,
            event.sequence,
            event.event_type.value,
            event.event_version,
            event.occurred_at,
            event.content_hash(),
            Jsonb(event.payload.model_dump(mode="json")),
        ),
    )
