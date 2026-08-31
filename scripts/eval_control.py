"""Control arm and structured extraction for the tool evaluation (ADR-0011).

The control arm answers the corpus without the tool, so the promotion argument in
`AGENTS.md` stays falsifiable. Extraction converts its prose into the same claims the tool
returns, and one deterministic rubric then scores both arms. No model decides which arm is
better.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

# The control arm reads the repository the way an operator would without Maestro: the same
# binary and the same read-only tools, but no verifier policy, no output schema, and no
# validation. Anything more would stop answering "is the tool worth more than using the
# provider directly?".
_CONTROL_TOOLS = ("Read", "Glob", "Grep")
_DENIED_TOOLS = ("Write", "Edit", "NotebookEdit", "Bash", "WebSearch", "WebFetch", "Task")
_ENVIRONMENT_NAMES = ("HOME", "PATH", "USER")
_MAX_OUTPUT_BYTES = 1_048_576

_EXTRACTION_INSTRUCTIONS = """You convert one answer about a repository into structured claims.

You are not judging quality. Report only what the answer states.

status: which verdict the answer expresses.
  resolved - it states a definite factual answer.
  uncertain - it says the evidence is insufficient or contradictory.
  human_decision_required - it says the question needs a human or normative decision.
evidence_paths: every repository-relative file path the answer cites as evidence.
  Use the exact path as written. Omit paths it merely mentions in passing.
"""


class _Envelope(BaseModel):
    """The provider envelope fields the evaluation reads; the rest is ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    is_error: bool = False
    result: str = ""
    total_cost_usd: float = 0.0


class ExtractedClaims(BaseModel):
    """The claims a control-arm answer makes, in the tool's own vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["resolved", "uncertain", "human_decision_required"]
    evidence_paths: list[Annotated[str, StringConstraints(min_length=1, max_length=1_024)]]


@dataclass(frozen=True, slots=True)
class ProviderInvocation:
    """One provider the evaluation can invoke, named so a report states what produced it."""

    name: str
    executable: str
    model: str

    def label(self) -> str:
        return f"{self.name}:{self.model}"


@dataclass(frozen=True, slots=True)
class Answer:
    """One provider response, with the cost it consumed."""

    text: str
    cost_usd: float
    failed: bool = False


def claude_provider(model: str = "claude-opus-5") -> ProviderInvocation:
    """Return the Claude Code invocation used when no other provider is configured."""

    return ProviderInvocation(
        name="claude", executable=os.environ.get("MAESTRO_CLAUDE_EXECUTABLE", "claude"), model=model
    )


def configured_extractor() -> ProviderInvocation:
    """Return the extractor provider.

    ADR-0011 prefers an extractor from a different model family than the arms it reads. Only
    one provider is usable today, so the default shares a family and the report says so.
    `MAESTRO_EVAL_EXTRACTOR_MODEL` selects another model once one is available.
    """

    return claude_provider(os.environ.get("MAESTRO_EVAL_EXTRACTOR_MODEL", "claude-opus-5"))


def extractor_shares_family(extractor: ProviderInvocation, arm: ProviderInvocation) -> bool:
    """Report whether impartiality is assumed rather than established."""

    return extractor.name == arm.name


async def answer_without_tool(
    provider: ProviderInvocation,
    repository: Path,
    question: str,
    *,
    effort: str,
    max_budget_usd: float,
) -> Answer:
    """Answer one question with repository read access but none of Maestro's scaffolding."""

    command = (
        provider.executable,
        "--print",
        "--output-format",
        "json",
        "--model",
        provider.model,
        "--effort",
        effort,
        "--max-budget-usd",
        str(max_budget_usd),
        "--allowed-tools",
        ",".join(_CONTROL_TOOLS),
        "--disallowed-tools",
        ",".join(_DENIED_TOOLS),
    )
    envelope = await _invoke(command, question.encode("utf-8"), cwd=repository)
    if envelope is None:
        return Answer(text="", cost_usd=0.0, failed=True)
    return Answer(text=envelope.result, cost_usd=envelope.total_cost_usd, failed=envelope.is_error)


async def extract_claims(
    provider: ProviderInvocation, answer: str, *, max_budget_usd: float
) -> tuple[ExtractedClaims | None, float]:
    """Convert one prose answer into structured claims, or report that it could not be read."""

    schema = json.dumps(ExtractedClaims.model_json_schema())
    command = (
        provider.executable,
        "--print",
        "--output-format",
        "json",
        "--model",
        provider.model,
        "--effort",
        "low",
        "--max-budget-usd",
        str(max_budget_usd),
        "--json-schema",
        schema,
        "--system-prompt",
        _EXTRACTION_INSTRUCTIONS,
        "--disallowed-tools",
        ",".join((*_CONTROL_TOOLS, *_DENIED_TOOLS)),
    )
    envelope = await _invoke(command, answer.encode("utf-8"), cwd=Path.cwd())
    if envelope is None or envelope.is_error:
        return None, 0.0
    try:
        return ExtractedClaims.model_validate_json(envelope.result), envelope.total_cost_usd
    except ValidationError:
        return None, envelope.total_cost_usd


async def _invoke(command: tuple[str, ...], payload: bytes, *, cwd: Path) -> _Envelope | None:
    """Run one bounded provider invocation and return its parsed envelope."""

    environment = {
        name: value for name in _ENVIRONMENT_NAMES if (value := os.environ.get(name)) is not None
    }
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
            close_fds=True,
        )
    except OSError:
        return None
    if process.stdin is None or process.stdout is None:
        return None
    process.stdin.write(payload)
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await process.stdin.drain()
    process.stdin.close()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await process.stdin.wait_closed()
    raw = await process.stdout.read(_MAX_OUTPUT_BYTES)
    await process.wait()
    if process.returncode != 0:
        return None
    try:
        return _Envelope.model_validate_json(raw)
    except ValidationError:
        return None
