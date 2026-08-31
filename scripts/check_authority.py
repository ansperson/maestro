#!/usr/bin/env python3
"""Ask whether one proposed action may proceed, against a real GitHub issue.

This is the development entry point behind `make authority`. It stands in for the Unblocker
ADR-0006 describes, which pauses a run and resumes it after approval and needs durable Jobs
(ADR-0008). Until those exist the flow completes by re-running after approval, which still
exercises the port, the engine, the block, and Audit end to end.

Refused once, approved on the issue, run again: cleared.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from maestro import __version__
from maestro.audit.postgres.adapter import PostgresAuditPort
from maestro.audit.recorder import AuditRecorder, AuditRuntimeMetadata
from maestro.authority.contracts import ActionTarget, ProposedAction
from maestro.authority.documents import read_authority_documents
from maestro.authority.engine import AuthorityOutcome
from maestro.authority.github import GitHubWorkItemPort
from maestro.authority.service import AuthorityService
from maestro.config import Settings, load_work_item_settings
from maestro.errors import MaestroError
from maestro.observability.logging import configure_logging
from maestro.repository.guard import RepositoryGuard

_AUTHORITY_POLICY_VERSION = "decision-authority/v1"


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="the repository the action is in")
    parser.add_argument("--issue", required=True, help="the work item the action belongs to")
    parser.add_argument("--subject", required=True, help="what the action would settle")
    parser.add_argument("--choice", required=True, help="the option the action would apply")
    parser.add_argument("--project", required=True, help="the project the action is in")
    parser.add_argument(
        "--document",
        action="append",
        default=[],
        help="an authority document to read; repeatable, and never discovered by scanning",
    )
    return parser.parse_args()


def _render(outcome: AuthorityOutcome) -> None:
    print(f"\n  outcome     {outcome.kind.value}")
    print(f"  action      {outcome.action.describe()}")
    print(f"\n  {outcome.summary}")
    if outcome.applied is not None:
        print(f"\n  applied     {outcome.applied.describe()}")
    for entry in outcome.considered:
        print(f"  considered  {entry.describe()}")


async def main() -> int:
    """Evaluate one action and report the engine's single answer."""

    options = _parse_arguments()
    settings = Settings()  # pyright: ignore[reportCallIssue] - values come from environment
    configure_logging(settings.log_level)
    work_item_settings = load_work_item_settings(settings)
    guard = RepositoryGuard(settings)
    repository = guard.authorize(options.repository)
    fingerprint = await guard.fingerprint(repository)
    service = AuthorityService(
        GitHubWorkItemPort(work_item_settings.work_item_configuration()),
        AuditRecorder(
            PostgresAuditPort(settings.audit_writer_configuration()),
            AuditRuntimeMetadata(
                server_version=__version__,
                runtime_name="authority",
                runtime_version=__version__,
                model=settings.agent_model(),
                prompt_policy_version=_AUTHORITY_POLICY_VERSION,
            ),
        ),
        documents=read_authority_documents(tuple(Path(document) for document in options.document)),
    )
    action = ProposedAction(
        subject=options.subject,
        choice=options.choice,
        target=ActionTarget(project=options.project, work_item=options.issue),
    )
    try:
        outcome = await service.authorize(action, repository, fingerprint)
    except MaestroError as error:
        print(f"\n  refused     {error.public_json()}", file=sys.stderr)
        print("  the request, if any, is now on the work item; answer it and run again.")
        return 1
    _render(outcome)
    return 0


if __name__ == "__main__":
    if os.name != "posix":
        raise SystemExit("the development entry point requires a POSIX platform")
    raise SystemExit(asyncio.run(main()))
