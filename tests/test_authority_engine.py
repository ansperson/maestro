"""Behavioral coverage of the deterministic authority engine.

Every test asks the question an operator would: given these decisions and this action, is it
cleared? None of them assert how the engine matched a rule.
"""

from __future__ import annotations

from datetime import date

import pytest
from helpers.authority_fixtures import (
    CHOICE,
    PROJECT,
    SUBJECT,
    TODAY,
    WORK_ITEM,
    action,
    decision,
    rule,
    until,
)
from hypothesis import given, settings as hypothesis_settings, strategies as st

from maestro.authority.contracts import DecisionScopeKind, ValidityKind
from maestro.authority.engine import (
    ApprovalReason,
    AuthorityOutcomeKind,
    evaluate_authority,
)


def test_a_decision_in_the_work_item_clears_a_matching_action() -> None:
    outcome = evaluate_authority(action(), [decision()], evaluated_on=TODAY)

    assert outcome.kind is AuthorityOutcomeKind.CLEARED
    assert outcome.applied is not None
    assert outcome.applied.choice == CHOICE


def test_an_action_no_decision_covers_is_not_cleared() -> None:
    outcome = evaluate_authority(action(), [], evaluated_on=TODAY)

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED
    assert outcome.reason is ApprovalReason.NO_COVERING_SOURCE
    assert outcome.considered == ()


def test_a_decision_about_another_subject_does_not_clear_this_action() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(subject="audit.retention")],
        evaluated_on=TODAY,
    )

    assert outcome.reason is ApprovalReason.NO_COVERING_SOURCE


def test_a_project_scoped_decision_clears_an_action_in_that_project() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(scope_kind=DecisionScopeKind.PROJECT, scope_target=PROJECT)],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CLEARED


def test_a_decision_for_another_work_item_does_not_reach_this_one() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(scope_target="99")],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED
    assert outcome.reason is ApprovalReason.OUT_OF_SCOPE
    assert len(outcome.considered) == 1


def test_a_decision_for_another_project_does_not_reach_this_one() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(scope_kind=DecisionScopeKind.PROJECT, scope_target="another-product")],
        evaluated_on=TODAY,
    )

    assert outcome.reason is ApprovalReason.OUT_OF_SCOPE


def test_a_work_item_scoped_decision_does_not_govern_the_whole_project() -> None:
    """The failure ADR-0006 exists to prevent: one case silently governing another."""

    outcome = evaluate_authority(
        action(work_item="99"),
        [decision(scope_target=WORK_ITEM)],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED


@pytest.mark.parametrize("near_miss", ["2", "260", "26 ", " 26"])
def test_only_an_exact_target_match_reuses_a_decision(near_miss: str) -> None:
    outcome = evaluate_authority(
        action(work_item="26"),
        [decision(scope_target=near_miss)],
        evaluated_on=TODAY,
    )

    expected = (
        AuthorityOutcomeKind.CLEARED
        if near_miss.strip() == "26"
        else AuthorityOutcomeKind.APPROVAL_REQUIRED
    )
    assert outcome.kind is expected


def test_case_and_spacing_do_not_distinguish_a_subject() -> None:
    outcome = evaluate_authority(
        action(subject="Audit.Persistence_Backend", choice="PostgreSQL"),
        [decision()],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CLEARED


def test_a_dated_decision_holds_on_its_final_day() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(validity=until(TODAY))],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CLEARED


def test_a_dated_decision_stops_clearing_the_day_after_it_lapses() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(validity=until(date(2026, 8, 30)))],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED
    assert outcome.reason is ApprovalReason.VALIDITY_LAPSED
    assert len(outcome.considered) == 1


def test_a_superseded_decision_no_longer_clears_an_action() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(superseded=True)],
        evaluated_on=TODAY,
    )

    assert outcome.reason is ApprovalReason.VALIDITY_LAPSED


def test_a_lapsed_decision_is_reported_apart_from_no_decision_at_all() -> None:
    lapsed = evaluate_authority(
        action(),
        [decision(validity=until(date(2020, 1, 1)))],
        evaluated_on=TODAY,
    )
    absent = evaluate_authority(action(), [], evaluated_on=TODAY)

    assert lapsed.reason is not absent.reason
    assert lapsed.summary != absent.summary


def test_a_decision_in_force_that_settles_the_subject_differently_is_not_cleared() -> None:
    outcome = evaluate_authority(
        action(choice="sqlite"),
        [decision(choice="postgresql")],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED
    assert outcome.reason is ApprovalReason.DIFFERENT_CHOICE_DECIDED
    assert outcome.considered[0].choice == "postgresql"


def test_two_decisions_that_answer_the_same_action_differently_conflict() -> None:
    outcome = evaluate_authority(
        action(),
        [
            decision(choice="postgresql", origin="work item 26"),
            decision(choice="sqlite", origin="ADR-0005"),
        ],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CONFLICT
    assert outcome.applied is None
    assert {entry.choice for entry in outcome.considered} == {"postgresql", "sqlite"}


def test_a_decision_conflicting_with_a_written_rule_is_a_conflict_not_a_precedence() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(choice="postgresql"), rule(choice="sqlite")],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CONFLICT
    assert len(outcome.considered) == 2


def test_two_sources_that_agree_are_not_a_conflict() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(choice=CHOICE), rule(choice=CHOICE)],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CLEARED


def test_a_cleared_action_records_the_human_decision_rather_than_the_delegation() -> None:
    outcome = evaluate_authority(
        action(),
        [rule(choice=CHOICE), decision(choice=CHOICE)],
        evaluated_on=TODAY,
    )

    assert outcome.applied is not None
    assert outcome.applied.approved_by == "an-operator"


def test_a_lapsed_source_cannot_create_a_conflict() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(choice=CHOICE), decision(choice="sqlite", superseded=True)],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CLEARED


def test_an_out_of_scope_source_cannot_create_a_conflict() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(choice=CHOICE), decision(choice="sqlite", scope_target="99")],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.CLEARED


def test_a_written_rule_clears_an_action_without_an_approver() -> None:
    outcome = evaluate_authority(action(), [rule()], evaluated_on=TODAY)

    assert outcome.kind is AuthorityOutcomeKind.CLEARED
    assert outcome.applied is not None
    assert outcome.applied.approved_by is None


def test_a_rule_that_does_not_name_this_project_does_not_reach_the_action() -> None:
    outcome = evaluate_authority(
        action(),
        [rule(scope_target="another-product")],
        evaluated_on=TODAY,
    )

    assert outcome.reason is ApprovalReason.OUT_OF_SCOPE


def test_removing_the_only_rule_changes_what_runs_unattended() -> None:
    """Supervision is tuned by writing a rule, with no code change."""

    with_rule = evaluate_authority(action(), [rule()], evaluated_on=TODAY)
    without_rule = evaluate_authority(action(), [], evaluated_on=TODAY)

    assert with_rule.kind is AuthorityOutcomeKind.CLEARED
    assert without_rule.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED


def test_a_lapsed_decision_is_reported_ahead_of_an_out_of_scope_one() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(superseded=True), decision(scope_target="99")],
        evaluated_on=TODAY,
    )

    assert outcome.reason is ApprovalReason.VALIDITY_LAPSED


def test_the_same_inputs_always_produce_the_same_outcome() -> None:
    sources = [
        rule(choice=CHOICE, origin="rules.md"),
        decision(choice=CHOICE, origin="work item 26"),
        decision(choice=CHOICE, scope_kind=DecisionScopeKind.PROJECT, scope_target=PROJECT),
    ]
    first = evaluate_authority(action(), sources, evaluated_on=TODAY)
    second = evaluate_authority(action(), list(reversed(sources)), evaluated_on=TODAY)

    assert first == second


@hypothesis_settings(max_examples=200, deadline=None)
@given(
    subject=st.text(min_size=1, max_size=40).filter(lambda value: value.strip() != ""),
    choice=st.text(min_size=1, max_size=40).filter(lambda value: value.strip() != ""),
    other_choice=st.text(min_size=1, max_size=40).filter(lambda value: value.strip() != ""),
    evaluated_on=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)),
)
def test_an_action_is_never_cleared_by_a_source_that_settles_it_differently(
    subject: str,
    choice: str,
    other_choice: str,
    evaluated_on: date,
) -> None:
    outcome = evaluate_authority(
        action(subject=subject, choice=choice),
        [decision(subject=subject, choice=other_choice)],
        evaluated_on=evaluated_on,
    )

    cleared = outcome.kind is AuthorityOutcomeKind.CLEARED
    assert cleared is (
        " ".join(choice.split()).casefold() == " ".join(other_choice.split()).casefold()
    )


def test_the_engine_reads_no_clock_of_its_own() -> None:
    """A dated decision's fate is decided by the caller's day, not by wall-clock time."""

    dated = decision(validity=until(date(2026, 1, 1)))

    assert (
        evaluate_authority(action(), [dated], evaluated_on=date(2025, 12, 31)).kind
        is AuthorityOutcomeKind.CLEARED
    )
    assert (
        evaluate_authority(action(), [dated], evaluated_on=date(2026, 1, 2)).kind
        is AuthorityOutcomeKind.APPROVAL_REQUIRED
    )


def test_a_conflict_summary_states_that_maestro_does_not_choose() -> None:
    outcome = evaluate_authority(
        action(),
        [decision(choice="postgresql"), decision(choice="sqlite")],
        evaluated_on=TODAY,
    )

    assert "does not choose" in outcome.summary


def test_an_until_superseded_decision_never_lapses_on_its_own() -> None:
    entry = decision()

    assert entry.validity.kind is ValidityKind.UNTIL_SUPERSEDED
    assert (
        evaluate_authority(action(), [entry], evaluated_on=date(2099, 1, 1)).kind
        is AuthorityOutcomeKind.CLEARED
    )


def test_the_subject_alone_does_not_authorize_an_unrelated_choice() -> None:
    outcome = evaluate_authority(
        action(subject=SUBJECT, choice="mysql"),
        [decision(subject=SUBJECT, choice=CHOICE)],
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED


def test_a_source_that_is_both_lapsed_and_out_of_scope_is_still_reported() -> None:
    """Regression: an entry disqualified twice fell through both buckets and vanished.

    The result claimed nothing spoke to the subject while something did, which is exactly the
    distinction the reason codes exist to make.
    """

    outcome = evaluate_authority(
        action(),
        [decision(scope_target="99", validity=until(date(2020, 1, 1)))],
        evaluated_on=TODAY,
    )

    assert outcome.reason is not ApprovalReason.NO_COVERING_SOURCE
    assert len(outcome.considered) == 1
    assert "No decision or rule speaks to this subject." not in outcome.summary


def test_nothing_speaking_to_the_subject_is_the_only_way_to_report_no_covering_source() -> None:
    speaks = [decision(scope_target="99", superseded=True)]
    silent = [decision(subject="another.subject")]

    assert (
        evaluate_authority(action(), speaks, evaluated_on=TODAY).reason
        is not ApprovalReason.NO_COVERING_SOURCE
    )
    assert (
        evaluate_authority(action(), silent, evaluated_on=TODAY).reason
        is ApprovalReason.NO_COVERING_SOURCE
    )
