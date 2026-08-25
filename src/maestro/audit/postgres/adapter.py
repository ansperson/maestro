"""Short-transaction Psycopg adapter for the Audit tracer."""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from maestro.audit.contracts import (
    AuditEventV1,
    AuditExecutionStartV1,
    AuditInvestigationCompletionV1,
)

_SUPPORTED_SCHEMA_VERSION = 1


class PostgresAuditPort:
    """Persist Audit records without retaining a connection between operations."""

    def __init__(self, database_url: SecretStr) -> None:
        self._database_url = database_url

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        """Atomically insert the execution row and sequence-one start event."""

        connection = await AsyncConnection.connect(self._database_url.get_secret_value())
        try:
            async with connection.transaction():
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
        finally:
            await connection.close()

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        """Insert the single sequence-two completion in its own short transaction."""

        connection = await AsyncConnection.connect(self._database_url.get_secret_value())
        try:
            async with connection.transaction():
                await _verify_schema(connection)
                await _insert_event(connection, record.event)
        finally:
            await connection.close()


async def _verify_schema(connection: AsyncConnection[tuple[object, ...]]) -> None:
    cursor = await connection.execute("SELECT version FROM audit.schema_version WHERE singleton")
    row = await cursor.fetchone()
    if row is None or row[0] != _SUPPORTED_SCHEMA_VERSION:
        raise RuntimeError("The configured Audit schema version is unsupported.")


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
