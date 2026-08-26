"""Application service for the single Maestro v1 Capability."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass

from maestro import __version__
from maestro.agents.runtime import AgentRuntime, InvestigationRequest
from maestro.audit import AuditExecutionHandle, AuditFailureStage, AuditRecorder
from maestro.capabilities.resolve_codebase_fact.audit_mapping import (
    map_result_to_audit_completion,
)
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
    AuditPersistenceError,
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
_CANCELLATION_AUDIT_BUDGET_SECONDS = 1.0
_CANCELLATION_AUDIT_DRAIN_RESERVE_SECONDS = 0.1


@dataclass(slots=True)
class _ExecutionState:
    request_id: str
    started: float
    fingerprint: RepositoryFingerprint | None = None
    audit_handle: AuditExecutionHandle | None = None
    failure_stage: AuditFailureStage | None = None


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
        state = _ExecutionState(request_id=uuid.uuid4().hex, started=time.monotonic())
        queued_at = state.started
        depth_token = _DEPTH.set(1)
        try:
            async with self._admission.slot():
                queue_duration_ms = round((time.monotonic() - queued_at) * 1_000, 2)
                async with asyncio.timeout(self._settings.verifier_timeout_seconds):
                    fingerprint = await self._repository.fingerprint(repository)
                    state.fingerprint = fingerprint
                    objective = neutralize_question(request.question)
                state.audit_handle = await self._audit.start_resolve_codebase_fact(
                    repository,
                    fingerprint,
                    objective,
                )
                state.request_id = state.audit_handle.execution_id.hex
                state.failure_stage = AuditFailureStage.INVESTIGATION
                async with asyncio.timeout(self._settings.verifier_timeout_seconds):
                    result = await self._investigate(
                        repository,
                        request,
                        fingerprint,
                        objective,
                    )
                    state.failure_stage = AuditFailureStage.VALIDATION
                    await self._validate_result(repository, fingerprint, result)
                    result = sanitize_result(result, repository.root)
                    self._validate_result_size(result)
                state.failure_stage = AuditFailureStage.TERMINAL_PERSISTENCE
                await self._audit.record_investigation_completed(
                    state.audit_handle,
                    repository,
                    map_result_to_audit_completion(result),
                )
        except TimeoutError as exc:
            error = AgentTimeoutError()
            await self._handle_maestro_failure(error, repository, state)
            raise error from exc
        except asyncio.CancelledError:
            await self._handle_cancellation(repository, state)
            raise
        except MaestroError as exc:
            await self._handle_maestro_failure(exc, repository, state)
            raise
        except Exception:
            await self._handle_unexpected_failure(repository, state)
            raise
        else:
            _LOGGER.info(
                "capability completed",
                extra={
                    "metadata": {
                        "request_id": state.request_id,
                        "capability": "resolve_codebase_fact",
                        "repository": repository.repository_id,
                        "repository_fingerprint": fingerprint.digest,
                        "server_version": __version__,
                        "model": self._settings.codex_model.value,
                        "prompt_policy_version": POLICY_VERSION,
                        "duration_ms": round((time.monotonic() - state.started) * 1_000, 2),
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
        repository: AuthorizedRepository,
        state: _ExecutionState,
    ) -> None:
        _LOGGER.warning(
            "capability failed",
            extra={
                "metadata": {
                    "request_id": state.request_id,
                    "capability": "resolve_codebase_fact",
                    "repository": repository.repository_id,
                    "repository_fingerprint": state.fingerprint.digest
                    if state.fingerprint is not None
                    else None,
                    "server_version": __version__,
                    "model": self._settings.codex_model.value,
                    "prompt_policy_version": POLICY_VERSION,
                    "duration_ms": round((time.monotonic() - state.started) * 1_000, 2),
                    "error_code": error_code.value,
                    "failure_stage": (
                        state.failure_stage.value if state.failure_stage is not None else None
                    ),
                }
            },
        )

    async def _handle_maestro_failure(
        self,
        error: MaestroError,
        repository: AuthorizedRepository,
        state: _ExecutionState,
    ) -> None:
        self._log_failure(error.code, repository, state)
        if state.failure_stage is not AuditFailureStage.TERMINAL_PERSISTENCE:
            await self._record_failure_if_started(
                state.audit_handle,
                error.code,
                state.failure_stage,
            )

    async def _handle_unexpected_failure(
        self,
        repository: AuthorizedRepository,
        state: _ExecutionState,
    ) -> None:
        self._log_failure(ErrorCode.INTERNAL_ERROR, repository, state)
        await self._record_failure_if_started(
            state.audit_handle,
            ErrorCode.INTERNAL_ERROR,
            state.failure_stage,
        )

    async def _handle_cancellation(
        self,
        repository: AuthorizedRepository,
        state: _ExecutionState,
    ) -> None:
        self._log_failure(ErrorCode.AGENT_CANCELLED, repository, state)
        await self._record_cancellation_if_started(state.audit_handle, state.failure_stage)

    async def _record_failure_if_started(
        self,
        handle: AuditExecutionHandle | None,
        error_code: ErrorCode,
        failure_stage: AuditFailureStage | None,
    ) -> None:
        if handle is None or failure_stage is None:
            return
        try:
            await self._audit.record_execution_failed(handle, error_code, failure_stage)
        except asyncio.CancelledError:
            raise
        except MaestroError:
            raise
        except Exception:
            raise AuditPersistenceError from None

    async def _record_cancellation_if_started(
        self,
        handle: AuditExecutionHandle | None,
        failure_stage: AuditFailureStage | None,
    ) -> None:
        if handle is None or failure_stage is None:
            return
        cleanup = asyncio.create_task(
            self._audit.record_execution_failed(
                handle,
                ErrorCode.AGENT_CANCELLED,
                failure_stage,
            ),
            name=f"audit-cancellation-{handle.execution_id.hex}",
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CANCELLATION_AUDIT_BUDGET_SECONDS
        drain_reserve = min(
            _CANCELLATION_AUDIT_DRAIN_RESERVE_SECONDS,
            _CANCELLATION_AUDIT_BUDGET_SECONDS / 5,
        )
        await self._wait_for_cleanup(cleanup, deadline - drain_reserve)
        if not cleanup.done():
            self._abort_and_cancel_cleanup(handle, cleanup)
        exceeded_budget = await self._join_cleanup_task(cleanup, handle, deadline)
        if exceeded_budget:
            _LOGGER.warning(
                "audit cancellation cleanup exceeded cooperative budget",
                extra={
                    "metadata": {
                        "request_id": handle.execution_id.hex,
                        "capability": "resolve_codebase_fact",
                        "error_code": ErrorCode.AGENT_CANCELLED.value,
                        "failure_stage": failure_stage.value,
                    }
                },
            )
        established = cleanup.done() and not cleanup.cancelled() and cleanup.exception() is None
        if not established:
            _LOGGER.warning(
                "audit cancellation cleanup incomplete",
                extra={
                    "metadata": {
                        "request_id": handle.execution_id.hex,
                        "capability": "resolve_codebase_fact",
                        "error_code": ErrorCode.AGENT_CANCELLED.value,
                        "failure_stage": failure_stage.value,
                    }
                },
            )

    @staticmethod
    async def _wait_for_cleanup(cleanup: asyncio.Task[None], deadline: float) -> None:
        """Wait within the persistence-attempt share without cancelling the owned task."""

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait({cleanup}, timeout=remaining)
        except asyncio.CancelledError:
            # A repeated caller cancellation accelerates abort; the original cancellation is
            # re-raised by execute only after this owned task is quiescent.
            return

    def _abort_and_cancel_cleanup(
        self,
        handle: AuditExecutionHandle,
        cleanup: asyncio.Task[None],
    ) -> None:
        with suppress(Exception):
            self._audit.abort_execution_failure(handle)
        cleanup.cancel()

    async def _join_cleanup_task(
        self,
        cleanup: asyncio.Task[None],
        handle: AuditExecutionHandle,
        deadline: float,
    ) -> bool:
        """Join despite repeated cancellation, preserving quiescence beyond a bad port."""

        loop = asyncio.get_running_loop()
        while not cleanup.done() and (remaining := deadline - loop.time()) > 0:
            try:
                await asyncio.wait({cleanup}, timeout=remaining)
            except asyncio.CancelledError:
                self._abort_and_cancel_cleanup(handle, cleanup)
        exceeded_budget = not cleanup.done()
        while not cleanup.done():
            self._abort_and_cancel_cleanup(handle, cleanup)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(BaseException):
            cleanup.result()
        return exceeded_budget


def _all_evidence(result: VerificationResult) -> Iterable[Evidence]:
    yield from result.evidence
    for conflict in result.conflicts:
        yield from conflict.evidence
