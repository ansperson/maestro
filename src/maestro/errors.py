"""Typed operational failures kept separate from epistemic results."""

from __future__ import annotations

import json
from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable public operational error codes."""

    INVALID_INPUT = "INVALID_INPUT"
    REPOSITORY_NOT_ALLOWED = "REPOSITORY_NOT_ALLOWED"
    REPOSITORY_NOT_FOUND = "REPOSITORY_NOT_FOUND"
    REPOSITORY_CHANGED_DURING_INVESTIGATION = "REPOSITORY_CHANGED_DURING_INVESTIGATION"
    SERVER_BUSY = "SERVER_BUSY"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_CANCELLED = "AGENT_CANCELLED"
    AGENT_RUNTIME_ERROR = "AGENT_RUNTIME_ERROR"
    INVALID_AGENT_OUTPUT = "INVALID_AGENT_OUTPUT"
    EVIDENCE_VALIDATION_ERROR = "EVIDENCE_VALIDATION_ERROR"
    RECURSION_NOT_ALLOWED = "RECURSION_NOT_ALLOWED"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    AUDIT_PERSISTENCE_ERROR = "AUDIT_PERSISTENCE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MaestroError(Exception):
    """Base exception with a stable code and client-safe message."""

    code = ErrorCode.INTERNAL_ERROR
    default_message = "Maestro could not complete the request."

    def __init__(self, message: str | None = None) -> None:
        self.safe_message = message or self.default_message
        super().__init__(self.safe_message)

    def public_json(self) -> str:
        """Return a stable tool-visible payload without internal details."""

        return json.dumps(
            {"code": self.code.value, "message": self.safe_message},
            ensure_ascii=True,
            separators=(",", ":"),
        )


class InvalidInputError(MaestroError):
    code = ErrorCode.INVALID_INPUT
    default_message = "The request is invalid."


class RepositoryNotAllowedError(MaestroError):
    code = ErrorCode.REPOSITORY_NOT_ALLOWED
    default_message = "The repository is outside the configured allowed roots."


class RepositoryNotFoundError(MaestroError):
    code = ErrorCode.REPOSITORY_NOT_FOUND
    default_message = "The repository path does not exist or is not a directory."


class RepositoryChangedError(MaestroError):
    code = ErrorCode.REPOSITORY_CHANGED_DURING_INVESTIGATION
    default_message = "The repository changed during investigation; retry against a stable state."


class ServerBusyError(MaestroError):
    code = ErrorCode.SERVER_BUSY
    default_message = "The verifier is at capacity; retry later."


class AgentTimeoutError(MaestroError):
    code = ErrorCode.AGENT_TIMEOUT
    default_message = "The verifier exceeded its configured deadline."


class AgentCancelledError(MaestroError):
    code = ErrorCode.AGENT_CANCELLED
    default_message = "The verifier worker was cancelled."


class AgentRuntimeError(MaestroError):
    code = ErrorCode.AGENT_RUNTIME_ERROR
    default_message = "The verifier runtime failed."


class InvalidAgentOutputError(MaestroError):
    code = ErrorCode.INVALID_AGENT_OUTPUT
    default_message = "The verifier returned malformed structured output."


class EvidenceValidationError(MaestroError):
    code = ErrorCode.EVIDENCE_VALIDATION_ERROR
    default_message = "The verifier returned evidence that could not be validated."


class RecursionNotAllowedError(MaestroError):
    code = ErrorCode.RECURSION_NOT_ALLOWED
    default_message = "Recursive Maestro verifier access is not allowed."


class OutputLimitExceededError(MaestroError):
    code = ErrorCode.OUTPUT_LIMIT_EXCEEDED
    default_message = "The validated result exceeds the configured output limit."


class AuditUnavailableError(MaestroError):
    code = ErrorCode.AUDIT_UNAVAILABLE
    default_message = "Audit persistence is temporarily unavailable."


class AuditPersistenceError(MaestroError):
    code = ErrorCode.AUDIT_PERSISTENCE_ERROR
    default_message = "Audit persistence could not establish the required durable record."
