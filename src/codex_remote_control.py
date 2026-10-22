"""Maintain the saved Codex Remote Control choice for ChatGPT users.

Codex owns its daemon settings. Agentbot leaves an off/unconfigured setting
alone and uses native commands only after confirming ChatGPT login. Pairing
remains a human action; no pairing code or login output is shown here.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .command_runner import CommandRunner
    from .paths import AgentbotPaths

_TIMEOUT_SECONDS = 90


def _codex(paths: AgentbotPaths) -> str | None:
    # A missing ~/.codex means Codex was never set up here, the same rule the
    # config merge applies. It also keeps a sandboxed run off the real daemon.
    if not paths.codex_home.is_dir():
        return None
    return shutil.which("codex")


def _persisted_enabled(paths: AgentbotPaths) -> bool:
    settings = paths.codex_home / "app-server-daemon" / "settings.json"
    try:
        return json.loads(settings.read_text(encoding="utf-8")).get("remoteControlEnabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def _chatgpt_login(codex: str, paths: AgentbotPaths, runner: CommandRunner) -> bool:
    result = runner.run(
        [codex, "login", "status"],
        cwd=paths.codex_home.parent,
        timeout_seconds=15,
    )
    return result.ok and any(
        line.strip().casefold() == "logged in using chatgpt"
        for line in (result.stdout + "\n" + result.stderr).splitlines()
    )


def inspect(paths: AgentbotPaths, runner: CommandRunner) -> tuple[str, str]:
    codex = _codex(paths)
    if codex is None:
        return "codex not set up", "skipped"
    if not _persisted_enabled(paths):
        return "remote control off; enable explicitly with `codex remote-control start`", "skipped"
    if not _chatgpt_login(codex, paths, runner):
        return "ChatGPT login not confirmed; remote control unchanged", "skipped"
    running = runner.run([codex, "app-server", "daemon", "version"], timeout_seconds=30)
    if not running.ok:
        # Persisted but stopped is the normal state after a reboot: the next
        # `codex` session starts the daemon with remote control on.
        return "remote control on; daemon starts with the next codex session", "ok"
    return "remote control on; daemon running", "ok"


def ensure(paths: AgentbotPaths, runner: CommandRunner) -> tuple[str, str]:
    codex = _codex(paths)
    if codex is None:
        return "codex not set up", "skipped"
    if not _persisted_enabled(paths):
        return "remote control off; enable explicitly with `codex remote-control start`", "skipped"
    if not _chatgpt_login(codex, paths, runner):
        return "ChatGPT login not confirmed; remote control unchanged", "skipped"
    result = runner.run(
        [codex, "remote-control", "start", "--json"],
        cwd=paths.codex_home.parent,
        timeout_seconds=_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return f"remote-control start failed: {result.detail()}", "check"
    try:
        status = json.loads(result.stdout).get("status")
    except (ValueError, AttributeError):
        status = None
    if status != "connected":
        return f"remote control on; relay status {status or 'unknown'}", "check"
    return "remote control on; daemon connected", "ok"
