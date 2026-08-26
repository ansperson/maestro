"""Packaged, explicitly applied Audit schema migrations."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True, slots=True)
class AuditMigration:
    version: int
    name: str
    sql: str


def packaged_migrations() -> tuple[AuditMigration, ...]:
    """Load ordered SQL resources; normal application startup never calls this."""

    resources = (
        (1, files(__package__).joinpath("0001_audit_tracer.sql")),
        (2, files(__package__).joinpath("0002_execution_failed.sql")),
    )
    return tuple(
        AuditMigration(
            version=version, name=resource.name, sql=resource.read_text(encoding="utf-8")
        )
        for version, resource in resources
    )


__all__ = ["AuditMigration", "packaged_migrations"]
