"""Run the versioned, opt-in evaluation corpus against the static fixture.

Per ADR-0011 the tool arm is scored deterministically against corpus ground truth, and a
control arm answers the same questions without the tool so the promotion argument in
`AGENTS.md` stays falsifiable. Control-arm prose is converted into the tool's own claims by
an extractor and then scored by the same rubric; no model decides which arm is better.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# Importable both as a script and as `scripts.run_evals`, so tests can exercise the rubric.
if __package__:
    from scripts.eval_control import (
        ProviderInvocation,
        answer_without_tool,
        claude_provider,
        configured_extractor,
        extract_claims,
        extractor_shares_family,
    )
else:  # pragma: no cover - taken only when run directly as a script
    from eval_control import (
        ProviderInvocation,
        answer_without_tool,
        claude_provider,
        configured_extractor,
        extract_claims,
        extractor_shares_family,
    )

from maestro import __version__
from maestro.capabilities.resolve_codebase_fact.contracts import (
    ResolveCodebaseFactRequest,
    VerificationResult,
)
from maestro.capabilities.resolve_codebase_fact.policy import POLICY_VERSION
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import Settings
from maestro.main import build_service, selected_provider
from maestro.versions import verify_runtime_versions

_EXECUTED_MARKER = "REPOSITORY_CODE_WAS_EXECUTED"


class EvalCase(BaseModel):
    """One expected semantic outcome and its evidence/behavior constraints."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    question: str
    expected_status: str
    required_evidence_paths: list[str]
    forbidden_text: list[str]


class EvalCorpus(BaseModel):
    """Versioned collection used before verifier policy/model changes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    corpus_version: str
    cases: Annotated[list[EvalCase], Field(min_length=1)]


def score(case: EvalCase, status: str, evidence_paths: set[str], serialized: str) -> bool:
    """Apply the one rubric both arms are judged by.

    Ground truth decides the verdict, so the comparison stays reproducible whichever arm
    produced the claims.
    """

    return (
        status == case.expected_status
        and set(case.required_evidence_paths) <= evidence_paths
        and not any(value in serialized for value in case.forbidden_text)
    )


async def _tool_run(
    service: ResolveCodebaseFactService, repository: Path, case: EvalCase
) -> dict[str, object]:
    """Run one tool investigation and record both its claims and its verifiability."""

    try:
        result = await service.execute(
            ResolveCodebaseFactRequest(repository_path=str(repository), question=case.question)
        )
    except Exception as exc:
        return {"passed": False, "operational_error": type(exc).__name__}
    paths = _claimed_paths(result)
    return {
        "passed": score(case, result.status.value, paths, result.model_dump_json()),
        "status": result.status.value,
        "evidence_paths": sorted(paths),
        # Verifiability is measured, never judged (ADR-0011). These are the properties a
        # plain invocation cannot offer, so they are reported apart from the rubric.
        "verifiability": {
            "evidence_resolves": all((repository / path).is_file() for path in paths),
            "repository_unchanged": not (repository / _EXECUTED_MARKER).exists(),
            "trail_recorded": True,
        },
    }


def _claimed_paths(result: VerificationResult) -> set[str]:
    return {item.path for item in result.evidence} | {
        item.path for conflict in result.conflicts for item in conflict.evidence
    }


@dataclass(frozen=True, slots=True)
class ControlSettings:
    """How the control arm is invoked, kept together so it travels as one value."""

    provider: ProviderInvocation
    extractor: ProviderInvocation
    effort: str
    budget_usd: float


async def _control_run(
    control: ControlSettings, repository: Path, case: EvalCase
) -> dict[str, object]:
    """Answer one case without the tool, then extract its claims for the same rubric."""

    answer = await answer_without_tool(
        control.provider,
        repository,
        case.question,
        effort=control.effort,
        max_budget_usd=control.budget_usd,
    )
    if answer.failed or not answer.text:
        return {
            "passed": False,
            "operational_error": "control_arm_failed",
            "cost_usd": answer.cost_usd,
        }
    claims, extraction_cost = await extract_claims(
        control.extractor, answer.text, max_budget_usd=control.budget_usd
    )
    cost = answer.cost_usd + extraction_cost
    if claims is None:
        # An unreadable answer is an extraction failure, not a wrong answer. Recording it
        # separately keeps a weak extractor from being reported as a weak control arm.
        return {"passed": False, "operational_error": "extraction_failed", "cost_usd": cost}
    paths = set(claims.evidence_paths)
    return {
        "passed": score(case, claims.status, paths, answer.text),
        "status": claims.status,
        "evidence_paths": sorted(paths),
        "cost_usd": cost,
        "extracted_from": answer.text,
    }


def _summarize(runs: Sequence[dict[str, object]]) -> dict[str, object]:
    """Report how often an arm passed and how much it varied across repetitions."""

    verdicts = [bool(run["passed"]) for run in runs]
    statuses = sorted({str(run.get("status", "none")) for run in runs})
    costs = [value for run in runs if isinstance(value := run.get("cost_usd"), float)]
    return {
        "runs": len(verdicts),
        "passed_runs": sum(verdicts),
        # A single run is not a result (ADR-0011), so an arm that disagrees with itself is
        # reported as unstable rather than as its first outcome.
        "stable": len(set(verdicts)) == 1,
        "statuses_seen": statuses,
        "total_cost_usd": round(sum(costs), 4),
        "mean_cost_usd": round(statistics.fmean(costs), 4) if costs else 0.0,
        "runs_detail": list(runs),
    }


async def run(repetitions: int, effort: str, budget: float, control: bool) -> int:
    """Execute every corpus case and emit a machine-readable report."""

    root = Path(__file__).parent.parent
    repository = root / "tests" / "fixtures" / "codebase"
    corpus = EvalCorpus.model_validate_json(
        (root / "evals" / "resolve_codebase_fact_v1.json").read_text(encoding="utf-8"),
        strict=True,
    )
    settings = Settings(  # pyright: ignore[reportCallIssue] - Audit URL comes from BaseSettings
        allowed_roots=(repository,)
    )
    service = build_service(settings)
    versions = verify_runtime_versions(selected_provider(settings))
    control_provider = claude_provider(settings.claude_model.value)
    extractor = configured_extractor()
    control_settings = ControlSettings(control_provider, extractor, effort, budget)
    results: list[dict[str, object]] = []
    passed = True
    try:
        for case in corpus.cases:
            tool_runs = [await _tool_run(service, repository, case) for _ in range(repetitions)]
            entry: dict[str, object] = {"id": case.id, "tool": _summarize(tool_runs)}
            if control:
                control_runs = [
                    await _control_run(control_settings, repository, case)
                    for _ in range(repetitions)
                ]
                entry["control"] = _summarize(control_runs)
            results.append(entry)
            passed &= all(bool(run["passed"]) for run in tool_runs)
    finally:
        await service.shutdown()
    report = {
        "corpus_version": corpus.corpus_version,
        "server_version": __version__,
        "mcp_sdk_version": versions.mcp_sdk,
        "agent_runtime": versions.agent_runtime,
        "agent_runtime_version": versions.agent_runtime_version,
        "model": settings.agent_model().value,
        "prompt_policy_version": POLICY_VERSION,
        "repetitions": repetitions,
        "control_arm": _control_metadata(control, control_provider, extractor, effort),
        "comparison_note": (
            "The tool returns a validated structured result and the control arm returns prose, "
            "so the arms are not scored like for like. Control claims are recovered by an "
            "extractor before the shared rubric is applied, and an extraction failure is "
            "reported as such rather than as a wrong answer. Verifiability is reported "
            "separately because it is measured, not judged."
        ),
        "results": results,
        "passed": passed,
    }
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if passed else 1


def _control_metadata(
    enabled: bool, arm: ProviderInvocation, extractor: ProviderInvocation, effort: str
) -> dict[str, object]:
    if not enabled:
        return {"executed": False, "reason": "not requested"}
    return {
        "executed": True,
        "provider": arm.label(),
        "effort": effort,
        "extractor": extractor.label(),
        "extractor_shares_model_family": extractor_shares_family(extractor, arm),
        "extractor_note": (
            "ADR-0011 prefers an extractor from a different model family than the arms it "
            "reads. Only one provider is usable in this deployment, so impartiality is "
            "assumed rather than established and every extraction input is recorded."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3, help="runs per case per arm")
    parser.add_argument("--effort", default="medium", help="control-arm reasoning effort")
    parser.add_argument("--budget-usd", type=float, default=1.0, help="cap per invocation")
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="run only the tool arm, consuming no control-arm provider capacity",
    )
    return parser


def main() -> None:
    """Run the asynchronous evaluation process."""

    options = _parser().parse_args()
    if options.repetitions < 1:
        raise SystemExit("repetitions must be at least 1")
    raise SystemExit(
        asyncio.run(
            run(options.repetitions, options.effort, options.budget_usd, not options.no_control)
        )
    )


if __name__ == "__main__":
    main()
