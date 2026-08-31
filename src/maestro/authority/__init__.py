"""Decision authority: what Maestro may decide, and what belongs to a human."""

from maestro.authority.contracts import (
    ActionTarget,
    AuthoritySource,
    AuthoritySourceKind,
    DecisionScope,
    DecisionScopeKind,
    DecisionValidity,
    MalformedDecisionBlockError,
    ProposedAction,
    ValidityKind,
    WorkItem,
    WorkItemReference,
)
from maestro.authority.engine import (
    ApprovalReason,
    AuthorityOutcome,
    AuthorityOutcomeKind,
    evaluate_authority,
)
from maestro.authority.port import (
    ApprovalRequest,
    WorkItemAccessError,
    WorkItemFailureKind,
    WorkItemPort,
)
from maestro.authority.service import AuthorityService

__all__ = [
    "ActionTarget",
    "ApprovalReason",
    "ApprovalRequest",
    "AuthorityOutcome",
    "AuthorityOutcomeKind",
    "AuthorityService",
    "AuthoritySource",
    "AuthoritySourceKind",
    "DecisionScope",
    "DecisionScopeKind",
    "DecisionValidity",
    "MalformedDecisionBlockError",
    "ProposedAction",
    "ValidityKind",
    "WorkItem",
    "WorkItemAccessError",
    "WorkItemFailureKind",
    "WorkItemPort",
    "WorkItemReference",
    "evaluate_authority",
]
