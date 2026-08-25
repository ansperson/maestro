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
_PATH_CHARACTER = r"[^\s\x00-\x1f<>\"'()\[\]{},;!?]"
_PRIVATE_POSIX_PATH = re.compile(
    rf"(?<![A-Za-z0-9._~-])/(?:Users|home|private|tmp|var|opt|srv|etc|root)"
    rf"(?![A-Za-z0-9._~-])(?:/{_PATH_CHARACTER}*)?"
)
_DRIVE_ABSOLUTE_PATH = re.compile(rf"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]){_PATH_CHARACTER}*")
_UNC_HOST = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_UNC_SHARE = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\$?"
_UNC_TAIL_SEGMENT = r"[A-Za-z0-9._$-]+"
_UNC_TERMINATOR = r"(?=$|[\s.,;:!'\"<>])"
_BACKSLASH_UNC_PATH = re.compile(
    rf"(?<![\\A-Za-z0-9])\\\\{_UNC_HOST}\\{_UNC_SHARE}"
    rf"(?:\\{_UNC_TAIL_SEGMENT})*{_UNC_TERMINATOR}"
)
_FORWARD_SLASH_UNC_PATH = re.compile(
    rf"(?<![:/A-Za-z0-9])//{_UNC_HOST}/{_UNC_SHARE}"
    rf"(?:/{_UNC_TAIL_SEGMENT})*{_UNC_TERMINATOR}"
)
_DISALLOWED_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_audit_text(value: str, repository_root: Path) -> str:
    """Remove secrets, private absolute paths, and unsafe control characters."""

    sanitized = _redact_repository_root(value, repository_root)
    sanitized = _CREDENTIAL_URI_USERINFO.sub(_replace_uri_userinfo, sanitized)
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(_replace_secret, sanitized)
    for pattern in (
        _DRIVE_ABSOLUTE_PATH,
        _BACKSLASH_UNC_PATH,
        _FORWARD_SLASH_UNC_PATH,
        _PRIVATE_POSIX_PATH,
    ):
        sanitized = pattern.sub(_replace_path, sanitized)
    sanitized = _DISALLOWED_CONTROLS.sub(" ", sanitized)
    stripped = sanitized.strip()
    return stripped or _bounded_token(value, "[REDACTED]")


def _replace_secret(match: re.Match[str]) -> str:
    if match.lastindex:
        return _bounded_token(match.group(0), f"{match.group(1)}=*")
    return _bounded_token(match.group(0), "[REDACTED]")


def _replace_uri_userinfo(match: re.Match[str]) -> str:
    return _bounded_token(match.group(0), f"{match.group('scheme')}*@")


def _replace_path(match: re.Match[str]) -> str:
    return _bounded_token(match.group(0), "<path>")


def _redact_repository_root(value: str, repository_root: Path) -> str:
    root = str(repository_root)
    return value.replace(root, _bounded_token(root, "<repository>"))


def _bounded_token(matched: str, preferred: str) -> str:
    """Return a visible replacement that never exceeds the matched text."""

    return preferred if len(preferred) <= len(matched) else "*"
