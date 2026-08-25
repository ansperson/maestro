"""Deterministic, bounded redaction for durable Audit text."""

from __future__ import annotations

import re
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*(['\"]?)[^\s,'\"]{6,}\2"
    ),
)
_CREDENTIAL_URI_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]{0,31}://)[^/@\s]+@")
_ABSOLUTE_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9._~/-])/(?!/)[^\s\x00-\x1f<>\"']+")
_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\x00-\x1f<>\"']+")
_DISALLOWED_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_audit_text(value: str, repository_root: Path) -> str:
    """Remove secrets, private absolute paths, and unsafe control characters."""

    sanitized = value.replace(str(repository_root), "<repository>")
    sanitized = _CREDENTIAL_URI_USERINFO.sub(_replace_uri_userinfo, sanitized)
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(_replace_secret, sanitized)
    sanitized = _ABSOLUTE_WINDOWS_PATH.sub("<absolute-path>", sanitized)
    sanitized = _ABSOLUTE_POSIX_PATH.sub("<absolute-path>", sanitized)
    sanitized = _DISALLOWED_CONTROLS.sub(" ", sanitized)
    return sanitized.strip() or "[REDACTED]"


def _replace_secret(match: re.Match[str]) -> str:
    if match.lastindex:
        return f"{match.group(1)}=[REDACTED]"
    return "[REDACTED]"


def _replace_uri_userinfo(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}[REDACTED]@"
