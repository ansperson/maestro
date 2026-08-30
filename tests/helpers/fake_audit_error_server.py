"""Real-stdio fixture server for public Audit persistence error mapping."""

from __future__ import annotations

import sys
from pathlib import Path

from audit_boundary_fixtures import audit_payload_boundary_result

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
    mode = sys.argv[2]

    settings = Settings.model_validate(
        {
            "allowed_roots": (repository,),
        }
    )
    if mode == "payload_overflow":
        runtime = FakeAgentRuntime(lambda _request: audit_payload_boundary_result(overflow=True))
        audit = fake_audit_recorder()
    else:
        kind = AuditWriteFailureKind(mode)

        def fail_start(_record: object) -> None:
            raise AuditWriteError(kind)

        runtime = FakeAgentRuntime(_unexpected_worker)
        audit = fake_audit_recorder(FakeAuditPort(on_start=fail_start))
    service = ResolveCodebaseFactService(settings, runtime, audit)
    create_server(service).run(transport="stdio")


if __name__ == "__main__":
    main()
