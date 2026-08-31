"""Persistence boundary for the current audited Capability lifecycle."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from maestro.audit.contracts import (
    AuditAuthorityApplicationV1,
    AuditExecutionFailureV1,
    AuditExecutionStartV1,
    AuditInvestigationCompletionV1,
)


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
    """Persist the concrete idempotent v1 lifecycle; this is not a generic append API.

    Retried calls receive the same immutable record. An implementation may accept an existing
    identity only after exact record verification and must reject mismatched identity reuse.
    """

    async def start_execution(self, record: AuditExecutionStartV1) -> None:
        """Atomically persist an execution and its sequence-one start event."""
        ...

    async def apply_authority(self, record: AuditAuthorityApplicationV1) -> None:
        """Persist one applied decision against an execution already started."""
        ...

    async def complete_investigation(self, record: AuditInvestigationCompletionV1) -> None:
        """Persist the single sequence-two semantic completion event."""
        ...

    async def fail_execution(self, record: AuditExecutionFailureV1) -> None:
        """Persist the single sequence-two safe operational failure event."""
        ...

    def abort_execution_failure(self, event_id: UUID) -> None:
        """Synchronously abort the active write for one stable failure-event identity.

        Implementations must make the matching operation quiesce when it is subsequently
        cancelled. Calls for an operation that is not active, including repeated calls, are
        harmless.
        """
        ...
