"""Coverage of the marked blocks Maestro reads as authority.

A block is accepted whole or rejected whole. These tests assert that a malformed entry never
lands as a partially accepted decision.
"""

from __future__ import annotations

from datetime import date

import pytest
from helpers.authority_fixtures import CHOICE, SUBJECT, decision_block, decision_entry

from maestro.authority.block import parse_decision_block, parse_rule_block
from maestro.authority.contracts import (
    MAX_AUTHORITY_SOURCES,
    AuthoritySource,
    AuthoritySourceKind,
    DecisionScopeKind,
    MalformedDecisionBlockError,
    ValidityKind,
)

ORIGIN = "work item 26"


def parse(*entries: str) -> tuple[AuthoritySource, ...]:
    return parse_decision_block(decision_block(*entries), origin=ORIGIN)


def test_a_marked_decision_becomes_a_typed_contract() -> None:
    (entry,) = parse_decision_block(decision_block(decision_entry()), origin=ORIGIN)

    assert entry.kind is AuthoritySourceKind.DECISION
    assert entry.subject == SUBJECT
    assert entry.choice == CHOICE
    assert entry.scope.kind is DecisionScopeKind.WORK_ITEM
    assert entry.scope.target == "26"
    assert entry.validity.kind is ValidityKind.UNTIL_SUPERSEDED
    assert entry.approved_by == "an-operator"
    assert entry.origin == ORIGIN


def test_prose_outside_the_block_is_context_and_never_authority() -> None:
    body = "\n".join(
        [
            "### Decision: audit.persistence_backend",
            "- Decided: sqlite",
            "- Scope: project maestro",
            "- Validity: until superseded",
            "- Approved-by: nobody",
            "",
            decision_block(decision_entry()),
        ]
    )

    entries = parse_decision_block(body, origin=ORIGIN)

    assert [entry.choice for entry in entries] == [CHOICE]


def test_a_work_item_with_no_block_carries_no_decisions_and_is_not_an_error() -> None:
    assert parse_decision_block("Just a description of the work.", origin=ORIGIN) == ()


def test_an_empty_block_carries_no_decisions() -> None:
    assert parse_decision_block(decision_block(), origin=ORIGIN) == ()


def test_a_dated_validity_is_parsed() -> None:
    (entry,) = parse(decision_entry(validity="until 2027-01-01"))

    assert entry.validity.kind is ValidityKind.UNTIL_DATE
    assert entry.validity.until == date(2027, 1, 1)


def test_a_project_scope_is_parsed() -> None:
    (entry,) = parse(decision_entry(scope="project maestro"))

    assert entry.scope.kind is DecisionScopeKind.PROJECT
    assert entry.scope.target == "maestro"


def test_a_superseded_marker_is_parsed() -> None:
    (entry,) = parse(decision_entry(extra=("- Superseded: yes",)))

    assert entry.superseded is True


def test_a_rationale_is_carried_through() -> None:
    (entry,) = parse(decision_entry(extra=("- Rationale: a shared durable store",)))

    assert entry.rationale == "a shared durable store"


def test_several_decisions_are_parsed_in_order() -> None:
    entries = parse(
        decision_entry(subject="one"),
        decision_entry(subject="two", decided="sqlite"),
    )

    assert [entry.subject for entry in entries] == ["one", "two"]


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(decision_entry(approved_by=None), id="missing approver"),
        pytest.param(
            "\n".join(
                [
                    f"### Decision: {SUBJECT}",
                    f"- Decided: {CHOICE}",
                    "- Validity: until superseded",
                    "- Approved-by: an-operator",
                ]
            ),
            id="missing scope",
        ),
        pytest.param(decision_entry(validity="forever"), id="unparseable validity"),
        pytest.param(decision_entry(validity="until 2026-13-40"), id="impossible date"),
        pytest.param(decision_entry(scope="everything"), id="unparseable scope"),
        pytest.param(decision_entry(scope="team platform"), id="unknown scope kind"),
        pytest.param(decision_entry(decided=""), id="empty decided value"),
        pytest.param(
            decision_entry(extra=("- Superseded: maybe",)), id="unparseable superseded marker"
        ),
        pytest.param(decision_entry(extra=("- Owner: someone",)), id="unknown field"),
        pytest.param(decision_entry(extra=(f"- Decided: {CHOICE}",)), id="repeated field"),
        pytest.param("- Decided: postgresql", id="field without a heading"),
        pytest.param(f"### Decision: {SUBJECT}\nnot a field line", id="unreadable line"),
        pytest.param(f"### Rule: {SUBJECT}\n- Decided: x", id="rule inside a decision block"),
    ],
)
def test_a_malformed_entry_rejects_the_whole_block(entry: str) -> None:
    with pytest.raises(MalformedDecisionBlockError):
        parse(entry)


def test_a_malformed_entry_does_not_partially_accept_its_neighbours() -> None:
    with pytest.raises(MalformedDecisionBlockError):
        parse(decision_entry(subject="sound"), decision_entry(subject="broken", validity="soon"))


def test_a_block_marked_twice_is_rejected_rather_than_resolved() -> None:
    body = decision_block(decision_entry()) + decision_block(decision_entry(decided="sqlite"))

    with pytest.raises(MalformedDecisionBlockError):
        parse_decision_block(body, origin=ORIGIN)


def test_a_block_that_ends_before_it_begins_is_rejected() -> None:
    body = "<!-- maestro:decisions:end -->\n<!-- maestro:decisions:begin -->"

    with pytest.raises(MalformedDecisionBlockError):
        parse_decision_block(body, origin=ORIGIN)


def test_an_unclosed_block_is_rejected() -> None:
    with pytest.raises(MalformedDecisionBlockError):
        parse_decision_block("<!-- maestro:decisions:begin -->\n", origin=ORIGIN)


def test_an_oversized_body_is_rejected_before_parsing() -> None:
    with pytest.raises(MalformedDecisionBlockError):
        parse_decision_block("x" * 200_000, origin=ORIGIN)


def test_too_many_entries_are_rejected() -> None:
    entries = [
        decision_entry(subject=f"subject-{index}") for index in range(MAX_AUTHORITY_SOURCES + 1)
    ]

    with pytest.raises(MalformedDecisionBlockError):
        parse(*entries)


def test_a_rule_block_yields_rules_that_name_no_approver() -> None:
    body = "\n".join(
        [
            "<!-- maestro:rules:begin -->",
            "### Rule: naming.local_helpers",
            "- Decided: agent-chosen",
            "- Scope: project maestro",
            "- Validity: until superseded",
            "<!-- maestro:rules:end -->",
        ]
    )

    (entry,) = parse_rule_block(body, origin="rules.md")

    assert entry.kind is AuthoritySourceKind.RULE
    assert entry.approved_by is None


def test_a_rule_that_names_an_approver_is_rejected() -> None:
    body = "\n".join(
        [
            "<!-- maestro:rules:begin -->",
            "### Rule: naming.local_helpers",
            "- Decided: agent-chosen",
            "- Scope: project maestro",
            "- Validity: until superseded",
            "- Approved-by: an-operator",
            "<!-- maestro:rules:end -->",
        ]
    )

    with pytest.raises(MalformedDecisionBlockError):
        parse_rule_block(body, origin="rules.md")


def test_a_decision_block_and_a_rule_block_do_not_read_each_other() -> None:
    body = decision_block(decision_entry())

    assert parse_rule_block(body, origin=ORIGIN) == ()
