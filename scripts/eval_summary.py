#!/usr/bin/env python3
"""Render an evaluation report as a table an operator can read at a glance.

The JSON report stays the record; this is the view behind `make eval`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

_EXPECTED_ARGUMENTS = 2


class ArmSummary(BaseModel):
    """One arm's outcome for one case across its repetitions."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    runs: int
    passed_runs: int
    stable: bool
    statuses_seen: list[str] = Field(default_factory=list)
    total_cost_usd: float = 0.0


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    tool: ArmSummary
    control: ArmSummary | None = None


class Report(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    corpus_version: str
    server_version: str
    model: str
    repetitions: int
    passed: bool
    control_arm: dict[str, object] = Field(default_factory=dict)
    results: Annotated[list[CaseResult], Field(default_factory=list)]


def _cell(arm: ArmSummary | None) -> str:
    if arm is None:
        return "        -"
    mark = " " if arm.stable else "!"
    return f"{arm.passed_runs}/{arm.runs} {mark}"


def render(report: Report) -> None:
    """Print the comparison, marking any arm that disagreed with itself."""

    control = report.control_arm
    print(f"\n  corpus {report.corpus_version}   server {report.server_version}")
    print(f"  model {report.model}   repetitions {report.repetitions}")
    if control.get("executed"):
        shared = control.get("extractor_shares_model_family")
        print(f"  control {control.get('provider')}   extractor {control.get('extractor')}")
        if shared:
            # ADR-0011 prefers a different family; say so rather than let a reader assume.
            print("  note: extractor shares the arms' model family, so impartiality is assumed")
    else:
        print("  control arm not executed")

    # The tool arm reports no cost: the adapter does not surface the provider envelope
    # through VerificationResult, so only the control arm's spend is known here.
    print(f"\n  {'case':34s}{'tool':>10s}{'control':>12s}{'control cost':>14s}")
    tool_passed = control_passed = total = 0
    cost = 0.0
    for case in report.results:
        arm_cost = case.control.total_cost_usd if case.control else 0.0
        cost += arm_cost
        total += 1
        tool_passed += case.tool.passed_runs == case.tool.runs
        control_passed += bool(case.control and case.control.passed_runs == case.control.runs)
        shown = f"${arm_cost:.4f}" if case.control else "-"
        print(f"  {case.id:34s}{_cell(case.tool):>10s}{_cell(case.control):>12s}{shown:>14s}")

    print(f"\n  cases fully passing: tool {tool_passed}/{total}", end="")
    if any(case.control for case in report.results):
        print(f"   control {control_passed}/{total}")
    else:
        print()
    print(f"  control-arm cost: ${cost:.4f}")
    if any(
        not case.tool.stable or (case.control and not case.control.stable)
        for case in report.results
    ):
        print("  ! marks an arm that answered differently across repetitions")
    print(f"  verdict: {'passed' if report.passed else 'failed'} (tool arm against ground truth)")


def main() -> int:
    """Render the report named on the command line."""

    if len(sys.argv) != _EXPECTED_ARGUMENTS:
        print("usage: eval_summary.py REPORT.json", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        render(Report.model_validate_json(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"  could not read {path}: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
