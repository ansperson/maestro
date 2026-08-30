from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import cast
from uuid import UUID

import pytest
from psycopg import sql

import maestro.audit.postgres.admin as admin_module
from maestro.audit.postgres.migrations import (
    packaged_migrations,
    packaged_role_bootstrap,
    packaged_role_bootstrap_body,
)


def _configure_role_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for role in ("bootstrap", "migration", "writer", "reader"):
        value = f"distinct-{role}-password"
        path = tmp_path / f"{role}-password"
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
        prefix = f"MAESTRO_AUDIT_{role.upper()}_"
        monkeypatch.setenv(f"{prefix}HOST", "audit-postgres")
        monkeypatch.setenv(f"{prefix}PORT", "5432")
        monkeypatch.setenv(f"{prefix}DATABASE", "maestro")
        monkeypatch.setenv(
            f"{prefix}USER",
            {
                "bootstrap": "postgres",
                "migration": "maestro_audit_migrator",
                "writer": "maestro_audit_writer",
                "reader": "maestro_audit_reader",
            }[role],
        )
        monkeypatch.setenv(f"{prefix}PASSWORD_FILE", str(path))
        values[role] = value
    return values


def test_packaged_bootstrap_body_preserves_only_the_transaction_contents() -> None:
    resource = packaged_role_bootstrap()
    body = packaged_role_bootstrap_body()

    assert resource == f"BEGIN;\n{body}\nCOMMIT;\n"
    assert "CREATE ROLE maestro_audit_writer" in body
    assert not body.startswith("BEGIN")
    assert not body.endswith("COMMIT;")


class _Transaction(AbstractContextManager[None]):
    def __init__(self) -> None:
        self.exception_type: type[BaseException] | None = None

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exception, traceback
        self.exception_type = exception_type
        return False


class _SyncCursor:
    def fetchone(self) -> tuple[int]:
        return (3,)


class _PGConnection:
    def __init__(self, owner: _SyncConnection) -> None:
        self._owner = owner

    def change_password(self, user: bytes, password: bytes) -> None:
        self._owner.password_calls.append((user, password))


class _ConnectionInfo:
    encoding = "utf-8"


class _SyncConnection:
    def __init__(self) -> None:
        self.pgconn = _PGConnection(self)
        self.info = _ConnectionInfo()
        self.transaction_state = _Transaction()
        self.statements: list[str] = []
        self.password_calls: list[tuple[bytes, bytes]] = []
        self.closed = False

    def transaction(self) -> _Transaction:
        return self.transaction_state

    def execute(
        self,
        statement: str | sql.SQL,
        parameters: tuple[str, str, str] | None = None,
    ) -> _SyncCursor:
        del parameters
        self.statements.append(
            statement.as_string() if isinstance(statement, sql.SQL) else statement
        )
        return _SyncCursor()

    def close(self) -> None:
        self.closed = True


def test_bootstrap_uses_libpq_password_change_inside_one_transaction_without_sql_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = _configure_role_files(monkeypatch, tmp_path)
    connection = _SyncConnection()

    def fake_connection_connect(**_kwargs: object) -> admin_module.Connection[tuple[object, ...]]:
        return cast(admin_module.Connection[tuple[object, ...]], connection)

    monkeypatch.setattr(
        admin_module.Connection,
        "connect",
        fake_connection_connect,
    )

    assert admin_module.main(["bootstrap"]) == 0

    assert len(connection.password_calls) == 3
    assert {password.decode() for _user, password in connection.password_calls} == {
        values["migration"],
        values["writer"],
        values["reader"],
    }
    rendered_sql = "\n".join(connection.statements)
    assert all(value not in rendered_sql for value in values.values())
    assert "CREATE ROLE maestro_audit_writer" in rendered_sql
    assert connection.transaction_state.exception_type is None
    assert connection.closed is True


def test_bootstrap_failure_remains_inside_transaction_and_is_safely_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = _configure_role_files(monkeypatch, tmp_path)
    connection = _SyncConnection()

    def fail_on_password(user: bytes, password: bytes) -> None:
        del user, password
        raise admin_module.AuditAdministrationError(values["writer"])

    monkeypatch.setattr(connection.pgconn, "change_password", fail_on_password)

    def fake_connection_connect(**_kwargs: object) -> admin_module.Connection[tuple[object, ...]]:
        return cast(admin_module.Connection[tuple[object, ...]], connection)

    monkeypatch.setattr(
        admin_module.Connection,
        "connect",
        fake_connection_connect,
    )

    assert admin_module.main(["bootstrap"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Audit administration operation failed\n"
    assert values["writer"] not in captured.err
    assert connection.transaction_state.exception_type is admin_module.AuditAdministrationError
    assert connection.closed is True


@pytest.mark.asyncio
async def test_migrate_applies_only_pending_ordered_resources_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_role_files(monkeypatch, tmp_path)
    connection = _AsyncConnection()
    versions: Iterator[int] = iter((1, 3))
    applied: list[str] = []

    async def fake_connect(*_args: object, **_kwargs: object) -> _AsyncConnection:
        return connection

    async def fake_version(_connection: object) -> int:
        return next(versions)

    async def fake_execute(_connection: object, resource: str) -> None:
        applied.append(resource)

    monkeypatch.setattr(admin_module, "_connect", fake_connect)
    monkeypatch.setattr(admin_module, "_schema_version", fake_version)
    monkeypatch.setattr(admin_module, "_execute_resource", fake_execute)

    await admin_module.migrate_schema()

    assert applied == [migration.sql for migration in packaged_migrations()[1:]]
    assert connection.closed is True


class _Column:
    def __init__(self, name: str) -> None:
        self.name = name


class _AsyncCursor:
    description = (_Column("audit_id"), _Column("started_at"), _Column("is_incomplete"))

    async def fetchall(self) -> list[tuple[object, ...]]:
        return [(UUID(int=1), datetime(2026, 1, 1, tzinfo=UTC), False)]


class _AsyncConnection:
    def __init__(self) -> None:
        self.parameters: tuple[object, ...] | None = None
        self.closed = False

    async def execute(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> _AsyncCursor:
        assert "audit_read.execution_summary" in statement
        self.parameters = parameters
        return _AsyncCursor()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_curated_reader_uses_fixed_view_bound_filters_and_json_safe_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_role_files(monkeypatch, tmp_path)
    connection = _AsyncConnection()

    async def fake_connect(*_args: object, **_kwargs: object) -> _AsyncConnection:
        return connection

    monkeypatch.setattr(admin_module, "_connect", fake_connect)
    audit_id = UUID(int=1)

    rows = await admin_module.read_curated_view(
        view=admin_module.ReadView.SUMMARY,
        audit_id=audit_id,
        execution_id=None,
        repository_id="a" * 16,
        outcome="incomplete",
    )

    assert rows == (
        {
            "audit_id": str(audit_id),
            "started_at": "2026-01-01T00:00:00+00:00",
            "is_incomplete": False,
        },
    )
    assert connection.parameters == (
        audit_id,
        audit_id,
        None,
        None,
        "a" * 16,
        "a" * 16,
        "incomplete",
        "incomplete",
    )
    assert connection.closed is True


@pytest.mark.asyncio
async def test_outcome_filter_is_rejected_for_non_summary_view() -> None:
    with pytest.raises(admin_module.AuditAdministrationError):
        await admin_module.read_curated_view(
            view=admin_module.ReadView.TIMELINE,
            audit_id=None,
            execution_id=None,
            repository_id=None,
            outcome="resolved",
        )
