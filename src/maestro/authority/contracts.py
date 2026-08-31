"""Strict typed contracts for decisions, written rules, and proposed actions.

These are contracts only. Matching lives in the engine, tracker mechanics live behind
`WorkItemPort`, and parsing lives in `block`. Nothing here performs I/O.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MAX_AUTHORITY_SUBJECT_CHARS = 200
MAX_AUTHORITY_CHOICE_CHARS = 500
MAX_AUTHORITY_TARGET_CHARS = 200
MAX_AUTHORITY_APPROVER_CHARS = 200
MAX_AUTHORITY_RATIONALE_CHARS = 2_000
MAX_AUTHORITY_SOURCE_CHARS = 500
MAX_AUTHORITY_SOURCES = 100
MAX_WORK_ITEM_REFERENCE_CHARS = 200

_Subject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUTHORITY_SUBJECT_CHARS),
]
_Choice = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUTHORITY_CHOICE_CHARS),
]
_Target = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUTHORITY_TARGET_CHARS),
]
_Approver = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUTHORITY_APPROVER_CHARS),
]
_Rationale = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_AUTHORITY_RATIONALE_CHARS
    ),
]
_SourceLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AUTHORITY_SOURCE_CHARS),
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def normalize_authority_key(value: str) -> str:
    """Fold a subject, choice, or target to the one form matching compares.

    Matching is exact on this normalized form. Case and surrounding whitespace do not
    distinguish two keys, but nothing weaker matches: a prefix, a substring, or a near
    spelling is a different key, because silent widening is the failure ADR-0006 exists to
    prevent.
    """

    return " ".join(value.split()).casefold()


class AuthoritySourceKind(StrEnum):
    """Where an entry in force came from.

    Both kinds carry identical matching semantics. The distinction is provenance, which a
    human needs when a request or a conflict is put in front of them.
    """

    DECISION = "decision"
    RULE = "rule"


class DecisionScopeKind(StrEnum):
    """What a decision or rule applies to (ADR-0006, *How precisely scope is represented*)."""

    WORK_ITEM = "work_item"
    PROJECT = "project"


class ValidityKind(StrEnum):
    """How long an entry stays in force. Declared validity is how authority expires."""

    UNTIL_SUPERSEDED = "until_superseded"
    UNTIL_DATE = "until_date"


class DecisionScope(_StrictFrozenModel):
    """The target an entry applies to. Reuse requires an explicit match."""

    kind: DecisionScopeKind
    target: _Target

    def covers(self, action_target: ActionTarget) -> bool:
        """Report whether this scope reaches the target the action is proposed against."""

        if self.kind is DecisionScopeKind.WORK_ITEM:
            return normalize_authority_key(self.target) == normalize_authority_key(
                action_target.work_item
            )
        return normalize_authority_key(self.target) == normalize_authority_key(
            action_target.project
        )

    def describe(self) -> str:
        """Render the scope for a human reading an approval request."""

        label = "work item" if self.kind is DecisionScopeKind.WORK_ITEM else "project"
        return f"{label} {self.target}"


class DecisionValidity(_StrictFrozenModel):
    """How long an entry holds. `until_date` is inclusive of the named day."""

    kind: ValidityKind
    until: date | None = None

    @model_validator(mode="after")
    def validate_validity(self) -> Self:
        if self.kind is ValidityKind.UNTIL_DATE and self.until is None:
            raise ValueError("a dated validity must name the date it holds until")
        if self.kind is ValidityKind.UNTIL_SUPERSEDED and self.until is not None:
            raise ValueError("an until-superseded validity cannot also name a date")
        return self

    def holds_on(self, evaluated_on: date) -> bool:
        """Report whether the entry is still in force on the supplied day."""

        if self.kind is ValidityKind.UNTIL_SUPERSEDED:
            return True
        # `until` is present whenever the kind is dated; the validator enforces it.
        return self.until is not None and evaluated_on <= self.until

    def describe(self) -> str:
        """Render the validity for a human reading an approval request."""

        if self.kind is ValidityKind.UNTIL_SUPERSEDED:
            return "until superseded"
        return f"until {self.until.isoformat()}" if self.until is not None else "until superseded"


class AuthoritySource(_StrictFrozenModel):
    """One entry in force: a decision from a work item or document, or a written rule.

    A decision names its approver because ADR-0006 requires the Trail to answer who
    authorized an outcome. A rule has no approver: writing the rule is the delegation, and
    the document it lives in is its provenance.
    """

    kind: AuthoritySourceKind
    subject: _Subject
    choice: _Choice
    scope: DecisionScope
    validity: DecisionValidity
    approved_by: _Approver | None = None
    rationale: _Rationale | None = None
    origin: _SourceLabel
    superseded: bool = False

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.kind is AuthoritySourceKind.DECISION and self.approved_by is None:
            raise ValueError("a decision must name who approved it")
        if self.kind is AuthoritySourceKind.RULE and self.approved_by is not None:
            raise ValueError("a written rule is delegation, not an approval, and names no approver")
        return self

    def in_force_on(self, evaluated_on: date) -> bool:
        """Report whether this entry is live: not superseded and within its validity."""

        return not self.superseded and self.validity.holds_on(evaluated_on)

    def matches_subject(self, subject: str) -> bool:
        """Report whether this entry speaks to the named subject at all."""

        return normalize_authority_key(self.subject) == normalize_authority_key(subject)

    def decides(self, choice: str) -> bool:
        """Report whether the entry's decided choice is the one being proposed."""

        return normalize_authority_key(self.choice) == normalize_authority_key(choice)

    def content_digest(self) -> str:
        """Hash the entry's content so a later edit to its source is detectable.

        The Trail captures applied content rather than a reference, because a work item is
        editable by design and a reference alone would let an edit change what the Trail
        says was authorized.
        """

        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def describe(self) -> str:
        """Render the entry for a human reading an approval request or a conflict."""

        approver = f", approved by {self.approved_by}" if self.approved_by is not None else ""
        return (
            f"{self.kind.value} '{self.subject}' = '{self.choice}' "
            f"({self.scope.describe()}, {self.validity.describe()}{approver}) "
            f"from {self.origin}"
        )


class ActionTarget(_StrictFrozenModel):
    """Where an action is being taken: one work item inside one project.

    Both are always present, so a project-scoped and a work-item-scoped entry can each be
    tested against the same action without the caller restating its context.
    """

    project: _Target
    work_item: _Target


class ProposedAction(_StrictFrozenModel):
    """The action a working agent proposes to take.

    An action names the subject it would settle and the choice it would apply. It carries no
    authority classification of its own: an agent that could declare its own action routine
    would be deciding whether its decision is checked, which is the asymmetry ADR-0006
    forbids. Every action requires a covering entry, and nothing the caller says lowers that.
    """

    subject: _Subject
    choice: _Choice
    target: ActionTarget

    def describe(self) -> str:
        """Render the action for a human reading an approval request."""

        return f"'{self.subject}' = '{self.choice}'"


class WorkItemReference(_StrictFrozenModel):
    """A tracker-neutral work item identity.

    The value is opaque to the domain. Only an adapter knows what shape its tracker's
    identifiers take, which is what keeps the tracker replaceable per ADR-0004.
    """

    value: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=MAX_WORK_ITEM_REFERENCE_CHARS
        ),
    ]


class WorkItem(_StrictFrozenModel):
    """A work item as authority: its identity and the decisions in its block.

    Everything outside the decision block is context and never reaches this contract.
    """

    reference: WorkItemReference
    decisions: tuple[AuthoritySource, ...] = Field(max_length=MAX_AUTHORITY_SOURCES)

    @model_validator(mode="after")
    def validate_decision_kinds(self) -> Self:
        if any(entry.kind is not AuthoritySourceKind.DECISION for entry in self.decisions):
            raise ValueError("a work item decision block contains decisions only")
        return self


class MalformedDecisionBlockError(ValueError):
    """A decision block could not be parsed, so none of it is accepted.

    A block is accepted whole or rejected whole. Partial acceptance would let a malformed
    entry silently drop while the entries around it kept their authority.
    """
