"""Cursor CLI statusLine ownership.

Cursor's CLI runs a user-configured command on each conversation update, feeds
it a JSON payload on stdin, and renders its stdout above the prompt. The
contract is aligned with Claude Code's but is not the same one, so this is a
separate script and a separate config block rather than the Claude statusline
pointed at a second file:

  * width arrives as ``render_width_chars`` in the payload, where Claude's
    script reads ``COLUMNS``;
  * there are no rate-limit fields;
  * ``vim.mode`` and ``worktree.name`` exist here and have no Claude equivalent.

The Cursor IDE is deliberately not covered. Cursor documents no way to point
the editor's status bar at a managed command -- that surface is VS Code's,
reachable only from an extension -- so claiming it would be claiming something
untested.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import write_text_atomic
from .models import DoctorIssue
from .paths import AgentbotPaths

STATUSLINE_SCRIPT_NAME = "statusline-command.sh"
STATUSLINE_COMMAND = f"~/.cursor/{STATUSLINE_SCRIPT_NAME}"
CONFIG_NAME = "cli-config.json"

# Cursor clamps updateIntervalMs to >= 300 and kills the command at timeoutMs.
# Both are stated so the managed block is explicit rather than inheriting
# defaults that could move under us.
DESIRED_BLOCK: dict[str, object] = {
    "type": "command",
    "command": STATUSLINE_COMMAND,
    "padding": 0,
    "updateIntervalMs": 300,
    "timeoutMs": 2000,
}


@dataclass(frozen=True)
class CursorStatuslineState:
    """What is on disk, and whether Agentbot may write it."""

    state: str
    detail: str

    @property
    def result(self) -> str:
        return {
            "ready": "ok",
            "missing": "missing",
            "stale": "check",
            "unowned": "check",
            "broken": "check",
        }.get(self.state, "check")


def statusline_source(paths: AgentbotPaths) -> Path:
    return paths.root / "global" / "cursor" / STATUSLINE_SCRIPT_NAME


def statusline_destination(paths: AgentbotPaths) -> Path:
    return paths.cursor_home / STATUSLINE_SCRIPT_NAME


def config_path(paths: AgentbotPaths) -> Path:
    return paths.cursor_home / CONFIG_NAME


def _read_config(path: Path) -> tuple[dict, str | None]:
    if not path.is_file():
        return {}, None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return {}, f"{path} is not valid JSON: {error}"
    if not isinstance(loaded, dict):
        return {}, f"{path} root must be an object"
    return loaded, None


def _is_managed_command(command: str, paths: AgentbotPaths) -> bool:
    """Whether a configured command is the one Agentbot writes.

    Compared against the resolved destination as well as the literal `~` form:
    Cursor expands `~` itself, so an operator who wrote the absolute path means
    the same file. Resolving through `paths` rather than the real home keeps
    this correct wherever the Cursor home actually is.
    """
    return command in {
        STATUSLINE_COMMAND,
        os.path.expanduser(STATUSLINE_COMMAND),
        str(statusline_destination(paths)),
    }


def inspect_cursor_statusline(paths: AgentbotPaths) -> CursorStatuslineState:
    destination = statusline_destination(paths)
    config, error = _read_config(config_path(paths))
    if error:
        return CursorStatuslineState("broken", error)

    block = config.get("statusLine")
    if not isinstance(block, dict):
        return CursorStatuslineState("missing", "no statusLine configured")

    command = str(block.get("command") or "")
    if command and not _is_managed_command(command, paths):
        # Somebody else's statusline. Reporting it is useful; replacing it is
        # not ours to do.
        return CursorStatuslineState("unowned", f"points at {command}")

    if not destination.is_file():
        return CursorStatuslineState("stale", f"configured but {destination} is missing")

    source = statusline_source(paths)
    if source.is_file() and source.read_text(encoding="utf-8") != destination.read_text(
        encoding="utf-8"
    ):
        return CursorStatuslineState("stale", "installed script differs from the managed source")

    return CursorStatuslineState("ready", str(destination))


def install_cursor_statusline(paths: AgentbotPaths) -> CursorStatuslineState:
    """Install the managed script and point Cursor's config at it.

    Refuses to take over a statusLine the operator pointed somewhere else, and
    leaves every other key in cli-config.json untouched.
    """
    source = statusline_source(paths)
    if not source.is_file():
        return CursorStatuslineState("broken", f"missing managed source: {source}")

    config_file = config_path(paths)
    config, error = _read_config(config_file)
    if error:
        return CursorStatuslineState("broken", error)

    block = config.get("statusLine")
    if isinstance(block, dict):
        command = str(block.get("command") or "")
        if command and not _is_managed_command(command, paths):
            return CursorStatuslineState("unowned", f"preserved: points at {command}")

    destination = statusline_destination(paths)
    if destination.is_symlink():
        return CursorStatuslineState(
            "broken", f"refusing to write through a symlink: {destination}"
        )
    write_text_atomic(destination, source.read_text(encoding="utf-8"), backup=destination.is_file())
    destination.chmod(0o755)

    if block != DESIRED_BLOCK:
        config["statusLine"] = dict(DESIRED_BLOCK)
        write_text_atomic(
            config_file,
            json.dumps(config, indent=2) + "\n",
            backup=config_file.is_file(),
        )

    return inspect_cursor_statusline(paths)


def doctor_cursor_statusline(
    paths: AgentbotPaths, *, state: CursorStatuslineState | None = None
) -> list[DoctorIssue]:
    """Report missing, stale, unowned, and broken configuration.

    An unowned statusline is a warning, not an error: the operator chose it,
    and Agentbot's job there is to say so rather than to object.
    """
    state = state or inspect_cursor_statusline(paths)
    if state.state == "ready":
        return []
    level = "error" if state.state == "broken" else "warning"
    hint = {
        "missing": "run 'agentbot cursor statusline' to install it",
        "stale": "run 'agentbot cursor statusline' to reinstall it",
        "unowned": "left as configured; Agentbot will not replace it",
        "broken": "fix the file, then rerun",
    }.get(state.state, "")
    return [
        DoctorIssue(
            level=level,
            scope="cursor-statusline",
            message=f"Cursor statusline {state.state}: {state.detail} ({hint})",
        )
    ]
