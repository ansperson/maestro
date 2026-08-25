"""Private process entry point that owns one official Codex SDK lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
from pydantic import ValidationError

from maestro.agents.codex.protocol import (
    CodexWorkerFailure,
    CodexWorkerRequest,
    CodexWorkerSuccess,
)
from maestro.capabilities.resolve_codebase_fact.contracts import VerificationResult
from maestro.capabilities.resolve_codebase_fact.policy import (
    VERIFIER_INSTRUCTIONS,
    build_verifier_prompt,
)


async def investigate(request: CodexWorkerRequest) -> VerificationResult:
    """Perform exactly one structured, deny-escalation, read-only Codex turn."""

    codex_home = Path(os.environ["CODEX_HOME"])
    _write_isolated_config(codex_home, request.model)
    config = CodexConfig(cwd=str(request.repository_root), env={"CODEX_HOME": str(codex_home)})
    async with AsyncCodex(config) as codex:
        api_key = os.environ.get("MAESTRO_CODEX_API_KEY")
        if api_key is not None:
            await codex.login_api_key(api_key)
        thread = await codex.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=str(request.repository_root),
            developer_instructions=VERIFIER_INSTRUCTIONS,
            ephemeral=True,
            model=request.model,
            sandbox=Sandbox.read_only,
        )
        handle = await thread.turn(
            build_verifier_prompt(request.question, request.context),
            approval_mode=ApprovalMode.deny_all,
            output_schema=VerificationResult.model_json_schema(mode="validation"),
            sandbox=Sandbox.read_only,
        )
        try:
            turn_result = await handle.run()
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(handle.interrupt())
            raise
    response = turn_result.final_response
    if response is None or len(response.encode("utf-8")) > request.max_output_bytes:
        raise ValueError("missing or oversized final response")
    return VerificationResult.model_validate_json(response, strict=True)


def _write_isolated_config(codex_home: Path, model: str) -> None:
    """Write the complete clean-home config; no user/project layer is inherited."""

    shell_path = os.defpath
    home = str(Path.home())
    temporary_directory = os.environ["TMPDIR"]
    config = f"""model = {json.dumps(model)}
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
project_doc_max_bytes = 0
file_opener = "none"

[feedback]
enabled = false

[features]
apps = false
goals = false
hooks = false
memories = false
multi_agent = false
shell_tool = true
skill_mcp_dependency_install = false
web_search = false

[shell_environment_policy]
inherit = "none"
ignore_default_excludes = false
set = {{
  PATH = {json.dumps(shell_path)},
  HOME = {json.dumps(home)},
  TMPDIR = {json.dumps(temporary_directory)},
  MAESTRO_VERIFIER_DEPTH = "1"
}}
"""
    path = codex_home / "config.toml"
    path.write_text(config, encoding="utf-8")
    path.chmod(0o600)


async def _run() -> int:
    try:
        raw_request = await asyncio.to_thread(sys.stdin.buffer.read)
        request = CodexWorkerRequest.model_validate_json(raw_request, strict=True)
        result = await investigate(request)
        response = CodexWorkerSuccess(result=result)
    except (ValidationError, ValueError):
        response = CodexWorkerFailure(category="invalid_output")
    except asyncio.CancelledError:
        raise
    except Exception:
        response = CodexWorkerFailure(category="runtime")
    sys.stdout.write(response.model_dump_json())
    sys.stdout.flush()
    return 0


def main() -> None:
    """Install cancellation-aware signal handlers and run one worker request."""

    async def runner() -> int:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        if task is not None and os.name == "posix":
            for signal_number in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signal_number, task.cancel)
        return await _run()

    try:
        exit_code = asyncio.run(runner())
    except (KeyboardInterrupt, asyncio.CancelledError):
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
