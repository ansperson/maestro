"""Project a cleared outcome onto the capability-neutral Audit boundary.

Audit does not import the authority model, and the authority model does not construct Audit
payloads. This module is the one place that knows both, mirroring how the capability owns its
own Audit mapping.
"""

from __future__ import annotations

from maestro.audit.recorder import AuditAppliedDecisionInput
from maestro.authority.engine import AuthorityOutcome, AuthorityOutcomeKind


def applied_decision_from_outcome(
    outcome: AuthorityOutcome,
    *,
    work_item: str,
) -> AuditAppliedDecisionInput:
    """Capture the applied entry's content as it stood at the moment it was applied."""

    applied = outcome.applied
    if outcome.kind is not AuthorityOutcomeKind.CLEARED or applied is None:
        raise ValueError("only a cleared outcome applies a decision")
    return AuditAppliedDecisionInput(
        source_kind=applied.kind.value,
        subject=applied.subject,
        choice=applied.choice,
        scope=applied.scope.describe(),
        validity=applied.validity.describe(),
        approved_by=applied.approved_by,
        rationale=applied.rationale,
        origin=applied.origin,
        work_item=work_item,
        source_digest=applied.content_digest(),
    )
