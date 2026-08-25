"""Application service for the single Maestro v1 Capability."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from collections.abc import Iterable

from maestro import __version__
from maestro.agents.runtime import AgentRuntime, InvestigationRequest
from maestro.audit import AuditRecorder
from maestro.capabilities.resolve_codebase_fact.contracts import (
    Evidence,
    ResolveCodebaseFactRequest,
    VerificationResult,
    human_decision_result,
)
from maestro.capabilities.resolve_codebase_fact.policy import (
    POLICY_VERSION,
    neutralize_question,
    requires_human_decision,
)
from maestro.capabilities.resolve_codebase_fact.sanitization import sanitize_result
from maestro.config import Settings
from maestro.errors import (
    AgentTimeoutError,
    ErrorCode,
    InvalidInputError,
    MaestroError,
    OutputLimitExceededError,
    RecursionNotAllowedError,
    RepositoryChangedError,
)
from maestro.execution.admission import AdmissionController
from maestro.repository.guard import AuthorizedRepository, RepositoryFingerprint, RepositoryGuard

_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("maestro_verifier_depth", default=0)
_LOGGER = logging.getLogger("maestro.resolve_codebase_fact")


class ResolveCodebaseFactService:
    """Authorize, execute once, validate evidence, and return a safe result."""

    def __init__(
        self,
        settings: Settings,
        runtime: AgentRuntime,
        audit: AuditRecorder,
        repository_guard: RepositoryGuard | None = None,
        admission: AdmissionController | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._audit = audit
        self._repository = repository_guard or RepositoryGuard(settings)
        self._admission = admission or AdmissionController(
            settings.max_concurrency, settings.max_queue_size
        )

    async def execute(self, request: ResolveCodebaseFactRequest) -> VerificationResult:
        """Execute exactly one bounded verifier investigation."""

        if _DEPTH.get() != 0:
            raise RecursionNotAllowedError
        self._validate_configured_lengths(request)
        repository = self._repository.authorize(request.repository_path)
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        queued_at = started
        fingerprint: RepositoryFingerprint | None = None
        depth_token = _DEPTH.set(1)
        try:
            async with self._admission.slot():
                queue_duration_ms = round((time.monotonic() - queued_at) * 1_000, 2)
                async with asyncio.timeout(self._settings.verifier_timeout_seconds):
                    fingerprint = await self._repository.fingerprint(repository)
                    objective = neutralize_question(request.question)
                    audit_handle = await self._audit.start_resolve_codebase_fact(
                        repository,
                        fingerprint,
                        objective,
                    )
                    request_id = audit_handle.execution_id.hex
                    result = await self._investigate(repository, request, fingerprint, objective)
                    await self._validate_result(repository, fingerprint, result)
                    result = sanitize_result(result, repository.root)
                    self._validate_result_size(result)
                    await self._audit.record_investigation_completed(
                        audit_handle,
                        repository,
                        result,
                    )
        except TimeoutError as exc:
            error = AgentTimeoutError()
            self._log_failure(
                error.code,
                request_id,
                repository,
                fingerprint,
                started,
            )
            raise error from exc
        except asyncio.CancelledError:
            self._log_failure(
                ErrorCode.AGENT_CANCELLED,
                request_id,
                repository,
                fingerprint,
                started,
            )
            raise
        except MaestroError as exc:
            self._log_failure(
                exc.code,
                request_id,
                repository,
                fingerprint,
                started,
            )
            raise
        else:
            _LOGGER.info(
                "capability completed",
                extra={
                    "metadata": {
                        "request_id": request_id,
                        "capability": "resolve_codebase_fact",
                        "repository": repository.repository_id,
                        "repository_fingerprint": fingerprint.digest,
                        "server_version": __version__,
                        "model": self._settings.codex_model,
                        "prompt_policy_version": POLICY_VERSION,
                        "duration_ms": round((time.monotonic() - started) * 1_000, 2),
                        "queue_duration_ms": queue_duration_ms,
                        "status": result.status.value,
                        "confidence": result.confidence.value,
                        "evidence_count": len(result.evidence),
                    }
                },
            )
            return result
        finally:
            _DEPTH.reset(depth_token)

    async def shutdown(self) -> None:
        """Stop admission and cancel active investigations."""

        await self._admission.shutdown()

    async def _investigate(
        self,
        repository: AuthorizedRepository,
        request: ResolveCodebaseFactRequest,
        fingerprint: RepositoryFingerprint,
        objective: str,
    ) -> VerificationResult:
        if requires_human_decision(request.question):
            return human_decision_result(
                "The question asks what should be decided, not what is currently true."
            )
        investigation = InvestigationRequest(
            repository_root=repository.root,
            question=objective,
            context=request.context,
            repository_fingerprint=fingerprint.digest,
            model=self._settings.codex_model,
            max_output_bytes=self._settings.max_agent_output_bytes,
        )
        return await self._runtime.investigate(investigation)

    async def _validate_result(
        self,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint,
        result: VerificationResult,
    ) -> None:
        if len(result.evidence) > self._settings.max_evidence_items:
            raise OutputLimitExceededError("The result contains too many evidence items.")
        if len(result.conflicts) > self._settings.max_conflicts:
            raise OutputLimitExceededError("The result contains too many conflicts.")
        all_evidence = _all_evidence(result)
        await self._repository.validate_evidence(repository, fingerprint, all_evidence)
        current = await self._repository.fingerprint(repository)
        if current.digest != fingerprint.digest:
            raise RepositoryChangedError

    def _validate_configured_lengths(self, request: ResolveCodebaseFactRequest) -> None:
        if len(request.question) > self._settings.max_question_chars:
            raise InvalidInputError("question exceeds the configured size limit")
        if request.context is not None and len(request.context) > self._settings.max_context_chars:
            raise InvalidInputError("context exceeds the configured size limit")

    def _validate_result_size(self, result: VerificationResult) -> None:
        size = len(result.model_dump_json().encode("utf-8"))
        if size > self._settings.max_result_bytes:
            raise OutputLimitExceededError

    def _log_failure(
        self,
        error_code: ErrorCode,
        request_id: str,
        repository: AuthorizedRepository,
        fingerprint: RepositoryFingerprint | None,
        started: float,
    ) -> None:
        _LOGGER.warning(
            "capability failed",
            extra={
                "metadata": {
                    "request_id": request_id,
                    "capability": "resolve_codebase_fact",
                    "repository": repository.repository_id,
                    "repository_fingerprint": fingerprint.digest
                    if fingerprint is not None
                    else None,
                    "server_version": __version__,
                    "model": self._settings.codex_model,
                    "prompt_policy_version": POLICY_VERSION,
                    "duration_ms": round((time.monotonic() - started) * 1_000, 2),
                    "error_code": error_code.value,
                }
            },
        )


def _all_evidence(result: VerificationResult) -> Iterable[Evidence]:
    yield from result.evidence
    for conflict in result.conflicts:
        yield from conflict.evidence
