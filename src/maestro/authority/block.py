"""Parse the marked blocks Maestro reads as authority.

A work item and a document are both untrusted text. Authority comes only from an explicitly
marked block inside them, never from surrounding prose, so that an observation about the code
cannot be written down and acquire authority ADR-0006 denies it.

A block is accepted whole or rejected whole.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import ValidationError

from maestro.authority.contracts import (
    MAX_AUTHORITY_SOURCES,
    AuthoritySource,
    AuthoritySourceKind,
    DecisionScope,
    DecisionScopeKind,
    DecisionValidity,
    MalformedDecisionBlockError,
    ValidityKind,
)

MAX_AUTHORITY_BLOCK_CHARS = 100_000

DECISION_BLOCK_BEGIN = "<!-- maestro:decisions:begin -->"
DECISION_BLOCK_END = "<!-- maestro:decisions:end -->"
RULE_BLOCK_BEGIN = "<!-- maestro:rules:begin -->"
RULE_BLOCK_END = "<!-- maestro:rules:end -->"

_ENTRY_HEADING = re.compile(r"^#{1,6}\s+(?P<kind>Decision|Rule)\s*:\s*(?P<subject>.+?)\s*$")
_FIELD_LINE = re.compile(r"^[-*]\s+(?P<field>[A-Za-z][A-Za-z-]*)\s*:\s*(?P<value>.*?)\s*$")
_SCOPE_VALUE = re.compile(r"^(?P<kind>project|work[- ]item)\s+(?P<target>.+?)$", re.IGNORECASE)
_UNTIL_SUPERSEDED = re.compile(r"^until\s+superseded$", re.IGNORECASE)
_UNTIL_DATE = re.compile(r"^until\s+(?P<until>\d{4}-\d{2}-\d{2})$", re.IGNORECASE)

_REQUIRED_DECISION_FIELDS = frozenset({"decided", "scope", "validity", "approved-by"})
_REQUIRED_RULE_FIELDS = frozenset({"decided", "scope", "validity"})
_OPTIONAL_FIELDS = frozenset({"rationale", "superseded"})
_TRUE_VALUES = frozenset({"yes", "true"})
_FALSE_VALUES = frozenset({"no", "false"})


def parse_decision_block(body: str, *, origin: str) -> tuple[AuthoritySource, ...]:
    """Parse the marked decision block of one work item or document.

    An absent block yields no decisions and is not an error: a work item that states no
    decision simply carries no authority.
    """

    return _parse_marked_block(
        body,
        origin=origin,
        begin=DECISION_BLOCK_BEGIN,
        end=DECISION_BLOCK_END,
        kind=AuthoritySourceKind.DECISION,
    )


def parse_rule_block(body: str, *, origin: str) -> tuple[AuthoritySource, ...]:
    """Parse the marked rule block of one document.

    Writing a rule delegates that class of decision, so a rule block is the delegation
    mechanism and there is no separate one.
    """

    return _parse_marked_block(
        body,
        origin=origin,
        begin=RULE_BLOCK_BEGIN,
        end=RULE_BLOCK_END,
        kind=AuthoritySourceKind.RULE,
    )


def _parse_marked_block(
    body: str,
    *,
    origin: str,
    begin: str,
    end: str,
    kind: AuthoritySourceKind,
) -> tuple[AuthoritySource, ...]:
    if len(body) > MAX_AUTHORITY_BLOCK_CHARS:
        raise MalformedDecisionBlockError("the authority document exceeds its size limit")
    marked = _extract_marked_region(body, begin=begin, end=end)
    if marked is None:
        return ()
    return _parse_entries(marked, origin=origin, kind=kind)


def _extract_marked_region(body: str, *, begin: str, end: str) -> str | None:
    """Return the text between one unambiguous begin/end pair, or None when unmarked.

    Repeated markers are rejected rather than resolved. Choosing which of two blocks is the
    real one would be exactly the silent adjudication this feature refuses to perform.
    """

    starts = _find_all(body, begin)
    ends = _find_all(body, end)
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1:
        raise MalformedDecisionBlockError("an authority block must be marked exactly once")
    if ends[0] < starts[0]:
        raise MalformedDecisionBlockError("an authority block ends before it begins")
    return body[starts[0] + len(begin) : ends[0]]


def _find_all(body: str, needle: str) -> list[int]:
    found: list[int] = []
    index = body.find(needle)
    while index != -1:
        found.append(index)
        index = body.find(needle, index + len(needle))
    return found


def _parse_entries(
    marked: str,
    *,
    origin: str,
    kind: AuthoritySourceKind,
) -> tuple[AuthoritySource, ...]:
    entries: list[AuthoritySource] = []
    subject: str | None = None
    fields: dict[str, str] = {}
    for raw_line in marked.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _ENTRY_HEADING.match(line)
        if heading is not None:
            if subject is not None:
                entries.append(_build_entry(subject, fields, origin=origin, kind=kind))
            subject = _heading_subject(heading, kind)
            fields = {}
            continue
        if subject is None:
            raise MalformedDecisionBlockError("an authority block entry must start with a heading")
        _collect_field(line, fields)
    if subject is not None:
        entries.append(_build_entry(subject, fields, origin=origin, kind=kind))
    if len(entries) > MAX_AUTHORITY_SOURCES:
        raise MalformedDecisionBlockError("the authority block declares too many entries")
    return tuple(entries)


def _heading_subject(heading: re.Match[str], kind: AuthoritySourceKind) -> str:
    declared = heading.group("kind").casefold()
    expected = kind.value.casefold()
    if declared != expected:
        raise MalformedDecisionBlockError(f"a {expected} block cannot declare a {declared}")
    return heading.group("subject")


def _collect_field(line: str, fields: dict[str, str]) -> None:
    field_match = _FIELD_LINE.match(line)
    if field_match is None:
        raise MalformedDecisionBlockError("an authority block contains an unreadable line")
    field = field_match.group("field").casefold()
    if field in fields:
        raise MalformedDecisionBlockError(f"an authority entry repeats the field '{field}'")
    fields[field] = field_match.group("value")


def _build_entry(
    subject: str,
    fields: dict[str, str],
    *,
    origin: str,
    kind: AuthoritySourceKind,
) -> AuthoritySource:
    required = (
        _REQUIRED_DECISION_FIELDS if kind is AuthoritySourceKind.DECISION else _REQUIRED_RULE_FIELDS
    )
    present = frozenset(fields)
    if missing := sorted(required - present):
        raise MalformedDecisionBlockError(
            f"an authority entry is missing required fields: {', '.join(missing)}"
        )
    if unknown := sorted(present - required - _OPTIONAL_FIELDS):
        raise MalformedDecisionBlockError(
            f"an authority entry declares unknown fields: {', '.join(unknown)}"
        )
    try:
        return AuthoritySource(
            kind=kind,
            subject=subject,
            choice=fields["decided"],
            scope=_parse_scope(fields["scope"]),
            validity=_parse_validity(fields["validity"]),
            approved_by=fields.get("approved-by"),
            rationale=fields.get("rationale"),
            origin=origin,
            superseded=_parse_flag(fields.get("superseded", "no")),
        )
    except ValidationError as exc:
        raise MalformedDecisionBlockError("an authority entry is not a valid decision") from exc


def _parse_scope(value: str) -> DecisionScope:
    scope_match = _SCOPE_VALUE.match(value.strip())
    if scope_match is None:
        raise MalformedDecisionBlockError(
            "a scope must read 'project <name>' or 'work-item <reference>'"
        )
    declared = scope_match.group("kind").casefold()
    kind = DecisionScopeKind.PROJECT if declared == "project" else DecisionScopeKind.WORK_ITEM
    try:
        return DecisionScope(kind=kind, target=scope_match.group("target"))
    except ValidationError as exc:
        raise MalformedDecisionBlockError("a scope names an invalid target") from exc


def _parse_validity(value: str) -> DecisionValidity:
    candidate = value.strip()
    if _UNTIL_SUPERSEDED.match(candidate) is not None:
        return DecisionValidity(kind=ValidityKind.UNTIL_SUPERSEDED)
    dated = _UNTIL_DATE.match(candidate)
    if dated is None:
        raise MalformedDecisionBlockError(
            "a validity must read 'until superseded' or 'until YYYY-MM-DD'"
        )
    try:
        until = date.fromisoformat(dated.group("until"))
    except ValueError as exc:
        raise MalformedDecisionBlockError("a validity names an impossible date") from exc
    return DecisionValidity(kind=ValidityKind.UNTIL_DATE, until=until)


def _parse_flag(value: str) -> bool:
    candidate = value.strip().casefold()
    if candidate in _TRUE_VALUES:
        return True
    if candidate in _FALSE_VALUES:
        return False
    raise MalformedDecisionBlockError("a superseded marker must read 'yes' or 'no'")
