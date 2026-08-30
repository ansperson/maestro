"""Run the versioned, opt-in Codex evaluation corpus against the static fixture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from maestro import __version__
from maestro.capabilities.resolve_codebase_fact.contracts import ResolveCodebaseFactRequest
from maestro.capabilities.resolve_codebase_fact.policy import POLICY_VERSION
from maestro.config import Settings
from maestro.main import build_service
from maestro.versions import verify_runtime_versions


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


async def run() -> int:
    """Execute every corpus case once and emit a machine-readable report."""

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
    versions = verify_runtime_versions()
    outcomes: list[dict[str, object]] = []
    passed = True
    try:
        for case in corpus.cases:
            try:
                result = await service.execute(
                    ResolveCodebaseFactRequest(
                        repository_path=str(repository),
                        question=case.question,
                    )
                )
            except Exception as exc:
                passed = False
                outcomes.append(
                    {
                        "id": case.id,
                        "passed": False,
                        "operational_error": type(exc).__name__,
                    }
                )
                continue
            serialized = result.model_dump_json()
            evidence_paths = {item.path for item in result.evidence} | {
                item.path for conflict in result.conflicts for item in conflict.evidence
            }
            case_passed = (
                result.status.value == case.expected_status
                and set(case.required_evidence_paths) <= evidence_paths
                and not any(value in serialized for value in case.forbidden_text)
                and not (repository / "REPOSITORY_CODE_WAS_EXECUTED").exists()
            )
            passed &= case_passed
            outcomes.append(
                {
                    "id": case.id,
                    "passed": case_passed,
                    "status": result.status.value,
                    "evidence_paths": sorted(evidence_paths),
                }
            )
    finally:
        await service.shutdown()
    report = {
        "corpus_version": corpus.corpus_version,
        "server_version": __version__,
        "mcp_sdk_version": versions.mcp_sdk,
        "codex_sdk_version": versions.codex_sdk,
        "codex_runtime_version": versions.codex_runtime,
        "model": settings.codex_model,
        "prompt_policy_version": POLICY_VERSION,
        "results": outcomes,
        "passed": passed,
    }
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if passed else 1


def main() -> None:
    """Run the asynchronous evaluation process."""

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
