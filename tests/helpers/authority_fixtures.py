"""Shared constructors for authority tests.

Every helper here is deterministic and needs no tracker, no network, and no credential.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from maestro.authority.contracts import (
    ActionTarget,
    AuthoritySource,
    AuthoritySourceKind,
    DecisionScope,
    DecisionScopeKind,
    DecisionValidity,
    ProposedAction,
    ValidityKind,
)
from maestro.authority.documents import AuthorityDocument
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint

PROJECT = "maestro"
WORK_ITEM = "26"
SUBJECT = "audit.persistence_backend"
CHOICE = "postgresql"
TODAY = date(2026, 8, 31)


def target(*, project: str = PROJECT, work_item: str = WORK_ITEM) -> ActionTarget:
    return ActionTarget(project=project, work_item=work_item)


def action(
    *,
    subject: str = SUBJECT,
    choice: str = CHOICE,
    project: str = PROJECT,
    work_item: str = WORK_ITEM,
) -> ProposedAction:
    return ProposedAction(
        subject=subject,
        choice=choice,
        target=target(project=project, work_item=work_item),
    )


def decision(
    *,
    subject: str = SUBJECT,
    choice: str = CHOICE,
    scope_kind: DecisionScopeKind = DecisionScopeKind.WORK_ITEM,
    scope_target: str = WORK_ITEM,
    validity: DecisionValidity | None = None,
    approved_by: str = "an-operator",
    rationale: str | None = None,
    origin: str = "work item 26",
    superseded: bool = False,
) -> AuthoritySource:
    return AuthoritySource(
        kind=AuthoritySourceKind.DECISION,
        subject=subject,
        choice=choice,
        scope=DecisionScope(kind=scope_kind, target=scope_target),
        validity=validity or DecisionValidity(kind=ValidityKind.UNTIL_SUPERSEDED),
        approved_by=approved_by,
        rationale=rationale,
        origin=origin,
        superseded=superseded,
    )


def rule(
    *,
    subject: str = SUBJECT,
    choice: str = CHOICE,
    scope_kind: DecisionScopeKind = DecisionScopeKind.PROJECT,
    scope_target: str = PROJECT,
    validity: DecisionValidity | None = None,
    origin: str = "rules.md",
) -> AuthoritySource:
    return AuthoritySource(
        kind=AuthoritySourceKind.RULE,
        subject=subject,
        choice=choice,
        scope=DecisionScope(kind=scope_kind, target=scope_target),
        validity=validity or DecisionValidity(kind=ValidityKind.UNTIL_SUPERSEDED),
        origin=origin,
    )


def rules_document(*sources: AuthoritySource, origin: str = "rules.md") -> AuthorityDocument:
    """Wrap written rules the way an accepted, marked document confers them."""

    return AuthorityDocument(origin=origin, status="accepted", sources=sources)


def until(day: date) -> DecisionValidity:
    return DecisionValidity(kind=ValidityKind.UNTIL_DATE, until=day)


def authorized_repository(root: Path) -> AuthorizedRepository:
    return AuthorizedRepository(root=root, repository_id="0123456789abcdef")


def fingerprint() -> RepositoryFingerprint:
    return RepositoryFingerprint(
        digest="a" * 64,
        repository_id="0123456789abcdef",
        git_top_level_id=None,
        head=None,
        dirty_digest=None,
        files={},
        truncated=False,
    )


def decision_block(*entries: str) -> str:
    """Wrap rendered entries in the marked block a work item carries."""

    return "\n".join(
        [
            "Context that is not authority, including a note that the code already does this.",
            "",
            "<!-- maestro:decisions:begin -->",
            *entries,
            "<!-- maestro:decisions:end -->",
            "",
            "More context below the block.",
        ]
    )


def decision_entry(
    *,
    subject: str = SUBJECT,
    decided: str = CHOICE,
    scope: str = f"work-item {WORK_ITEM}",
    validity: str = "until superseded",
    approved_by: str | None = "an-operator",
    extra: tuple[str, ...] = (),
) -> str:
    lines = [
        f"### Decision: {subject}",
        f"- Decided: {decided}",
        f"- Scope: {scope}",
        f"- Validity: {validity}",
    ]
    if approved_by is not None:
        lines.append(f"- Approved-by: {approved_by}")
    lines.extend(extra)
    return "\n".join(lines)
