from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from scripts import eval_control, run_evals  # noqa: E402

sys.path.pop(0)

ExtractedClaims = eval_control.ExtractedClaims
ProviderInvocation = eval_control.ProviderInvocation
EvalCase = run_evals.EvalCase
score = run_evals.score
_summarize = run_evals._summarize  # pyright: ignore[reportPrivateUsage]


def _case(**overrides: object) -> EvalCase:
    values: dict[str, object] = {
        "id": "example",
        "question": "Can an Order have many Payments?",
        "expected_status": "resolved",
        "required_evidence_paths": ["src/models.py"],
        "forbidden_text": ["fixture-secret-value"],
    }
    values.update(overrides)
    return EvalCase.model_validate(values)


def test_one_rubric_accepts_the_same_claims_from_either_arm() -> None:
    """Ground truth decides the verdict, so the arm that produced the claims is irrelevant."""

    case = _case()
    tool_shaped = score(case, "resolved", {"src/models.py"}, '{"answer": "many"}')
    control_shaped = score(case, "resolved", {"src/models.py"}, "Many. See src/models.py.")

    assert tool_shaped is True
    assert control_shaped is True


@pytest.mark.parametrize(
    ("status", "paths", "text"),
    [
        ("uncertain", {"src/models.py"}, "ok"),
        ("resolved", {"docs/adr/0001-payment-cardinality.md"}, "ok"),
        ("resolved", {"src/models.py"}, "leaked fixture-secret-value here"),
    ],
)
def test_rubric_rejects_wrong_status_missing_evidence_and_forbidden_text(
    status: str, paths: set[str], text: str
) -> None:
    assert score(_case(), status, paths, text) is False


def test_summary_reports_disagreement_between_repetitions_as_unstable() -> None:
    """A single run is not a result, so an arm that disagrees with itself must show it."""

    summary = _summarize(
        [
            {"passed": True, "status": "resolved", "cost_usd": 0.10},
            {"passed": False, "status": "uncertain", "cost_usd": 0.20},
        ]
    )

    assert summary["stable"] is False
    assert summary["passed_runs"] == 1
    assert summary["statuses_seen"] == ["resolved", "uncertain"]


def test_summary_reports_cost_so_a_quality_claim_is_never_costless() -> None:
    summary = _summarize([{"passed": True, "cost_usd": 0.10}, {"passed": True, "cost_usd": 0.30}])

    assert summary["total_cost_usd"] == pytest.approx(0.40)
    assert summary["mean_cost_usd"] == pytest.approx(0.20)
    assert summary["stable"] is True


def test_summary_tolerates_runs_that_recorded_no_cost() -> None:
    """A failed invocation records no cost, and must not break the aggregate."""

    summary = _summarize([{"passed": False, "operational_error": "control_arm_failed"}])

    assert summary["total_cost_usd"] == pytest.approx(0.0)
    assert summary["stable"] is True


def test_extracted_claims_reject_a_status_outside_the_tool_vocabulary() -> None:
    """Extraction converts formats; it must not invent a verdict the contract lacks."""

    with pytest.raises(ValueError, match="status"):
        ExtractedClaims.model_validate({"status": "probably", "evidence_paths": []})


def test_extractor_family_overlap_is_reported_rather_than_assumed() -> None:
    """ADR-0011 prefers a different family, and requires saying so when there is not one."""

    arm = eval_control.claude_provider("claude-opus-5")

    assert eval_control.extractor_shares_family(eval_control.configured_extractor(), arm) is True
    assert (
        eval_control.extractor_shares_family(
            ProviderInvocation(name="codex", executable="codex", model="gpt-5.4"), arm
        )
        is False
    )


def test_extractor_model_is_configurable_for_a_future_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second provider must be selectable without changing the harness."""

    monkeypatch.setenv("MAESTRO_EVAL_EXTRACTOR_MODEL", "claude-haiku-4-5")

    assert eval_control.configured_extractor().model == "claude-haiku-4-5"
