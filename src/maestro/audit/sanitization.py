"""Deterministic, bounded redaction for durable Audit text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
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
_BACKSLASH_UNC_PATH = re.compile(
    rf"(?<![\\A-Za-z0-9])\\\\{_UNC_HOST}\\{_UNC_SHARE}"
    rf"(?:\\{_UNC_TAIL_SEGMENT})*"
)
_FORWARD_SLASH_UNC_PATH = re.compile(
    rf"(?<![:/A-Za-z0-9])//{_UNC_HOST}/{_UNC_SHARE}"
    rf"(?:/{_UNC_TAIL_SEGMENT})*"
)
_DISALLOWED_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_ORDINARY_UNC_TERMINATORS = frozenset(" \t\r\n.,;:!")
_BALANCED_UNC_DELIMITERS = {")": "(", "]": "[", "}": "{", '"': '"', "'": "'"}


class _Priority(IntEnum):
    """Security precedence when independently detected spans overlap."""

    CONTROL = 10
    PATH = 20
    REPOSITORY = 30
    SECRET = 40
    CREDENTIAL_URI = 50


@dataclass(frozen=True, slots=True)
class _RedactionSpan:
    start: int
    end: int
    replacement: str
    priority: _Priority


def sanitize_audit_text(value: str, repository_root: Path) -> str:
    """Redact governed data from one original value without expanding it."""

    spans = [
        *_credential_uri_spans(value),
        *_secret_spans(value),
        *_repository_spans(value, repository_root),
        *_path_spans(value),
        *_control_spans(value),
    ]
    sanitized = _apply_spans_once(value, _merge_overlapping_spans(spans, value))
    stripped = sanitized.strip()
    return stripped or _bounded_token(value, "[REDACTED]")


def _credential_uri_spans(value: str) -> list[_RedactionSpan]:
    return [
        _RedactionSpan(
            start=match.start(),
            end=match.end(),
            replacement=_bounded_token(match.group(0), f"{match.group('scheme')}*@"),
            priority=_Priority.CREDENTIAL_URI,
        )
        for match in _CREDENTIAL_URI_USERINFO.finditer(value)
    ]


def _secret_spans(value: str) -> list[_RedactionSpan]:
    spans: list[_RedactionSpan] = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(value):
            preferred = f"{match.group(1)}=*" if match.lastindex else "[REDACTED]"
            spans.append(
                _RedactionSpan(
                    start=match.start(),
                    end=match.end(),
                    replacement=_bounded_token(match.group(0), preferred),
                    priority=_Priority.SECRET,
                )
            )
    return spans


def _repository_spans(value: str, repository_root: Path) -> list[_RedactionSpan]:
    root = str(repository_root)
    if not root or _is_filesystem_anchor(repository_root):
        return []
    replacement = _bounded_token(root, "<repository>")
    spans: list[_RedactionSpan] = []
    start = 0
    while (index := value.find(root, start)) >= 0:
        end = index + len(root)
        spans.append(_RedactionSpan(index, end, replacement, _Priority.REPOSITORY))
        start = end
    return spans


def _path_spans(value: str) -> list[_RedactionSpan]:
    spans: list[_RedactionSpan] = []
    for pattern in (_DRIVE_ABSOLUTE_PATH, _PRIVATE_POSIX_PATH):
        spans.extend(_regex_path_spans(value, pattern))
    for pattern in (_BACKSLASH_UNC_PATH, _FORWARD_SLASH_UNC_PATH):
        spans.extend(
            _path_span(match)
            for match in pattern.finditer(value)
            if _has_unc_boundary(value, match.start(), match.end())
        )
    return spans


def _regex_path_spans(value: str, pattern: re.Pattern[str]) -> list[_RedactionSpan]:
    return [_path_span(match) for match in pattern.finditer(value)]


def _path_span(match: re.Match[str]) -> _RedactionSpan:
    return _RedactionSpan(
        start=match.start(),
        end=match.end(),
        replacement=_bounded_token(match.group(0), "<path>"),
        priority=_Priority.PATH,
    )


def _has_unc_boundary(value: str, start: int, end: int) -> bool:
    if end == len(value):
        return True
    following = value[end]
    if following in _ORDINARY_UNC_TERMINATORS:
        return True
    opener = _BALANCED_UNC_DELIMITERS.get(following)
    return opener is not None and start > 0 and value[start - 1] == opener


def _control_spans(value: str) -> list[_RedactionSpan]:
    return [
        _RedactionSpan(
            start=match.start(),
            end=match.end(),
            replacement=" " * len(match.group(0)),
            priority=_Priority.CONTROL,
        )
        for match in _DISALLOWED_CONTROLS.finditer(value)
    ]


def _merge_overlapping_spans(spans: list[_RedactionSpan], original: str) -> list[_RedactionSpan]:
    if not spans:
        return []
    ordered = sorted(spans, key=lambda span: (span.start, span.end, -span.priority))
    merged: list[_RedactionSpan] = []
    group: list[_RedactionSpan] = [ordered[0]]
    group_end = ordered[0].end
    for span in ordered[1:]:
        if span.start < group_end:
            group.append(span)
            group_end = max(group_end, span.end)
            continue
        merged.append(_merge_span_group(group, original))
        group = [span]
        group_end = span.end
    merged.append(_merge_span_group(group, original))
    return merged


def _merge_span_group(group: list[_RedactionSpan], original: str) -> _RedactionSpan:
    start = min(span.start for span in group)
    end = max(span.end for span in group)
    winner = max(
        group,
        key=lambda span: (
            span.priority,
            span.end - span.start,
            -span.start,
            span.replacement,
        ),
    )
    return _RedactionSpan(
        start=start,
        end=end,
        replacement=_bounded_token(original[start:end], winner.replacement),
        priority=winner.priority,
    )


def _apply_spans_once(value: str, spans: list[_RedactionSpan]) -> str:
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.extend((value[cursor : span.start], span.replacement))
        cursor = span.end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _is_filesystem_anchor(path: Path) -> bool:
    return bool(path.anchor) and path == Path(path.anchor)


def _bounded_token(matched: str, preferred: str) -> str:
    """Return a visible replacement that never exceeds the matched text."""

    return preferred if len(preferred) <= len(matched) else "*"
