"""Bounded redaction for untrusted model output."""

from __future__ import annotations

import re
from pathlib import Path

from maestro.capabilities.resolve_codebase_fact.contracts import (
    Conflict,
    Evidence,
    VerificationResult,
)

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*(['\"]?)[^\s,'\"]{6,}\2"
    ),
)
_ABSOLUTE_PATHS = re.compile(
    r"(?:(?<=\s)|^)(?:/(?:Users|home|private|tmp|var)/[^\s,;]+|[A-Za-z]:\\[^\s,;]+)"
)


def sanitize_result(result: VerificationResult, repository_root: Path) -> VerificationResult:
    """Redact secrets and host paths from every public free-text field."""

    evidence = [_sanitize_evidence(item, repository_root) for item in result.evidence]
    conflicts = [
        Conflict(
            description=_sanitize_text(conflict.description, repository_root),
            evidence=[_sanitize_evidence(item, repository_root) for item in conflict.evidence],
        )
        for conflict in result.conflicts
    ]
    return VerificationResult(
        status=result.status,
        answer=(
            _sanitize_text(result.answer, repository_root) if result.answer is not None else None
        ),
        confidence=result.confidence,
        evidence=evidence,
        conflicts=conflicts,
        reason=_sanitize_text(result.reason, repository_root),
    )


def _sanitize_evidence(evidence: Evidence, repository_root: Path) -> Evidence:
    return Evidence(
        path=evidence.path,
        line_start=evidence.line_start,
        line_end=evidence.line_end,
        symbol=_sanitize_text(evidence.symbol, repository_root)
        if evidence.symbol is not None
        else None,
        finding=_sanitize_text(evidence.finding, repository_root),
    )


def _sanitize_text(value: str, repository_root: Path) -> str:
    sanitized = value.replace(str(repository_root), "<repository>")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(_replace_secret, sanitized)
    return _ABSOLUTE_PATHS.sub("<absolute-path>", sanitized)


def _replace_secret(match: re.Match[str]) -> str:
    if match.lastindex:
        return f"{match.group(1)}=[REDACTED]"
    return "[REDACTED]"
