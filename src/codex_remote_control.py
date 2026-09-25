"""Keep Codex Remote Control on, so the ChatGPT app can reach this machine.

Claude's equivalent is one key in its settings file, `remoteControlAtStartup`,
and lives in `cli/claude.settings.json`. Codex has no config key for it: the
switch is persisted by its app-server daemon in
`~/.codex/app-server-daemon/settings.json`, which Codex owns and rewrites. So
this module drives Codex's own command rather than editing that file.

`codex remote-control start` persists the setting, starts the daemon if it is
not running, and is a no-op when both are already true. Every later daemon
start -- including the one the interactive `codex` TUI triggers itself --
honours the persisted setting.

Pairing a phone is deliberately not here: its code is a secret and pairing is
a one-time operator step (`codex remote-control pair`).
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


def inspect(paths: AgentbotPaths, runner: CommandRunner) -> tuple[str, str]:
    codex = _codex(paths)
    if codex is None:
        return "codex not set up", "skipped"
    if not _persisted_enabled(paths):
        return "remote control off; run `agentbot cli-config apply`", "check"
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
