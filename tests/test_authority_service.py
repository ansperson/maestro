"""Coverage of the whole authority loop against the fake tracker.

Nothing here needs a tracker, a network, or a credential, so the deterministic gate covers
the refusal path, the conflict path, the fail-closed paths, and the Trail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from helpers.authority_fixtures import (
    CHOICE,
    SUBJECT,
    TODAY,
    WORK_ITEM,
    action,
    authorized_repository,
    decision,
    decision_block,
    decision_entry,
    fingerprint,
    rule,
    rules_document,
    until,
)

from maestro.audit.contracts import AuditCapability, AuditEventType, AuthorityAppliedV1
from maestro.audit.testing import FakeAuditPort, fake_audit_recorder
from maestro.authority.contracts import WorkItemReference
from maestro.authority.documents import AuthorityDocument
from maestro.authority.engine import ApprovalReason, AuthorityOutcomeKind
from maestro.authority.port import WorkItemFailureKind
from maestro.authority.service import AuthorityService
from maestro.authority.testing import FakeWorkItemPort
from maestro.errors import (
    AuthorityConflictError,
    AuthorityRequiredError,
    ErrorCode,
    WorkItemUnavailableError,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def build(
    port: FakeWorkItemPort,
    audit_port: FakeAuditPort | None = None,
    *,
    documents: tuple[AuthorityDocument, ...] = (),
) -> tuple[AuthorityService, FakeAuditPort]:
    audit = audit_port or FakeAuditPort()
    service = AuthorityService(
        port,
        fake_audit_recorder(audit),
        documents=documents,
        clock=lambda: NOW,
    )
    return service, audit


async def authorize(service: AuthorityService, tmp_path: Path, **overrides: object):
    return await service.authorize(
        action(**overrides),  # pyright: ignore[reportArgumentType] - keyword passthrough
        authorized_repository(tmp_path),
        fingerprint(),
    )


async def test_a_decision_already_in_the_work_item_clears_the_action(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: (decision(),)})
    service, _ = build(port)

    outcome = await authorize(service, tmp_path)

    assert outcome.kind is AuthorityOutcomeKind.CLEARED
    assert port.reads == [WorkItemReference(value=WORK_ITEM)]
    assert port.requests == []


async def test_the_adapter_reads_the_same_contracts_the_parser_produces(tmp_path: Path) -> None:
    port = FakeWorkItemPort.from_bodies({WORK_ITEM: decision_block(decision_entry())})
    service, _ = build(port)

    assert (await authorize(service, tmp_path)).kind is AuthorityOutcomeKind.CLEARED


async def test_an_uncovered_action_is_refused_and_the_request_is_recorded(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: ()})
    service, _ = build(port)

    with pytest.raises(AuthorityRequiredError) as raised:
        await authorize(service, tmp_path)

    assert raised.value.code is ErrorCode.AUTHORITY_REQUIRED
    ((reference, request),) = port.requests
    assert reference.value == WORK_ITEM
    assert request.reason is ApprovalReason.NO_COVERING_SOURCE
    assert request.requested_at == NOW


async def test_the_recorded_request_lets_a_human_answer_without_reading_the_logs(
    tmp_path: Path,
) -> None:
    port = FakeWorkItemPort({WORK_ITEM: ()})
    service, _ = build(port)

    with pytest.raises(AuthorityRequiredError):
        await authorize(service, tmp_path)

    rendered = port.requests[0][1].render()

    assert SUBJECT in rendered
    assert CHOICE in rendered
    assert "<!-- maestro:decisions:begin -->" in rendered
    assert "How to answer" in rendered


async def test_the_request_distinguishes_no_decision_from_one_that_does_not_reach_here(
    tmp_path: Path,
) -> None:
    absent, _ = build(FakeWorkItemPort({WORK_ITEM: ()}))
    out_of_scope_port = FakeWorkItemPort({WORK_ITEM: (decision(scope_target="99"),)})
    out_of_scope, _ = build(out_of_scope_port)

    with pytest.raises(AuthorityRequiredError):
        await authorize(absent, tmp_path)
    with pytest.raises(AuthorityRequiredError):
        await authorize(out_of_scope, tmp_path)

    assert "Related entries" in out_of_scope_port.requests[0][1].render()


async def test_the_public_error_message_exposes_no_internal_detail(tmp_path: Path) -> None:
    service, _ = build(FakeWorkItemPort({WORK_ITEM: ()}))

    with pytest.raises(AuthorityRequiredError) as raised:
        await authorize(service, tmp_path)

    payload = raised.value.public_json()

    assert "AUTHORITY_REQUIRED" in payload
    assert SUBJECT not in payload
    assert "work item" not in payload


async def test_conflicting_sources_are_refused_and_both_are_recorded(tmp_path: Path) -> None:
    port = FakeWorkItemPort(
        {WORK_ITEM: (decision(choice="postgresql"), decision(choice="sqlite", origin="ADR-0005"))}
    )
    service, _ = build(port)

    with pytest.raises(AuthorityConflictError) as raised:
        await authorize(service, tmp_path)

    assert raised.value.code is ErrorCode.AUTHORITY_CONFLICT
    request = port.requests[0][1]
    assert request.outcome_kind is AuthorityOutcomeKind.CONFLICT
    rendered = request.render()
    assert "postgresql" in rendered
    assert "sqlite" in rendered
    assert "Conflicting sources" in rendered


async def test_a_conflict_offers_no_way_to_pick_a_winner(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: (decision(choice="postgresql"),)})
    service, _ = build(port, documents=(rules_document(rule(choice="sqlite")),))

    with pytest.raises(AuthorityConflictError):
        await authorize(service, tmp_path)

    assert "does not choose" in port.requests[0][1].render()


async def test_approving_on_the_work_item_clears_the_previously_refused_action(
    tmp_path: Path,
) -> None:
    """The end-to-end path: refuse, approve in the tracker, run again, cleared."""

    port = FakeWorkItemPort({WORK_ITEM: ()})
    service, _ = build(port)

    with pytest.raises(AuthorityRequiredError):
        await authorize(service, tmp_path)

    port.set_decisions(WORK_ITEM, (decision(),))

    assert (await authorize(service, tmp_path)).kind is AuthorityOutcomeKind.CLEARED


async def test_an_unreachable_tracker_fails_closed_rather_than_proceeding(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: ()}, read_failure=WorkItemFailureKind.UNREACHABLE)
    service, _ = build(port)

    with pytest.raises(WorkItemUnavailableError):
        await authorize(service, tmp_path)


async def test_an_unauthenticated_tracker_fails_closed(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: ()}, read_failure=WorkItemFailureKind.UNAUTHENTICATED)
    service, _ = build(port)

    with pytest.raises(WorkItemUnavailableError):
        await authorize(service, tmp_path)


async def test_a_request_that_cannot_be_recorded_is_not_reported_as_recorded(
    tmp_path: Path,
) -> None:
    port = FakeWorkItemPort({WORK_ITEM: ()}, write_failure=WorkItemFailureKind.UNREACHABLE)
    service, _ = build(port)

    with pytest.raises(WorkItemUnavailableError):
        await authorize(service, tmp_path)

    assert port.requests == []


async def test_applying_a_decision_records_authority_applied_in_the_trail(tmp_path: Path) -> None:
    applied = decision(rationale="a shared durable store")
    service, audit = build(FakeWorkItemPort({WORK_ITEM: (applied,)}))

    await authorize(service, tmp_path)

    (record,) = audit.applied_authority
    payload = record.event.payload
    assert isinstance(payload, AuthorityAppliedV1)
    assert record.event.event_type is AuditEventType.AUTHORITY_APPLIED
    assert payload.subject == SUBJECT
    assert payload.choice == CHOICE
    assert payload.scope == "work item 26"
    assert payload.validity == "until superseded"
    assert payload.approved_by == "an-operator"
    assert payload.rationale == "a shared durable store"
    assert payload.work_item == WORK_ITEM
    assert payload.source_digest == applied.content_digest()


async def test_an_authority_check_is_its_own_audited_execution(tmp_path: Path) -> None:
    service, audit = build(FakeWorkItemPort({WORK_ITEM: (decision(),)}))

    await authorize(service, tmp_path)

    (start,) = audit.starts
    assert start.execution.capability is AuditCapability.DECISION_AUTHORITY


async def test_an_execution_that_halted_for_missing_authority_is_recorded(tmp_path: Path) -> None:
    service, audit = build(FakeWorkItemPort({WORK_ITEM: ()}))

    with pytest.raises(AuthorityRequiredError):
        await authorize(service, tmp_path)

    (failure,) = audit.failures
    assert failure.event.event_type is AuditEventType.EXECUTION_FAILED
    assert audit.applied_authority == []


async def test_a_conflict_halts_the_execution_with_its_own_error_code(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: (decision(choice="a"), decision(choice="b"))})
    service, audit = build(port)

    with pytest.raises(AuthorityConflictError):
        await authorize(service, tmp_path)

    payload = audit.failures[0].event.payload
    assert getattr(payload, "error_code", None) is ErrorCode.AUTHORITY_CONFLICT


async def test_a_later_edit_to_the_work_item_leaves_the_trail_unchanged(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: (decision(choice=CHOICE),)})
    service, audit = build(port)

    await authorize(service, tmp_path)
    recorded = audit.applied_authority[0].event.payload
    assert isinstance(recorded, AuthorityAppliedV1)
    captured_choice, captured_digest = recorded.choice, recorded.source_digest

    port.set_decisions(WORK_ITEM, (decision(choice="sqlite"),))

    assert recorded.choice == captured_choice
    assert recorded.source_digest == captured_digest
    assert len(audit.applied_authority) == 1


async def test_the_trail_gains_no_decision_lifecycle_events(tmp_path: Path) -> None:
    service, audit = build(FakeWorkItemPort({WORK_ITEM: (decision(),)}))

    await authorize(service, tmp_path)

    recorded = {record.event.event_type for record in (*audit.starts, *audit.applied_authority)}
    assert recorded == {AuditEventType.EXECUTION_STARTED, AuditEventType.AUTHORITY_APPLIED}
    assert audit.completions == []


async def test_a_rule_that_cleared_the_action_is_recorded_without_an_approver(
    tmp_path: Path,
) -> None:
    service, audit = build(FakeWorkItemPort({WORK_ITEM: ()}), documents=(rules_document(rule()),))

    await authorize(service, tmp_path)

    payload = audit.applied_authority[0].event.payload
    assert isinstance(payload, AuthorityAppliedV1)
    assert payload.source_kind == "rule"
    assert payload.approved_by is None


async def test_a_lapsed_decision_halts_rather_than_clearing(tmp_path: Path) -> None:
    port = FakeWorkItemPort({WORK_ITEM: (decision(validity=until(TODAY.replace(year=2020))),)})
    service, audit = build(port)

    with pytest.raises(AuthorityRequiredError):
        await authorize(service, tmp_path)

    assert port.requests[0][1].reason is ApprovalReason.VALIDITY_LAPSED
    assert audit.applied_authority == []
