"""Application service that answers one question: may this action proceed?

The working agent carries none of the authority model. It asks here and gets an answer, so
it holds no escalation policy of its own.

This is not the Unblocker ADR-0006 describes. The Unblocker pauses a run and resumes it after
approval, which needs durable Jobs (ADR-0008). Until those exist the flow completes by
re-running after approval, which still exercises the port, the engine, the block, and Audit.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from maestro.audit import AuditExecutionHandle, AuditFailureStage, AuditRecorder
from maestro.authority.audit_mapping import applied_decision_from_outcome
from maestro.authority.contracts import (
    AuthoritySource,
    MalformedDecisionBlockError,
    ProposedAction,
    WorkItemReference,
)
from maestro.authority.documents import AuthorityDocument, authoritative_sources
from maestro.authority.engine import AuthorityOutcome, AuthorityOutcomeKind, evaluate_authority
from maestro.authority.port import ApprovalRequest, WorkItemAccessError, WorkItemPort
from maestro.errors import (
    AuthorityConflictError,
    AuthorityRequiredError,
    ErrorCode,
    WorkItemUnavailableError,
)
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

_LOGGER = logging.getLogger("maestro.authority")


class AuthorityService:
    """Gather the entries in force, ask the engine, and act on its one answer."""

    def __init__(
        self,
        port: WorkItemPort,
        audit: AuditRecorder,
        *,
        documents: tuple[AuthorityDocument, ...] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._port = port
        self._audit = audit
        self._documents = documents
        self._clock = clock or (lambda: datetime.now(UTC))

    async def authorize(
        self,
        action: ProposedAction,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint,
    ) -> AuthorityOutcome:
        """Clear the action, or refuse it and record the request where a human will see it.

        Returns the outcome only when the action is cleared. A refusal and a conflict raise,
        because a caller that had to inspect a returned outcome to learn it may not proceed
        is a caller that can forget to.
        """

        handle = await self._audit.start_authority_check(
            repository,
            fingerprint,
            action.describe(),
        )
        reference = WorkItemReference(value=action.target.work_item)
        sources = await self._gather_sources(handle, reference)
        outcome = evaluate_authority(action, sources, evaluated_on=self._clock().date())
        if outcome.kind is AuthorityOutcomeKind.CLEARED:
            await self._audit.record_authority_applied(
                handle,
                repository,
                applied_decision_from_outcome(outcome, work_item=reference.value),
            )
            self._log(action, outcome)
            return outcome
        await self._record_request(handle, reference, outcome)
        self._log(action, outcome)
        raise self._refusal(outcome)

    async def _gather_sources(
        self,
        handle: AuditExecutionHandle,
        reference: WorkItemReference,
    ) -> tuple[AuthoritySource, ...]:
        """Read the work item's block and add what the configured documents confer.

        A tracker that cannot be read fails closed. Treating an unreadable item as an item
        without decisions would turn an outage into unrestricted autonomy.
        """

        try:
            work_item = await self._port.read_work_item(reference)
        except (WorkItemAccessError, MalformedDecisionBlockError) as exc:
            await self._fail(handle, ErrorCode.WORK_ITEM_UNAVAILABLE)
            raise WorkItemUnavailableError from exc
        return (*work_item.decisions, *authoritative_sources(self._documents))

    async def _record_request(
        self,
        handle: AuditExecutionHandle,
        reference: WorkItemReference,
        outcome: AuthorityOutcome,
    ) -> None:
        request = ApprovalRequest.from_outcome(outcome, requested_at=self._clock())
        try:
            await self._port.record_approval_request(reference, request)
        except WorkItemAccessError as exc:
            # The refusal stands either way, but it must not be reported as a recorded
            # request when nothing was written for a human to answer.
            await self._fail(handle, ErrorCode.WORK_ITEM_UNAVAILABLE)
            raise WorkItemUnavailableError from exc
        await self._fail(handle, self._refusal(outcome).code)

    async def _fail(self, handle: AuditExecutionHandle, error_code: ErrorCode) -> None:
        await self._audit.record_execution_failed(handle, error_code, AuditFailureStage.AUTHORITY)

    @staticmethod
    def _refusal(outcome: AuthorityOutcome) -> AuthorityRequiredError | AuthorityConflictError:
        if outcome.kind is AuthorityOutcomeKind.CONFLICT:
            return AuthorityConflictError()
        return AuthorityRequiredError()

    @staticmethod
    def _log(action: ProposedAction, outcome: AuthorityOutcome) -> None:
        _LOGGER.info(
            "authority evaluated",
            extra={
                "metadata": {
                    "capability": "decision_authority",
                    "subject": action.subject,
                    "work_item": action.target.work_item,
                    "outcome": outcome.kind.value,
                    "reason": outcome.reason.value if outcome.reason is not None else None,
                    "considered_count": len(outcome.considered),
                }
            },
        )
