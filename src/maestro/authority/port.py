"""The boundary through which Maestro reads work items and records approval requests.

ADR-0004 names `WorkItemPort` but leaves its interface open. This is that interface. Tracker
mechanics never reach the domain, so a second tracker is an addition behind this protocol
rather than surgery on the engine or the contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from maestro.authority.block import DECISION_BLOCK_BEGIN, DECISION_BLOCK_END
from maestro.authority.contracts import (
    AuthoritySource,
    ProposedAction,
    WorkItem,
    WorkItemReference,
)
from maestro.authority.engine import ApprovalReason, AuthorityOutcome, AuthorityOutcomeKind


class WorkItemFailureKind(StrEnum):
    """Dispositions a work-item adapter may report, with no adapter detail attached."""

    UNREACHABLE = "unreachable"
    UNAUTHENTICATED = "unauthenticated"
    NOT_FOUND = "not_found"
    MALFORMED = "malformed"


class WorkItemAccessError(Exception):
    """Safe port failure carrying a disposition and no tracker or credential detail.

    An adapter that cannot answer must raise this. It must never return an empty work item,
    because an empty item is indistinguishable from one that states no decisions, and the
    caller would proceed as if authority had been checked when it had not.
    """

    def __init__(self, kind: WorkItemFailureKind) -> None:
        self.kind = kind
        super().__init__("The work-management system could not be read or written.")


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What Maestro records on a work item when it will not proceed.

    It carries everything a human needs to answer from the tracker alone: the action, why it
    was not cleared, the entries that produced that outcome, and the exact block entry that
    would settle it.
    """

    action: ProposedAction
    outcome_kind: AuthorityOutcomeKind
    summary: str
    reason: ApprovalReason | None
    considered: tuple[AuthoritySource, ...]
    requested_at: datetime

    @classmethod
    def from_outcome(cls, outcome: AuthorityOutcome, *, requested_at: datetime) -> ApprovalRequest:
        """Build the request that corresponds to a refused or conflicting outcome."""

        if outcome.kind is AuthorityOutcomeKind.CLEARED:
            raise ValueError("a cleared action has nothing to request approval for")
        return cls(
            action=outcome.action,
            outcome_kind=outcome.kind,
            summary=outcome.summary,
            reason=outcome.reason,
            considered=outcome.considered,
            requested_at=requested_at,
        )

    def render(self) -> str:
        """Render the request as the text a human reads and answers in the tracker."""

        heading = (
            "## Maestro: authoritative sources conflict"
            if self.outcome_kind is AuthorityOutcomeKind.CONFLICT
            else "## Maestro: approval required"
        )
        lines = [
            heading,
            "",
            f"- **Subject:** {self.action.subject}",
            f"- **Proposed:** {self.action.choice}",
            f"- **Project:** {self.action.target.project}",
            f"- **Work item:** {self.action.target.work_item}",
            f"- **Requested at:** {self.requested_at.isoformat()}",
            "",
            self.summary,
        ]
        lines.extend(self._considered_section())
        lines.extend(self._answer_section())
        return "\n".join(lines)

    def _considered_section(self) -> list[str]:
        if not self.considered:
            return []
        label = (
            "### Conflicting sources"
            if self.outcome_kind is AuthorityOutcomeKind.CONFLICT
            else "### Related entries"
        )
        return ["", label, "", *(f"- {entry.describe()}" for entry in self.considered)]

    def _answer_section(self) -> list[str]:
        if self.outcome_kind is AuthorityOutcomeKind.CONFLICT:
            return [
                "",
                "### How to answer",
                "",
                "Maestro does not choose between authoritative sources. Resolve the",
                "disagreement at its source — supersede one entry, or correct its scope —",
                "and run again.",
            ]
        return [
            "",
            "### How to answer",
            "",
            "Add this entry to the decision block of this work item, editing the decided",
            "value if a different choice is correct, then run again.",
            "",
            "```markdown",
            DECISION_BLOCK_BEGIN,
            f"### Decision: {self.action.subject}",
            f"- Decided: {self.action.choice}",
            f"- Scope: work-item {self.action.target.work_item}",
            "- Validity: until superseded",
            "- Approved-by: <your tracker identity>",
            DECISION_BLOCK_END,
            "```",
        ]


class WorkItemPort(Protocol):
    """Read a work item as authority and record an approval request against it.

    The port deliberately exposes no decision lifecycle. Requesting, proposing, approving,
    rejecting, and superseding are coordination, and ADR-0004 gives coordination to Work
    Management: the human performs them in the tracker, and Maestro reads the result.
    """

    async def read_work_item(self, reference: WorkItemReference) -> WorkItem:
        """Read one work item's decision block as typed contracts.

        Raises `WorkItemAccessError` when the item cannot be read or its block cannot be
        parsed. An unreadable item is never reported as an item without decisions.
        """
        ...

    async def record_approval_request(
        self,
        reference: WorkItemReference,
        request: ApprovalRequest,
    ) -> None:
        """Record an approval request where a human will read and answer it."""
        ...
