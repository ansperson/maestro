"""Read documents that an operator has designated as authority.

A document counts as authority only when it says so. It must declare a current status, and
it must mark its decisions or rules. A document that declares no status, or that marks
nothing, is read as context.

This closes a specific gap. ADR-0006 forbids inferring authority from source code while
accepting a requirements artifact as authoritative. Without an explicit marker, an
observation about the code can be written into an artifact, accepted, and acquire authority
the rule denies it.

Repository content is untrusted, so documents are never discovered by scanning. Only paths an
operator configured explicitly are read, and each is canonicalized and bounded first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from maestro.authority.block import (
    MAX_AUTHORITY_BLOCK_CHARS,
    parse_decision_block,
    parse_rule_block,
)
from maestro.authority.contracts import AuthoritySource, MalformedDecisionBlockError

MAX_AUTHORITY_DOCUMENT_BYTES = 1_048_576

# The ADRs in this repository already carry this field, so the convention has prior art.
_STATUS_LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?\**\s*status\s*\**\s*:\s*\**\s*(?P<status>[A-Za-z][A-Za-z -]*?)\s*\**\s*$",
    re.IGNORECASE,
)
_STATUS_MARKER = re.compile(
    r"<!--\s*maestro:status\s*:\s*(?P<status>[A-Za-z][A-Za-z -]*?)\s*-->", re.IGNORECASE
)

# An allowlist, not a denylist. An unrecognized status confers nothing, so a new status word
# fails towards asking a human rather than towards silent authority.
AUTHORITATIVE_STATUSES = frozenset({"accepted"})


@dataclass(frozen=True, slots=True)
class AuthorityDocument:
    """One configured document, its declared status, and what it confers.

    `sources` is empty whenever the status is absent or not current, so a document that has
    not been accepted cannot contribute authority even if it marks decisions.
    """

    origin: str
    status: str | None
    sources: tuple[AuthoritySource, ...]

    @property
    def is_authoritative(self) -> bool:
        """Report whether the declared status is one that confers authority."""

        return self.status is not None and self.status in AUTHORITATIVE_STATUSES


class AuthorityDocumentError(ValueError):
    """A configured authority document could not be read."""


def read_authority_documents(paths: tuple[Path, ...]) -> tuple[AuthorityDocument, ...]:
    """Read every explicitly configured authority document, in the configured order."""

    return tuple(read_authority_document(path) for path in paths)


def read_authority_document(path: Path) -> AuthorityDocument:
    """Read one configured document's status and its marked decisions and rules."""

    canonical = _canonical_document(path)
    origin = path.name
    body = _read_bounded_text(canonical)
    status = _declared_status(body)
    document = AuthorityDocument(origin=origin, status=status, sources=())
    if not document.is_authoritative:
        # A non-current document is inert, so its blocks are not even parsed. Reporting a
        # malformed block in a document that confers nothing would be noise, not safety.
        return document
    return AuthorityDocument(
        origin=origin,
        status=status,
        sources=(
            *parse_decision_block(body, origin=origin),
            *parse_rule_block(body, origin=origin),
        ),
    )


def authoritative_sources(documents: tuple[AuthorityDocument, ...]) -> tuple[AuthoritySource, ...]:
    """Flatten what the configured documents confer into entries for the engine."""

    return tuple(source for document in documents for source in document.sources)


def _canonical_document(path: Path) -> Path:
    if path.is_symlink():
        raise AuthorityDocumentError("an authority document must be a regular non-symlink file")
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AuthorityDocumentError("an authority document does not exist") from exc
    if not canonical.is_file():
        raise AuthorityDocumentError("an authority document must be a regular file")
    return canonical


def _read_bounded_text(canonical: Path) -> str:
    try:
        if canonical.stat().st_size > MAX_AUTHORITY_DOCUMENT_BYTES:
            raise AuthorityDocumentError("an authority document exceeds its size limit")
        body = canonical.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthorityDocumentError("an authority document could not be read") from exc
    except UnicodeDecodeError as exc:
        raise AuthorityDocumentError("an authority document must contain UTF-8 text") from exc
    if len(body) > MAX_AUTHORITY_BLOCK_CHARS:
        raise AuthorityDocumentError("an authority document exceeds its size limit")
    return body


def _declared_status(body: str) -> str | None:
    """Find the document's declared status, preferring an explicit Maestro marker.

    Only the first declaration counts. A document that declares a status twice is ambiguous
    about what it is, and resolving that ambiguity is not the reader's to do.
    """

    marker = _STATUS_MARKER.search(body)
    if marker is not None:
        return _normalize_status(marker.group("status"))
    for line in body.splitlines():
        match = _STATUS_LINE.match(line)
        if match is not None:
            return _normalize_status(match.group("status"))
    return None


def _normalize_status(value: str) -> str:
    return " ".join(value.split()).casefold()


__all__ = [
    "AUTHORITATIVE_STATUSES",
    "MAX_AUTHORITY_DOCUMENT_BYTES",
    "AuthorityDocument",
    "AuthorityDocumentError",
    "MalformedDecisionBlockError",
    "authoritative_sources",
    "read_authority_document",
    "read_authority_documents",
]
