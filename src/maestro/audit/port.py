"""Persistence boundary for the current audited Capability lifecycle."""

from __future__ import annotations

from typing import Protocol

from maestro.audit.contracts import AuditExecutionStartV1, AuditInvestigationCompletionV1


class AuditPort(Protocol):
    """Persist the two successful-tracer transitions; this is not a generic append API."""

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        """Atomically persist an execution and its sequence-one start event."""
        ...

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        """Persist the single sequence-two semantic completion event."""
        ...
