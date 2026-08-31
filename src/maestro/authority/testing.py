"""Typed deterministic work-item adapter for application tests.

It follows `FakeAuditPort`: everything above the port is exercised with no tracker, no
network, and no credential, so the deterministic gate covers the whole authority model.
"""

from __future__ import annotations

from maestro.authority.block import parse_decision_block
from maestro.authority.contracts import (
    AuthoritySource,
    WorkItem,
    WorkItemReference,
)
from maestro.authority.port import (
    ApprovalRequest,
    WorkItemAccessError,
    WorkItemFailureKind,
)


class FakeWorkItemPort:
    """Serve work items from memory and capture the approval requests recorded against them."""

    def __init__(
        self,
        items: dict[str, tuple[AuthoritySource, ...]] | None = None,
        *,
        read_failure: WorkItemFailureKind | None = None,
        write_failure: WorkItemFailureKind | None = None,
    ) -> None:
        self._items = dict(items or {})
        self._read_failure = read_failure
        self._write_failure = write_failure
        self.reads: list[WorkItemReference] = []
        self.requests: list[tuple[WorkItemReference, ApprovalRequest]] = []

    @classmethod
    def from_bodies(cls, bodies: dict[str, str]) -> FakeWorkItemPort:
        """Build a fake from raw work-item bodies, exercising the same parser an adapter uses."""

        return cls(
            {
                reference: parse_decision_block(body, origin=f"work item {reference}")
                for reference, body in bodies.items()
            }
        )

    def set_decisions(self, reference: str, decisions: tuple[AuthoritySource, ...]) -> None:
        """Approve a decision the way a human would, by adding it to the item's block."""

        self._items[reference] = decisions

    async def read_work_item(self, reference: WorkItemReference) -> WorkItem:
        self.reads.append(reference)
        if self._read_failure is not None:
            raise WorkItemAccessError(self._read_failure)
        decisions = self._items.get(reference.value)
        if decisions is None:
            raise WorkItemAccessError(WorkItemFailureKind.NOT_FOUND)
        return WorkItem(reference=reference, decisions=decisions)

    async def record_approval_request(
        self,
        reference: WorkItemReference,
        request: ApprovalRequest,
    ) -> None:
        if self._write_failure is not None:
            raise WorkItemAccessError(self._write_failure)
        self.requests.append((reference, request))
