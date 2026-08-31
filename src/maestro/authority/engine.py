"""The deterministic authority engine.

`evaluate_authority` is a pure function. It performs no I/O, makes no model call, and reads
no clock or environment: the day it evaluates against is supplied by the caller. The same
inputs always produce the same outcome, which is what makes the rules testable.

The determinism is the point rather than an optimization. ADR-0006 settles that an engine
which judged would move the judgement rather than remove it, so all of the matching, validity
checking, and conflict detection lives behind this one interface and none of it asks a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from maestro.authority.contracts import (
    AuthoritySource,
    AuthoritySourceKind,
    ProposedAction,
    normalize_authority_key,
)


class AuthorityOutcomeKind(StrEnum):
    """The three answers the engine can give. There is no fourth."""

    CLEARED = "cleared"
    APPROVAL_REQUIRED = "approval_required"
    CONFLICT = "conflict"


class ApprovalReason(StrEnum):
    """Why an action was not cleared, in terms a human can act on.

    An operator who is told only that approval is required cannot tell a decision they never
    made from one they made for a different case, so the two are named apart.
    """

    NO_COVERING_SOURCE = "no_covering_source"
    OUT_OF_SCOPE = "out_of_scope"
    VALIDITY_LAPSED = "validity_lapsed"
    DIFFERENT_CHOICE_DECIDED = "different_choice_decided"


_REASON_SUMMARIES = {
    ApprovalReason.NO_COVERING_SOURCE: "No decision or rule speaks to this subject.",
    ApprovalReason.OUT_OF_SCOPE: (
        "A decision or rule settles this subject, but its scope does not reach this action."
    ),
    ApprovalReason.VALIDITY_LAPSED: (
        "A decision or rule settled this subject, but it is superseded or its validity lapsed."
    ),
    ApprovalReason.DIFFERENT_CHOICE_DECIDED: (
        "A decision or rule in force settles this subject differently."
    ),
}


@dataclass(frozen=True, slots=True)
class AuthorityOutcome:
    """What the engine concluded, plus the entries a human would need to see.

    `applied` is present exactly when the action was cleared, and `reason` exactly when
    approval is required. `considered` carries the entries that produced the outcome: the
    conflicting sources for a conflict, and the near misses for a refusal.
    """

    kind: AuthorityOutcomeKind
    action: ProposedAction
    applied: AuthoritySource | None = None
    reason: ApprovalReason | None = None
    considered: tuple[AuthoritySource, ...] = ()

    @property
    def summary(self) -> str:
        """Render one line stating the outcome, for a request comment or an operator."""

        if self.kind is AuthorityOutcomeKind.CLEARED:
            return f"Cleared {self.action.describe()}."
        if self.kind is AuthorityOutcomeKind.CONFLICT:
            return (
                f"Authoritative sources disagree about {self.action.describe()}. "
                "Maestro does not choose between them."
            )
        return _REASON_SUMMARIES[self.reason] if self.reason is not None else ""


def evaluate_authority(
    action: ProposedAction,
    sources: Sequence[AuthoritySource],
    *,
    evaluated_on: date,
) -> AuthorityOutcome:
    """Decide whether an action is cleared, needs approval, or hits a conflict.

    `sources` are the decisions and written rules already gathered from the work item and the
    configured authority documents. Gathering them is I/O and belongs to the caller; judging
    them is what this function refuses to do.
    """

    speaking = tuple(entry for entry in sources if entry.matches_subject(action.subject))
    live = tuple(entry for entry in speaking if entry.in_force_on(evaluated_on))
    covering = tuple(entry for entry in live if entry.scope.covers(action.target))
    if covering:
        return _outcome_from_covering(action, covering)
    return _refusal_without_cover(action, speaking, evaluated_on)


def _outcome_from_covering(
    action: ProposedAction,
    covering: tuple[AuthoritySource, ...],
) -> AuthorityOutcome:
    settled = {normalize_authority_key(entry.choice) for entry in covering}
    if len(settled) > 1:
        # Two sources in force answer the same action differently. Precedence is deliberately
        # not applied: ADR-0006 shows a case where neither older-wins nor newer-wins gives the
        # right answer, because the disagreement was a design error only a human could see.
        return AuthorityOutcome(
            kind=AuthorityOutcomeKind.CONFLICT,
            action=action,
            considered=_ordered(covering),
        )
    if not any(entry.decides(action.choice) for entry in covering):
        return AuthorityOutcome(
            kind=AuthorityOutcomeKind.APPROVAL_REQUIRED,
            action=action,
            reason=ApprovalReason.DIFFERENT_CHOICE_DECIDED,
            considered=_ordered(covering),
        )
    return AuthorityOutcome(
        kind=AuthorityOutcomeKind.CLEARED,
        action=action,
        applied=_ordered(covering)[0],
        considered=_ordered(covering),
    )


def _refusal_without_cover(
    action: ProposedAction,
    speaking: tuple[AuthoritySource, ...],
    evaluated_on: date,
) -> AuthorityOutcome:
    # The two buckets are exhaustive over everything that speaks to the subject: an entry
    # that is both lapsed and out of scope still falls in one, so an entry can never be
    # silently dropped and reported as "nothing speaks to this subject".
    lapsed = tuple(
        entry
        for entry in speaking
        if not entry.in_force_on(evaluated_on) and entry.scope.covers(action.target)
    )
    out_of_scope = tuple(entry for entry in speaking if not entry.scope.covers(action.target))
    if lapsed:
        reason, considered = ApprovalReason.VALIDITY_LAPSED, lapsed
    elif out_of_scope:
        reason, considered = ApprovalReason.OUT_OF_SCOPE, out_of_scope
    else:
        reason, considered = ApprovalReason.NO_COVERING_SOURCE, ()
    return AuthorityOutcome(
        kind=AuthorityOutcomeKind.APPROVAL_REQUIRED,
        action=action,
        reason=reason,
        considered=_ordered(considered),
    )


def _ordered(entries: Iterable[AuthoritySource]) -> tuple[AuthoritySource, ...]:
    """Order entries stably so the same inputs always render the same output.

    A human decision sorts ahead of a written rule, so a cleared action records the explicit
    approval rather than the delegation when both agree. Beyond that the order is arbitrary
    but fixed, because a Trail that reordered between runs would be harder to compare.
    """

    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry.kind is AuthoritySourceKind.DECISION else 1,
                entry.origin,
                normalize_authority_key(entry.subject),
                normalize_authority_key(entry.choice),
                entry.scope.kind.value,
                normalize_authority_key(entry.scope.target),
            ),
        )
    )
