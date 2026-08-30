"""Deterministic valid results at the Audit completion byte boundary."""

from __future__ import annotations

from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Conflict,
    Evidence,
    VerificationResult,
    VerificationStatus,
)

_BASE_CONFLICT_FINDING_CHARS = 893
_BOUNDARY_FIRST_FINDING_CHARS = 915


def audit_payload_boundary_result(*, overflow: bool) -> VerificationResult:
    """Return a public result whose fake-runtime Audit payload is exactly at/over its limit."""

    def evidence(finding_chars: int) -> Evidence:
        return Evidence(
            path="src/models.py",
            line_start=1,
            line_end=1,
            symbol="S" * 256,
            finding="f" * finding_chars,
        )

    conflict_findings = [_BASE_CONFLICT_FINDING_CHARS] * 20
    conflict_findings[0] = _BOUNDARY_FIRST_FINDING_CHARS + int(overflow)
    conflicts = [
        Conflict(
            description="d" * 1_000,
            evidence=[evidence(size) for size in conflict_findings[offset : offset + 10]],
        )
        for offset in (0, 10)
    ]
    return VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="a" * 8_000,
        confidence=Confidence.HIGH,
        evidence=[evidence(1_000) for _ in range(20)],
        conflicts=conflicts,
        reason="r" * 4_000,
    )
