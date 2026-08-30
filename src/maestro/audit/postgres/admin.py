"""Explicit package-owned PostgreSQL bootstrap, migration, and curated-read commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import LiteralString, cast
from uuid import UUID

from psycopg import AsyncConnection, Connection, Error as PsycopgError, sql
from pydantic import ValidationError

from maestro.config import (
    AuditBootstrapConfiguration,
    AuditBootstrapSettings,
    AuditMigrationConfiguration,
    AuditMigrationSettings,
    AuditReaderConfiguration,
    AuditReaderSettings,
    AuditWriterSettings,
    validate_audit_libpq_environment,
)

from .migrations import packaged_migrations, packaged_role_bootstrap_body

_SUPPORTED_SCHEMA_VERSION = 3
_ROLE_NAMES = {
    "migration": "maestro_audit_migrator",
    "writer": "maestro_audit_writer",
    "reader": "maestro_audit_reader",
}
_ConnectionConfiguration = (
    AuditBootstrapConfiguration | AuditMigrationConfiguration | AuditReaderConfiguration
)


class AuditAdministrationError(RuntimeError):
    """An explicit Audit administration operation could not be completed safely."""


class ReadView(StrEnum):
    SUMMARY = "summary"
    TIMELINE = "timeline"
    EVIDENCE = "evidence"


async def bootstrap_roles() -> None:
    """Create fixed roles once and provision their distinct SCRAM password verifiers."""

    bootstrap = AuditBootstrapSettings()  # pyright: ignore[reportCallIssue]
    migration = AuditMigrationSettings()  # pyright: ignore[reportCallIssue]
    writer = AuditWriterSettings()  # pyright: ignore[reportCallIssue]
    reader = AuditReaderSettings()  # pyright: ignore[reportCallIssue]
    bootstrap_password = bootstrap.connection_configuration().password.get_secret_value()
    passwords = {
        "migration": migration.connection_configuration().password.get_secret_value(),
        "writer": writer.connection_configuration().password.get_secret_value(),
        "reader": reader.connection_configuration().password.get_secret_value(),
    }
    if len({bootstrap_password, *passwords.values()}) != len(passwords) + 1:
        raise AuditAdministrationError("Audit role passwords must be distinct")
    await asyncio.to_thread(
        _bootstrap_roles_sync,
        bootstrap.connection_configuration(),
        passwords,
    )


def _bootstrap_roles_sync(
    configuration: AuditBootstrapConfiguration,
    passwords: dict[str, str],
) -> None:
    """Keep role creation and password provisioning in one rollback-capable transaction."""

    validate_audit_libpq_environment()
    connection = Connection.connect(
        host=configuration.host,
        port=configuration.port,
        dbname=configuration.database,
        user=configuration.user,
        password=configuration.password.get_secret_value(),
        application_name="maestro-audit-bootstrap",
    )
    try:
        with connection.transaction():
            _execute_resource_sync(connection, packaged_role_bootstrap_body())
            # `password_encryption` names the PostgreSQL hashing algorithm, not a credential.
            connection.execute(
                "SET LOCAL password_encryption = 'scram-sha-256'"  # pragma: allowlist secret
            )
            encoding = connection.info.encoding
            for scope, role_name in _ROLE_NAMES.items():
                connection.pgconn.change_password(
                    role_name.encode(encoding),
                    passwords[scope].encode(encoding),
                )
            cursor = connection.execute(
                """
                SELECT count(*)
                FROM pg_catalog.pg_authid
                WHERE rolname IN (%s, %s, %s)
                  AND rolpassword LIKE 'SCRAM-SHA-256$%%'
                """,
                tuple(_ROLE_NAMES.values()),
            )
            if cursor.fetchone() != (3,):
                raise AuditAdministrationError("role password provisioning was not verified")
    finally:
        connection.close()


async def migrate_schema() -> None:
    """Apply only ordered forward migrations as the fixed migration owner."""

    settings = AuditMigrationSettings()  # pyright: ignore[reportCallIssue]
    connection = await _connect(
        settings.connection_configuration(),
        application_name="maestro-audit-migration",
        autocommit=True,
    )
    try:
        current = await _schema_version(connection)
        if not 0 <= current <= _SUPPORTED_SCHEMA_VERSION:
            raise AuditAdministrationError("the Audit schema version is unsupported")
        for migration in packaged_migrations():
            if migration.version > current:
                await _execute_resource(connection, migration.sql)
        if await _schema_version(connection) != _SUPPORTED_SCHEMA_VERSION:
            raise AuditAdministrationError("the Audit schema migration was not verified")
    finally:
        await connection.close()


async def read_curated_view(
    *,
    view: ReadView,
    audit_id: UUID | None,
    execution_id: UUID | None,
    repository_id: str | None,
    outcome: str | None,
) -> tuple[dict[str, object], ...]:
    """Read one fixed curated view with bound optional filters."""

    if outcome is not None and view is not ReadView.SUMMARY:
        raise AuditAdministrationError("outcome filtering is available only for summary reads")
    settings = AuditReaderSettings()  # pyright: ignore[reportCallIssue]
    connection = await _connect(
        settings.connection_configuration(),
        application_name="maestro-audit-reader",
        autocommit=True,
    )
    try:
        cursor = await connection.execute(
            _read_statement(view),
            (
                audit_id,
                audit_id,
                execution_id,
                execution_id,
                repository_id,
                repository_id,
                outcome,
                outcome,
            ),
        )
        if cursor.description is None:
            raise AuditAdministrationError("the curated reader returned no columns")
        columns = tuple(column.name for column in cursor.description)
        rows = await cursor.fetchall()
        return tuple(
            {name: _json_value(value) for name, value in zip(columns, row, strict=True)}
            for row in rows
        )
    finally:
        await connection.close()


async def _connect(
    configuration: _ConnectionConfiguration,
    *,
    application_name: str,
    autocommit: bool,
) -> AsyncConnection[tuple[object, ...]]:
    validate_audit_libpq_environment()
    return await AsyncConnection.connect(
        host=configuration.host,
        port=configuration.port,
        dbname=configuration.database,
        user=configuration.user,
        password=configuration.password.get_secret_value(),
        application_name=application_name,
        autocommit=autocommit,
    )


async def _execute_resource(
    connection: AsyncConnection[tuple[object, ...]],
    resource: str,
) -> None:
    reviewed_sql = cast(LiteralString, resource)
    await connection.execute(sql.SQL(reviewed_sql))


def _execute_resource_sync(
    connection: Connection[tuple[object, ...]],
    resource: str,
) -> None:
    reviewed_sql = cast(LiteralString, resource)
    connection.execute(sql.SQL(reviewed_sql))


async def _schema_version(connection: AsyncConnection[tuple[object, ...]]) -> int:
    cursor = await connection.execute("SELECT pg_catalog.to_regclass('audit.schema_version')")
    row = await cursor.fetchone()
    if row is None or len(row) != 1:
        raise AuditAdministrationError("the Audit schema state is invalid")
    if row[0] is None:
        return 0
    cursor = await connection.execute("SELECT version FROM audit.schema_version WHERE singleton")
    version_row = await cursor.fetchone()
    if version_row is None or len(version_row) != 1 or not isinstance(version_row[0], int):
        raise AuditAdministrationError("the Audit schema state is invalid")
    return version_row[0]


def _read_statement(view: ReadView) -> LiteralString:
    return cast(
        LiteralString,
        {
            ReadView.SUMMARY: """
            SELECT * FROM audit_read.execution_summary
            WHERE (%s::uuid IS NULL OR audit_id = %s::uuid)
              AND (%s::uuid IS NULL OR execution_id = %s::uuid)
              AND (%s::text IS NULL OR repository_id = %s::text)
              AND (%s::text IS NULL OR outcome = %s::text)
            ORDER BY audit_id DESC LIMIT 100
        """,
            ReadView.TIMELINE: """
            SELECT * FROM audit_read.event_timeline
            WHERE (%s::uuid IS NULL OR audit_id = %s::uuid)
              AND (%s::uuid IS NULL OR execution_id = %s::uuid)
              AND (%s::text IS NULL OR repository_id = %s::text)
              AND (%s::text IS NULL OR %s::text IS NULL)
            ORDER BY audit_id DESC LIMIT 100
        """,
            ReadView.EVIDENCE: """
            SELECT * FROM audit_read.evidence
            WHERE (%s::uuid IS NULL OR audit_id = %s::uuid)
              AND (%s::uuid IS NULL OR execution_id = %s::uuid)
              AND (%s::text IS NULL OR repository_id = %s::text)
              AND (%s::text IS NULL OR %s::text IS NULL)
            ORDER BY audit_id DESC LIMIT 100
        """,
        }[view],
    )


def _json_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise AuditAdministrationError("the curated reader returned an unsupported value")


def _repository_id(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{16}", value) is None:
        raise argparse.ArgumentTypeError("repository identity is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("bootstrap")
    subparsers.add_parser("migrate")
    reader = subparsers.add_parser("read")
    reader.add_argument("--view", choices=tuple(view.value for view in ReadView), default="summary")
    reader.add_argument("--audit-id", type=UUID)
    reader.add_argument("--execution-id", type=UUID)
    reader.add_argument("--repository-id", type=_repository_id)
    reader.add_argument(
        "--outcome",
        choices=("resolved", "uncertain", "human_decision_required", "failed", "incomplete"),
    )
    return parser


async def _run(options: argparse.Namespace) -> None:
    operation = cast(str, options.operation)
    if operation == "bootstrap":
        await bootstrap_roles()
    elif operation == "migrate":
        await migrate_schema()
    else:
        rows = await read_curated_view(
            view=ReadView(cast(str, options.view)),
            audit_id=cast(UUID | None, options.audit_id),
            execution_id=cast(UUID | None, options.execution_id),
            repository_id=cast(str | None, options.repository_id),
            outcome=cast(str | None, options.outcome),
        )
        for row in rows:
            print(json.dumps(row, sort_keys=True, separators=(",", ":")))


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one explicit operation with safe public failure output."""

    options = _parser().parse_args(arguments)
    try:
        asyncio.run(_run(options))
    except (AuditAdministrationError, OSError, PsycopgError, ValidationError, ValueError):
        print("Audit administration operation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
