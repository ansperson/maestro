"""Real-stdio fixture server for public Audit persistence error mapping."""

from __future__ import annotations

import sys
from pathlib import Path

from maestro.agents import FakeAgentRuntime
from maestro.audit.port import AuditWriteError, AuditWriteFailureKind
from maestro.audit.testing import FakeAuditPort, fake_audit_recorder
from maestro.capabilities.resolve_codebase_fact.contracts import VerificationResult
from maestro.capabilities.resolve_codebase_fact.service import ResolveCodebaseFactService
from maestro.config import Settings
from maestro.mcp.server import create_server


def _unexpected_worker(_request: object) -> VerificationResult:
    raise AssertionError("Audit start failure must prevent worker execution")


def main() -> None:
    """Run a fixture whose Audit start fails with the selected safe classification."""

    repository = Path(sys.argv[1])
    kind = AuditWriteFailureKind(sys.argv[2])

    def fail_start(_record: object) -> None:
        raise AuditWriteError(kind)

    settings = Settings.model_validate(
        {
            "allowed_roots": (repository,),
            "audit_database_url": "postgresql://audit-writer@127.0.0.1:1/maestro",
        }
    )
    service = ResolveCodebaseFactService(
        settings,
        FakeAgentRuntime(_unexpected_worker),
        fake_audit_recorder(FakeAuditPort(on_start=fail_start)),
    )
    create_server(service).run(transport="stdio")


if __name__ == "__main__":
    main()
