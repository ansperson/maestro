"""Persistence boundary for the current audited Capability lifecycle."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from maestro.audit.contracts import AuditExecutionStartV1, AuditInvestigationCompletionV1


class AuditWriteFailureKind(StrEnum):
    """Persistence dispositions established by an Audit adapter."""

    RETRYABLE_NOT_COMMITTED = "retryable_not_committed"
    PERMANENT = "permanent"
    AMBIGUOUS = "ambiguous"


class AuditWriteError(Exception):
    """Safe port failure that contains no adapter or database detail."""

    def __init__(self, kind: AuditWriteFailureKind) -> None:
        self.kind = kind
        super().__init__("Audit persistence did not establish durable state.")


class AuditPort(Protocol):
    """Persist the two successful-tracer transitions; this is not a generic append API."""

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        """Atomically persist an execution and its sequence-one start event."""
        ...

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        """Persist the single sequence-two semantic completion event."""
        ...
