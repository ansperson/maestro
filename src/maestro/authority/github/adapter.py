"""The first `WorkItemPort` adapter: authority read from a GitHub issue.

This module is the only place in the codebase that knows the GitHub API, mirroring how the
PostgreSQL adapter is the only place that knows SQL. GitHub is the first tracker, not the
assumed one: ADR-0004 requires that no specific tracker becomes the canonical Maestro domain,
so a second adapter is an addition beside this one rather than surgery on the engine.

Everything here fails closed. A tracker that cannot be read raises, and never returns an item
with no decisions, because that is indistinguishable from an item that states none and would
turn an outage into unrestricted autonomy.
"""

from __future__ import annotations

import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from maestro.authority.block import parse_decision_block
from maestro.authority.contracts import (
    MalformedDecisionBlockError,
    WorkItem,
    WorkItemReference,
)
from maestro.authority.port import ApprovalRequest, WorkItemAccessError, WorkItemFailureKind
from maestro.config import MAX_WORK_ITEM_RESPONSE_BYTES, GitHubWorkItemConfiguration

_ISSUE_NUMBER = re.compile(r"[1-9][0-9]{0,17}\Z")
_NOT_FOUND = 404
_UNAUTHENTICATED_STATUSES = frozenset({401, 403})
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


class _IssuePayload(BaseModel):
    """The one field Maestro reads from an issue. Everything else in the response is ignored.

    A tracker response is untrusted input, so it is validated rather than indexed.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    body: str | None = Field(default=None, max_length=MAX_WORK_ITEM_RESPONSE_BYTES)


class GitHubWorkItemPort:
    """Read a decision block from a live issue and write approval requests back as comments."""

    def __init__(
        self,
        configuration: GitHubWorkItemConfiguration,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._configuration = configuration
        self._transport = transport

    async def read_work_item(self, reference: WorkItemReference) -> WorkItem:
        """Fetch one issue and parse the marked decision block from its body."""

        number = _issue_number(reference)
        response = await self._request(
            "GET",
            f"/repos/{self._configuration.repository}/issues/{number}",
        )
        issue = _decode(response)
        try:
            decisions = parse_decision_block(issue.body or "", origin=f"work item {number}")
        except MalformedDecisionBlockError:
            # A malformed block is refused rather than downgraded to "no decisions", so a
            # typo in the block cannot quietly widen what runs unattended.
            raise WorkItemAccessError(WorkItemFailureKind.MALFORMED) from None
        return WorkItem(reference=reference, decisions=decisions)

    async def record_approval_request(
        self,
        reference: WorkItemReference,
        request: ApprovalRequest,
    ) -> None:
        """Post the request as an issue comment, where a human reads and answers it."""

        number = _issue_number(reference)
        await self._request(
            "POST",
            f"/repos/{self._configuration.repository}/issues/{number}/comments",
            json={"body": request.render()},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Perform one bounded authenticated call, mapping every failure to a safe disposition."""

        try:
            async with httpx.AsyncClient(
                base_url=self._configuration.api_url,
                timeout=self._configuration.request_timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=json,
                    headers=self._headers(),
                )
        except httpx.HTTPError:
            # The exception carries adapter and possibly URL detail, so none of it escapes.
            raise WorkItemAccessError(WorkItemFailureKind.UNREACHABLE) from None
        _raise_for_status(response)
        return response

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": _ACCEPT,
            "Authorization": f"Bearer {self._configuration.token.get_secret_value()}",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": "maestro-work-item-port",
        }


def _issue_number(reference: WorkItemReference) -> str:
    """Validate the opaque reference as this tracker's identifier shape.

    Only the adapter knows what shape an identifier takes. Validating here also keeps the
    reference from contributing anything but digits to a request path.
    """

    if _ISSUE_NUMBER.fullmatch(reference.value) is None:
        raise WorkItemAccessError(WorkItemFailureKind.NOT_FOUND)
    return reference.value


def _raise_for_status(response: httpx.Response) -> None:
    """Map every unsuccessful status onto a disposition that carries no tracker detail."""

    if response.status_code == _NOT_FOUND:
        raise WorkItemAccessError(WorkItemFailureKind.NOT_FOUND)
    if response.status_code in _UNAUTHENTICATED_STATUSES:
        raise WorkItemAccessError(WorkItemFailureKind.UNAUTHENTICATED)
    if not response.is_success:
        raise WorkItemAccessError(WorkItemFailureKind.UNREACHABLE)


def _decode(response: httpx.Response) -> _IssuePayload:
    if len(response.content) > MAX_WORK_ITEM_RESPONSE_BYTES:
        raise WorkItemAccessError(WorkItemFailureKind.MALFORMED)
    try:
        return _IssuePayload.model_validate_json(response.content)
    except ValidationError:
        raise WorkItemAccessError(WorkItemFailureKind.MALFORMED) from None
