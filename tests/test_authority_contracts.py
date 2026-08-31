"""Coverage of the invariants the authority contracts refuse to bend.

These are the negative paths: a decision without an approver, a rule with one, a validity that
contradicts itself, and a request built from an outcome that has nothing to request.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from helpers.authority_fixtures import (
    PROJECT,
    SUBJECT,
    WORK_ITEM,
    action,
    decision,
    rule,
    target,
)
from pydantic import ValidationError

from maestro.authority.audit_mapping import applied_decision_from_outcome
from maestro.authority.contracts import (
    AuthoritySource,
    AuthoritySourceKind,
    DecisionScope,
    DecisionScopeKind,
    DecisionValidity,
    ValidityKind,
    WorkItem,
    WorkItemReference,
    normalize_authority_key,
)
from maestro.authority.documents import AuthorityDocumentError, read_authority_document
from maestro.authority.engine import (
    AuthorityOutcome,
    AuthorityOutcomeKind,
    evaluate_authority,
)
from maestro.authority.port import ApprovalRequest, WorkItemAccessError, WorkItemFailureKind
from maestro.authority.testing import FakeWorkItemPort


def test_a_decision_without_an_approver_is_rejected() -> None:
    with pytest.raises(ValidationError, match="who approved"):
        AuthoritySource(
            kind=AuthoritySourceKind.DECISION,
            subject=SUBJECT,
            choice="postgresql",
            scope=DecisionScope(kind=DecisionScopeKind.PROJECT, target=PROJECT),
            validity=DecisionValidity(kind=ValidityKind.UNTIL_SUPERSEDED),
            origin="work item 26",
        )


def test_a_rule_that_names_an_approver_is_rejected() -> None:
    """Writing a rule is the delegation, not an approval of one case."""

    with pytest.raises(ValidationError, match="delegation"):
        AuthoritySource(
            kind=AuthoritySourceKind.RULE,
            subject=SUBJECT,
            choice="postgresql",
            scope=DecisionScope(kind=DecisionScopeKind.PROJECT, target=PROJECT),
            validity=DecisionValidity(kind=ValidityKind.UNTIL_SUPERSEDED),
            approved_by="an-operator",
            origin="rules.md",
        )


def test_a_dated_validity_without_a_date_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must name the date"):
        DecisionValidity(kind=ValidityKind.UNTIL_DATE)


def test_an_until_superseded_validity_cannot_also_carry_a_date() -> None:
    with pytest.raises(ValidationError, match="cannot also name a date"):
        DecisionValidity(kind=ValidityKind.UNTIL_SUPERSEDED, until=date(2027, 1, 1))


def test_a_work_item_block_holds_decisions_only() -> None:
    """Written rules come from documents, so a rule in an item's block is a contract error."""

    with pytest.raises(ValidationError, match="decisions only"):
        WorkItem(reference=WorkItemReference(value=WORK_ITEM), decisions=(rule(),))


def test_a_dated_validity_renders_its_date() -> None:
    validity = DecisionValidity(kind=ValidityKind.UNTIL_DATE, until=date(2027, 1, 1))

    assert validity.describe() == "until 2027-01-01"


def test_a_cleared_outcome_renders_the_action_it_cleared() -> None:
    outcome = evaluate_authority(action(), [decision()], evaluated_on=date(2026, 8, 31))

    assert outcome.summary.startswith("Cleared")
    assert SUBJECT in outcome.summary


def test_a_cleared_outcome_has_nothing_to_request_approval_for() -> None:
    outcome = AuthorityOutcome(kind=AuthorityOutcomeKind.CLEARED, action=action())

    with pytest.raises(ValueError, match="nothing to request"):
        ApprovalRequest.from_outcome(outcome, requested_at=datetime.now(UTC))


def test_only_a_cleared_outcome_applies_a_decision() -> None:
    outcome = AuthorityOutcome(kind=AuthorityOutcomeKind.CONFLICT, action=action())

    with pytest.raises(ValueError, match="only a cleared outcome"):
        applied_decision_from_outcome(outcome, work_item=WORK_ITEM)


def test_the_applied_digest_changes_when_the_entry_changes() -> None:
    original = decision(choice="postgresql")
    edited = decision(choice="sqlite")

    assert original.content_digest() != edited.content_digest()
    assert original.content_digest() == decision(choice="postgresql").content_digest()


def test_normalization_folds_case_and_spacing_and_nothing_else() -> None:
    assert normalize_authority_key("  Audit   Backend ") == "audit backend"
    assert normalize_authority_key("audit-backend") != normalize_authority_key("audit backend")


def test_a_scope_describes_itself_for_a_human() -> None:
    work_item_scope = DecisionScope(kind=DecisionScopeKind.WORK_ITEM, target="26")
    project_scope = DecisionScope(kind=DecisionScopeKind.PROJECT, target="maestro")

    assert work_item_scope.describe() == "work item 26"
    assert project_scope.describe() == "project maestro"


def test_a_rule_describes_itself_without_naming_an_approver() -> None:
    rendered = rule().describe()

    assert "rule" in rendered
    assert "approved by" not in rendered
    assert "approved by an-operator" in decision().describe()


def test_a_scope_only_covers_its_own_kind_of_target() -> None:
    project_scope = DecisionScope(kind=DecisionScopeKind.PROJECT, target=WORK_ITEM)

    assert not project_scope.covers(target())


@pytest.mark.asyncio
async def test_an_unknown_work_item_is_reported_as_not_found() -> None:
    port = FakeWorkItemPort({})

    with pytest.raises(WorkItemAccessError) as raised:
        await port.read_work_item(WorkItemReference(value="999"))

    assert raised.value.kind is WorkItemFailureKind.NOT_FOUND


def test_an_unreadable_document_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    path = tmp_path / "unreadable.md"
    path.write_text("* **Status:** Accepted\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(AuthorityDocumentError, match="could not be read"):
            read_authority_document(path)
    finally:
        path.chmod(0o600)


def test_a_document_larger_than_the_byte_limit_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "huge.md"
    path.write_text("* **Status:** Accepted\n" + "x" * 2_000_000, encoding="utf-8")

    with pytest.raises(AuthorityDocumentError, match="size limit"):
        read_authority_document(path)


def test_a_scope_target_that_is_only_whitespace_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DecisionScope(kind=DecisionScopeKind.PROJECT, target="   ")
