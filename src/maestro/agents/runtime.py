"""Runtime-neutral verifier worker boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from maestro.capabilities.resolve_codebase_fact.contracts import VerificationResult
from maestro.model_identity import ModelIdentifier


@dataclass(frozen=True, slots=True)
class AgentRuntimeProvider:
    """One worker implementation, named and pinned by the adapter that supplies it.

    Startup verification and Audit metadata read identity from here rather than from
    literals, so a deployment built against a different provider changes only this value.
    """

    name: str
    version: str
    distributions: Mapping[str, str]
    """Exact distribution pins the adapter requires, as distribution name to version."""


@dataclass(frozen=True, slots=True)
class InvestigationRequest:
    """Internal, authorized request sent to exactly one worker."""

    repository_root: Path
    question: str
    context: str | None
    repository_fingerprint: str
    model: ModelIdentifier
    max_output_bytes: int


class AgentRuntime(Protocol):
    """AI-runtime-independent investigation interface."""

    async def investigate(self, request: InvestigationRequest) -> VerificationResult:
        """Investigate one request and return strict structured output."""
        ...


type FakeResponder = Callable[
    [InvestigationRequest], VerificationResult | Awaitable[VerificationResult]
]


class FakeAgentRuntime:
    """Typed deterministic runtime for application and integration tests."""

    def __init__(self, responder: FakeResponder) -> None:
        self._responder = responder
        self.requests: list[InvestigationRequest] = []

    async def investigate(self, request: InvestigationRequest) -> VerificationResult:
        """Record the request and return the configured deterministic response."""

        self.requests.append(request)
        result = self._responder(request)
        if isinstance(result, Awaitable):
            return await result
        return result
