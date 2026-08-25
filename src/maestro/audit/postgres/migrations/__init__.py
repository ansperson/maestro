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

    resource = files(__package__).joinpath("0001_audit_tracer.sql")
    return (
        AuditMigration(
            version=1,
            name=resource.name,
            sql=resource.read_text(encoding="utf-8"),
        ),
    )


__all__ = ["AuditMigration", "packaged_migrations"]
