"""Coverage of the GitHub work-item adapter against recorded responses.

Nothing here touches the network: every response is served by an in-process transport, and
the token is a synthetic value from a temporary owner-only file.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from helpers.authority_fixtures import (
    WORK_ITEM,
    action,
    decision_block,
    decision_entry,
)
from pydantic import ValidationError

from maestro.authority.contracts import WorkItemReference
from maestro.authority.engine import (
    ApprovalReason,
    AuthorityOutcome,
    AuthorityOutcomeKind,
)
from maestro.authority.github import GitHubWorkItemPort
from maestro.authority.port import ApprovalRequest, WorkItemAccessError, WorkItemFailureKind
from maestro.config import MAX_WORK_ITEM_RESPONSE_BYTES, GitHubWorkItemSettings

pytestmark = pytest.mark.asyncio

TOKEN = "synthetic-work-item-token"  # noqa: S105 - a fixture value, not a credential
REPOSITORY = "ansperson/maestro"
REFERENCE = WorkItemReference(value=WORK_ITEM)

type Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "github-token"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o600)
    return path


def build(token_file: Path, handler: Handler, **overrides: object) -> GitHubWorkItemPort:
    settings = GitHubWorkItemSettings(
        repository=REPOSITORY,
        token_file=token_file,
        **overrides,  # pyright: ignore[reportArgumentType] - keyword passthrough
    )
    return GitHubWorkItemPort(
        settings.work_item_configuration(),
        transport=httpx.MockTransport(handler),
    )


def issue_response(body: str | None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPOSITORY}/issues/{WORK_ITEM}"
        return httpx.Response(200, json={"number": int(WORK_ITEM), "body": body})

    return handler


def approval_request() -> ApprovalRequest:
    outcome = AuthorityOutcome(
        kind=AuthorityOutcomeKind.APPROVAL_REQUIRED,
        action=action(),
        reason=ApprovalReason.NO_COVERING_SOURCE,
    )
    return ApprovalRequest.from_outcome(outcome, requested_at=datetime(2026, 8, 31, tzinfo=UTC))


async def test_a_decision_block_in_a_real_issue_becomes_the_same_typed_contracts(
    token_file: Path,
) -> None:
    port = build(token_file, issue_response(decision_block(decision_entry())))

    work_item = await port.read_work_item(REFERENCE)

    assert len(work_item.decisions) == 1
    assert work_item.decisions[0].choice == "postgresql"
    assert work_item.decisions[0].origin == "work item 26"


async def test_an_issue_with_no_block_carries_no_decisions(token_file: Path) -> None:
    port = build(token_file, issue_response("Just a description."))

    assert (await port.read_work_item(REFERENCE)).decisions == ()


async def test_an_issue_with_an_empty_body_carries_no_decisions(token_file: Path) -> None:
    port = build(token_file, issue_response(None))

    assert (await port.read_work_item(REFERENCE)).decisions == ()


async def test_the_request_carries_the_token_and_the_pinned_api_version(
    token_file: Path,
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"body": ""})

    await build(token_file, handler).read_work_item(REFERENCE)

    assert seen["authorization"] == f"Bearer {TOKEN}"
    assert seen["x-github-api-version"] == "2022-11-28"
    assert seen["accept"] == "application/vnd.github+json"


async def test_an_approval_request_is_written_back_as_a_comment(token_file: Path) -> None:
    posted: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["path"] = request.url.path
        posted["method"] = request.method
        posted["body"] = json.loads(request.content)["body"]
        return httpx.Response(201, json={"id": 1})

    await build(token_file, handler).record_approval_request(REFERENCE, approval_request())

    assert posted["method"] == "POST"
    assert posted["path"] == f"/repos/{REPOSITORY}/issues/{WORK_ITEM}/comments"
    assert "approval required" in str(posted["body"])
    assert "<!-- maestro:decisions:begin -->" in str(posted["body"])


async def test_approving_on_the_issue_and_re_running_clears_the_refused_action(
    token_file: Path,
) -> None:
    """The end-to-end path this slice makes real, driven by the issue body alone."""

    bodies = iter(["No decisions yet.", decision_block(decision_entry())])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(200, json={"body": next(bodies)})

    port = build(token_file, handler)

    assert (await port.read_work_item(REFERENCE)).decisions == ()
    await port.record_approval_request(REFERENCE, approval_request())
    assert len((await port.read_work_item(REFERENCE)).decisions) == 1


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (404, WorkItemFailureKind.NOT_FOUND),
        (401, WorkItemFailureKind.UNAUTHENTICATED),
        (403, WorkItemFailureKind.UNAUTHENTICATED),
        (500, WorkItemFailureKind.UNREACHABLE),
        (502, WorkItemFailureKind.UNREACHABLE),
        (301, WorkItemFailureKind.UNREACHABLE),
    ],
)
async def test_an_unsuccessful_status_fails_closed(
    token_file: Path,
    status: int,
    kind: WorkItemFailureKind,
) -> None:
    port = build(token_file, lambda _request: httpx.Response(status, json={}))

    with pytest.raises(WorkItemAccessError) as raised:
        await port.read_work_item(REFERENCE)

    assert raised.value.kind is kind


async def test_an_unreachable_tracker_fails_closed(token_file: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(WorkItemAccessError) as raised:
        await build(token_file, handler).read_work_item(REFERENCE)

    assert raised.value.kind is WorkItemFailureKind.UNREACHABLE


async def test_a_failure_never_reports_an_item_without_decisions(token_file: Path) -> None:
    """The dangerous failure: an outage read as unrestricted autonomy."""

    port = build(token_file, lambda _request: httpx.Response(503, json={}))

    with pytest.raises(WorkItemAccessError):
        await port.read_work_item(REFERENCE)


async def test_a_failure_message_leaks_no_tracker_or_credential_detail(token_file: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {request.url}", request=request)

    with pytest.raises(WorkItemAccessError) as raised:
        await build(token_file, handler).read_work_item(REFERENCE)

    rendered = str(raised.value)
    assert TOKEN not in rendered
    assert REPOSITORY not in rendered
    assert "api.github.com" not in rendered


async def test_a_malformed_block_in_a_real_issue_is_refused(token_file: Path) -> None:
    port = build(token_file, issue_response(decision_block(decision_entry(validity="soon"))))

    with pytest.raises(WorkItemAccessError) as raised:
        await port.read_work_item(REFERENCE)

    assert raised.value.kind is WorkItemFailureKind.MALFORMED


async def test_a_response_that_is_not_an_issue_is_refused(token_file: Path) -> None:
    port = build(token_file, lambda _request: httpx.Response(200, content=b"not json"))

    with pytest.raises(WorkItemAccessError) as raised:
        await port.read_work_item(REFERENCE)

    assert raised.value.kind is WorkItemFailureKind.MALFORMED


async def test_an_oversized_response_is_refused(token_file: Path) -> None:
    oversized = httpx.Response(200, json={"body": "x" * 2_000_000})
    port = build(token_file, lambda _request: oversized)

    with pytest.raises(WorkItemAccessError) as raised:
        await port.read_work_item(REFERENCE)

    assert raised.value.kind is WorkItemFailureKind.MALFORMED


async def test_an_endless_body_stops_being_read_at_the_bound(token_file: Path) -> None:
    """The bound must stop the read, not measure it once the whole body is already in memory."""

    chunk_size = 65_536
    served = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal served
        while True:
            served += 1
            yield b"x" * chunk_size

    port = build(token_file, lambda _request: httpx.Response(200, content=endless()))

    with pytest.raises(WorkItemAccessError) as raised:
        await port.read_work_item(REFERENCE)

    assert raised.value.kind is WorkItemFailureKind.MALFORMED
    # The stream is abandoned just past the bound rather than run to exhaustion.
    assert served <= (MAX_WORK_ITEM_RESPONSE_BYTES // chunk_size) + 2


@pytest.mark.parametrize(
    "reference",
    ["0", "-1", "26/../27", "abc", "", "1e3", "26#comment"],
)
async def test_a_reference_that_is_not_an_issue_number_never_reaches_the_api(
    token_file: Path,
    reference: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an invalid reference reached the tracker")

    port = build(token_file, handler)
    try:
        candidate = WorkItemReference(value=reference)
    except ValidationError:
        return
    with pytest.raises(WorkItemAccessError):
        await port.read_work_item(candidate)
