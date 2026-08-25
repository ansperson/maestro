from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, strategies as st
from pydantic import ValidationError

from maestro.capabilities.resolve_codebase_fact.contracts import (
    Confidence,
    Evidence,
    ResolveCodebaseFactRequest,
    VerificationResult,
    VerificationStatus,
)


def test_request_rejects_extra_fields_and_controls() -> None:
    with pytest.raises(ValidationError):
        ResolveCodebaseFactRequest.model_validate(
            {
                "repository_path": str(Path.cwd()),
                "question": "Fact?",
                "unexpected": True,
            },
            strict=True,
        )
    with pytest.raises(ValidationError, match="control"):
        ResolveCodebaseFactRequest(repository_path=str(Path.cwd()), question="bad\x00question")


@pytest.mark.parametrize(
    ("status", "answer", "evidence"),
    [
        (VerificationStatus.RESOLVED, None, []),
        (VerificationStatus.RESOLVED, "yes", []),
        (
            VerificationStatus.HUMAN_DECISION_REQUIRED,
            "make a decision",
            [],
        ),
    ],
)
def test_result_invariants(
    status: VerificationStatus,
    answer: str | None,
    evidence: list[Evidence],
) -> None:
    with pytest.raises(ValidationError):
        VerificationResult(
            status=status,
            answer=answer,
            confidence=Confidence.HIGH,
            evidence=evidence,
            conflicts=[],
            reason="reason",
        )


def test_resolved_result_serializes_as_strict_json() -> None:
    result = VerificationResult(
        status=VerificationStatus.RESOLVED,
        answer="An Order has many Payments.",
        confidence=Confidence.HIGH,
        evidence=[Evidence(path="src/models.py", line_start=1, line_end=3, finding="list")],
        conflicts=[],
        reason="Source and schema agree.",
    )
    encoded = json.loads(result.model_dump_json())
    assert encoded["status"] == "resolved"
    assert encoded["evidence"][0]["path"] == "src/models.py"


@given(
    line_start=st.integers(min_value=1, max_value=10_000),
    line_end=st.integers(min_value=1, max_value=10_000),
)
def test_evidence_line_order_property(line_start: int, line_end: int) -> None:
    payload = {
        "path": "source.py",
        "line_start": line_start,
        "line_end": line_end,
        "finding": "finding",
    }
    if line_start <= line_end:
        assert Evidence.model_validate(payload).line_end == line_end
    else:
        with pytest.raises(ValidationError):
            Evidence.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../secret", "src/../secret", "src\\secret.py", "src//file.py"],
)
def test_evidence_rejects_non_normalized_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        Evidence(path=path, finding="bad")


def test_output_collection_bounds() -> None:
    evidence = [Evidence(path=f"src/{index}.py", finding="x") for index in range(21)]
    with pytest.raises(ValidationError):
        VerificationResult(
            status=VerificationStatus.RESOLVED,
            answer="answer",
            confidence=Confidence.HIGH,
            evidence=evidence,
            conflicts=[],
            reason="reason",
        )
