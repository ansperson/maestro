"""Map validated resolve-codebase-fact results into the Audit boundary."""

from __future__ import annotations

from maestro.audit import (
    AuditConflictInput,
    AuditEvidenceInput,
    AuditInvestigationCompletionInput,
)
from maestro.audit.contracts import AuditConfidence, AuditResultStatus
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Conflict,
    Evidence,
    VerificationResult,
)


def map_result_to_audit_completion(
    result: VerificationResult,
) -> AuditInvestigationCompletionInput:
    """Translate one validated Capability result into Audit-owned semantic input."""

    return AuditInvestigationCompletionInput(
        status=AuditResultStatus(result.status.value),
        answer=result.answer,
        confidence=AuditConfidence(result.confidence.value),
        rationale=result.reason,
        evidence=tuple(_map_evidence(item) for item in result.evidence),
        conflicts=tuple(_map_conflict(item) for item in result.conflicts),
    )


def _map_evidence(value: Evidence) -> AuditEvidenceInput:
    return AuditEvidenceInput(
        path=value.path,
        line_start=value.line_start,
        line_end=value.line_end,
        symbol=value.symbol,
        finding=value.finding,
    )


def _map_conflict(value: Conflict) -> AuditConflictInput:
    return AuditConflictInput(
        description=value.description,
        evidence=tuple(_map_evidence(item) for item in value.evidence),
    )
